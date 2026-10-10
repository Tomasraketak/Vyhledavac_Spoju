"""Čtení spojení z IDOS (idos.cz) – vlaky, autobusy i MHD.

Web ČD autobusy integrovaných systémů nevyhledá, IDOS ano. Stránka výsledků
``/vlakyautobusymhdvse/spojeni/vysledky/?f=…&t=…&date=…&time=…`` má spojení
přímo v HTML (3 na stránku). Každý úsek má název linky („Bus 330110 9“,
„Os 8715“, „R17 (R 730 Lužnice)“), druh v ``title`` („regionální autobus“,
„dálkový autobus“, „osobní vlak“…), dopravce a nástupní/výstupní zastávku.
Odkaz ``/spojeni/draha/?…`` vede na celou trasu spoje (všechny zastávky) –
ten se načítá jen tam, kde tarif počítá projeté zóny.
"""

from __future__ import annotations

import datetime as dt
import html as htmlmod
import re
from dataclasses import dataclass, field

from .models import DEFAULT_TZ

BASE = "https://idos.cz"
RESULTS = f"{BASE}/vlakyautobusymhdvse/spojeni/vysledky/"


@dataclass
class IdosLeg:
    line: str                       # „Bus 330110 9“, „Os 8715“
    kind: str                       # „regionální autobus“, „osobní vlak“…
    carrier: str
    origin: str
    destination: str
    departure: dt.datetime
    arrival: dt.datetime
    detail_url: str = ""
    stops: list[str] = field(default_factory=list)   # celá projetá trasa (z detailu)

    @property
    def is_bus(self) -> bool:
        return "autobus" in self.kind or self.line.lower().startswith("bus")

    @property
    def is_city(self) -> bool:
        return any(w in self.kind.lower() for w in ("tramvaj", "trolejbus", "metro", "městsk", "mhd"))

    @property
    def licence(self) -> str:
        """Číslo licence autobusové linky („Bus 330110 9“ -> „330110“)."""
        m = re.search(r"\b(\d{6})\b", self.line)
        return m.group(1) if m else ""

    @property
    def train_label(self) -> str:
        """„R17 (R 730 Lužnice)“ -> „R 730 Lužnice“; jinak název linky."""
        m = re.search(r"\(([^)]+)\)", self.line)
        return m.group(1) if m else self.line


@dataclass
class IdosConnection:
    legs: list[IdosLeg]
    share_url: str = ""

    @property
    def departure(self) -> dt.datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> dt.datetime:
        return self.legs[-1].arrival


def results_url() -> str:
    return RESULTS


def params(origin: str, destination: str, when: dt.datetime) -> dict[str, str]:
    when = when.astimezone(DEFAULT_TZ)
    return {"f": origin, "t": destination, "date": f"{when.day}.{when.month}.{when.year}",
            "time": f"{when.hour}:{when.minute:02d}"}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _at(day: dt.date, hhmm: str, after: dt.datetime | None) -> dt.datetime | None:
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    value = dt.datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)),
                        tzinfo=DEFAULT_TZ)
    while after is not None and value < after - dt.timedelta(hours=1):
        value += dt.timedelta(days=1)          # přes půlnoc
    return value


def parse_results(page: str, day: dt.date) -> list[IdosConnection]:
    out = []
    boxes = re.split(r'<div id="connectionBox-\d+" class="box connection', page)[1:]
    if not boxes:      # první box nemusí mít id
        boxes = re.split(r'class="box connection detail-box', page)[1:]
    for box in boxes:
        share = re.search(r'data-share-url="([^"]+)"', box)
        legs: list[IdosLeg] = []
        last: dt.datetime | None = None
        for item in re.split(r'<div class="outside-of-popup[^"]*">', box)[1:]:
            title = re.search(r'<h3 title="([^"]*)"[^>]*>(.*?)</h3>', item, re.S)
            if not title:
                continue
            kind = htmlmod.unescape(title.group(1)).split("(")[0].strip()
            line = _text(title.group(2))
            owner = re.search(r'class="owner">(.*?)</span>\s*</span>', item, re.S)
            href = re.search(r'href="(https://idos\.cz/[^"]*/spojeni/draha/[^"]+)"', item)
            stations = re.findall(
                r'<p class="reset time[^"]*"[^>]*>([^<]*)</p>\s*<p class="station">\s*<strong class="name[^"]*">(.*?)</strong>',
                item, re.S)
            if len(stations) < 2:
                continue
            dep = _at(day, stations[0][0], last)
            arr = _at(day, stations[-1][0], dep)
            if dep is None or arr is None:
                continue
            last = arr
            legs.append(IdosLeg(line=line, kind=kind, carrier=_text(owner.group(1)) if owner else "",
                                origin=_text(stations[0][1]), destination=_text(stations[-1][1]),
                                departure=dep, arrival=arr,
                                detail_url=htmlmod.unescape(href.group(1)) if href else ""))
        if legs:
            out.append(IdosConnection(legs, htmlmod.unescape(share.group(1)) if share else ""))
    return out


def parse_route(page: str) -> list[str]:
    """Zastávky projetého úseku z detailu spoje (``li.item`` bez ``inactive``)."""
    names = []
    for cls, body in re.findall(r'<li class="(item[^"]*)"[^>]*>(.*?)</li>', page, re.S):
        if "inactive" in cls:
            continue
        name = re.search(r'<strong class="name[^"]*">(.*?)</strong>', body, re.S)
        if name is None:
            name = re.search(r'class="station"[^>]*>(.*?)</', body, re.S)
        if name:
            names.append(_text(name.group(1)))
    return names


IDS_TAGS = ("IDESKA", "PID", "IDS JMK", "IDOL", "IREDO", "DÚK", "ODIS", "IDZK", "IDSOK", "IDPK",
            "VDV", "IDS JK", "IDS KK")


def parse_line_info(page: str) -> tuple[str, set[str]]:
    """Z detailu spoje: číslo licence linky a systémy IDS („linka 300 [IDESKA]“)."""
    text = _text(re.sub(r"<script.*?</script>", " ", page, flags=re.S))
    systems = set()
    for tag in re.findall(r"\[([^\]]{2,20})\]", text):
        tag = tag.strip()
        if tag == "IDS JK":
            tag = "IDESKA"
        if tag in IDS_TAGS:
            systems.add(tag)
    licence = re.search(r"\b(\d{6})\b(?=\s*\d{0,4}\)?)", text)
    return (licence.group(1) if licence else ""), systems


def display_name(name: str) -> str:
    """„Třeboň,,Táboritská“ -> „Třeboň, Táboritská“ (formát CIS JŘ)."""
    return ", ".join(p.strip() for p in name.split(",") if p.strip())
