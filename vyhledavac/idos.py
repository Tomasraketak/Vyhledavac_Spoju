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
from decimal import Decimal

from . import geo
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
    # projeté zastávky (jméno, příjezd, odjezd), když je zná už zdroj spojení (Transitous)
    inline_stops: list[tuple[str, str, str]] = field(default_factory=list)
    walk_before: int = 0            # pěší přesun před tímto úsekem (min), „Přesun asi 8 min“

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
    handle: int = 0                 # výsledek hledání (session IDOS) – pro nabídku jízdenky
    conn_id: int = 0

    @property
    def departure(self) -> dt.datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> dt.datetime:
        return self.legs[-1].arrival


SUGGEST = f"{BASE}/vlakyautobusymhdvse/Ajax/SearchTimetableObjects/"


def suggest_params(text: str) -> dict[str, str]:
    """Parametry našeptávače míst (JSONP, jako ho volá formulář IDOS)."""
    return {"count": "12", "prefixText": text, "positionAccuracy": "", "searchByPosition": "false",
            "onlyStation": "false", "format": "json", "bindTtIndex": "", "callback": "cb", "_": "1"}


def parse_suggest(text: str) -> list[dict]:
    """Odpověď našeptávače ``cb([...])`` → seznam míst (``selectedText`` = jednoznačné
    jméno pro hledání, např. „Kraków; Polsko“, ``description`` = „obec, Polsko, vlaky…“)."""
    import json

    text = (text or "").strip()
    if "(" not in text or not text.endswith((")", ");")):
        return []
    try:
        data = json.loads(text[text.index("(") + 1:text.rindex(")")])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def pick_foreign(items: list[dict], country: str) -> str | None:
    """Jméno zahraniční obce pro IDOS: „Košice; Slovensko“ (ne Košice u Kutné Hory).
    ``country`` je český název státu z databáze měst (Nizozemsko × IDOS „Nizozemí“ –
    stačí shoda prvních pěti písmen)."""
    stem = country[:5].lower()
    for item in items:
        text = str(item.get("selectedText") or "")
        desc = str(item.get("description") or "").lower()
        if "; " in text and desc.startswith("obec") and text.rsplit("; ", 1)[1][:5].lower() == stem:
            return text
    return None


def pick_stop(items: list[dict], town: str, whole: bool) -> dict | None:
    """Zastávka nebo stanice v obci ``town`` z našeptávače („Anděl“ – „zastávka
    (Praha)“, „Praha,,Letňany“ – „zastávka, okres Praha“); adresy ne.
    ``whole`` = hledalo se celé jméno i s obcí."""
    from .normalize import normalize

    want = normalize(town)
    for item in items:
        text = str(item.get("selectedText") or "")
        desc = str(item.get("description") or "")
        if not text or not desc.lower().startswith(("zastávka", "stanice", "část obce")):
            continue
        place = re.search(r"\(([^)]+)\)|okres ([^,]+)", desc)
        where = normalize((place.group(1) or place.group(2)) if place else "")
        if whole and normalize(text).startswith(want) or where.startswith(want) or want.startswith(where or "-"):
            return item
    return None


#: Jízdní řády IDOS: vše (vlaky, autobusy, MHD), nebo jen vlaky.
TRAINS = "vlaky"
ALL = "vlakyautobusymhdvse"


def results_url(combination: str = ALL) -> str:
    return f"{BASE}/{combination}/spojeni/vysledky/"


def ajax_url(name: str, combination: str = ALL) -> str:
    return f"{BASE}/{combination}/Ajax/{name}/" if name == "SearchTimetableObjects" else \
        f"{BASE}/{combination}/Ajax/{name}"


def params(origin: str, destination: str, when: dt.datetime,
           codes: tuple[str, str] = ("", "")) -> dict[str, str]:
    """Parametry hledání; ``codes`` = čísla míst z našeptávače (``value``) –
    nutná u nejednoznačné zastávky („Anděl“ je i v jiných obcích)."""
    when = when.astimezone(DEFAULT_TZ)
    out = {"f": origin, "t": destination, "date": f"{when.day}.{when.month}.{when.year}",
           "time": f"{when.hour}:{when.minute:02d}"}
    for key, code in zip(("fc", "tc"), codes):
        if code:
            out[key] = code
    return out


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
    handle = re.search(r'"handle":(\d+),"connData"', page)
    for box in boxes:
        share = re.search(r'data-share-url="([^"]+)"', box)
        conn_id = re.search(r"connResult\.showDetail\((\d+)", box)
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
            # IDOS píše místní čas stanice (Bukurešť 16:00 = 15:00 v Praze)
            dep = geo.local_time(dep, _text(stations[0][1]))
            arr = geo.local_time(arr, _text(stations[-1][1]))
            walk = re.search(r'class="walk[^"]*">.*?P[řr]esun\s+(?:asi\s+)?(\d+)\s*min', item, re.S)
            legs.append(IdosLeg(line=line, kind=kind, carrier=_text(owner.group(1)) if owner else "",
                                origin=_text(stations[0][1]), destination=_text(stations[-1][1]),
                                departure=dep, arrival=arr,
                                detail_url=htmlmod.unescape(href.group(1)) if href else "",
                                walk_before=int(walk.group(1)) if walk else 0))
        if legs:
            out.append(IdosConnection(legs, htmlmod.unescape(share.group(1)) if share else "",
                                      int(handle.group(1)) if handle else 0,
                                      int(conn_id.group(1)) if conn_id else 0))
    return out


OFFER = f"{BASE}/vlakyautobusymhdvse/Ajax/GetPriceOffer"


def offer_data(conn: IdosConnection) -> dict[str, str]:
    """Nabídka jízdenky ke spoji (e-shop IDOS – jízdenky ČD i na cesty do ciziny),
    pro výchozího cestujícího (dospělý)."""
    return {"handle": str(conn.handle), "connId": str(conn.conn_id), "passengers": "", "handleThere": "0",
            "connIdThere": "0", "format": "json"}


def parse_offer(text: str) -> tuple[Decimal | None, list[tuple[str, str]] | None]:
    """``cb({"price":"2 117   Kč","sellablePartsDesc":"Třeboň - Frankfurt(Main)Hbf"…})`` →
    (cena za celou nabídku, úseky, na které platí; ``None`` = celý spoj)."""
    import json

    text = (text or "").strip()
    try:
        data = json.loads(text[text.index("(") + 1:text.rindex(")")])
    except ValueError:
        return None, None
    digits = re.sub(r"[^0-9,]", "", str(data.get("price") or "")).replace(",", ".")
    if not digits or data.get("errorMsg"):
        return None, None
    parts = None
    desc = data.get("sellablePartsDesc")
    if desc:
        parts = [tuple(p.strip() for p in part.split(" - ", 1)) for part in re.split(r"<br\s*/?>", desc)
                 if " - " in part]
    return Decimal(digits), parts


def parse_stops(page: str) -> list[tuple[str, str, str]]:
    """Zastávky projetého úseku z detailu spoje (``li.item`` bez ``inactive``)
    jako ``(jméno, příjezd, odjezd)``; časy „H:MM“ nebo ``""``. Detail ukazuje
    jen zastávky, kde spoj staví – průjezdy v něm nejsou."""
    stops = []
    for cls, body in re.findall(r'<li class="(item[^"]*)"[^>]*>(.*?)</li>', page, re.S):
        if "inactive" in cls:
            continue
        name = re.search(r'<strong class="name[^"]*">(.*?)</strong>', body, re.S)
        if name is None:
            name = re.search(r'class="station"[^>]*>(.*?)</', body, re.S)
        if not name:
            continue
        times = []
        for part, end in (("arrival", r'<span class="departure">|<strong'), ("departure", r"<strong")):
            m = re.search(rf'<span class="{part}">(.*?)(?:{end})', body, re.S)
            hhmm = re.search(r"\d{1,2}:\d{2}", _text(m.group(1)) if m else "")
            times.append(hhmm.group(0) if hhmm else "")
        stops.append((_text(name.group(1)), times[0], times[1]))
    return stops


def parse_distances(page: str) -> list[float | None]:
    """Kilometry zastávek z detailu spoje („50 km“ od výchozí stanice spoje),
    ve stejném pořadí jako ``parse_stops``; ``None``, kde chybí."""
    out: list[float | None] = []
    for cls, body in re.findall(r'<li class="(item[^"]*)"[^>]*>(.*?)</li>', page, re.S):
        if "inactive" in cls:
            continue
        if not re.search(r'<strong class="name[^"]*">|class="station"', body):
            continue
        m = re.search(r'class="distance">(.*?)</span>\s*$', body, re.S) or re.search(r'class="distance">(.*)', body, re.S)
        km = re.search(r"(\d+(?:[.,]\d+)?)\s*km", _text(m.group(1)) if m else "")
        out.append(float(km.group(1).replace(",", ".")) if km else None)
    return out


def parse_route(page: str) -> list[str]:
    """Jména zastávek projetého úseku z detailu spoje."""
    return [name for name, _, _ in parse_stops(page)]


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
