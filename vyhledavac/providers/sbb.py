"""SBB – Švýcarské dráhy: jízdní řád z transport.opendata.ch, ceny ze sbb.ch.

Nový web sbb.ch volá GraphQL ``graphql.www.sbb.ch``, ale ten je za AWS
CloudFront a z datacentra vrací 403 všemu. **Starý jízdní řád**
``www.sbb.ch/en/buying/pages/fahrplan/fahrplan.xhtml`` (JSF/PrimeFaces) ale
odpovídá a u každého spoje ukazuje cenu „od“:

1. ``GET fahrplan.xhtml?von=…&nach=…&datum=DD.MM.RRRR&zeit=HH:MM&an=false&suche=true``
   – stránka se spoji (``shopForm_aktuelleVerbindungen_{i}_verbindung``)
   a formulářem ``shopForm`` (``javax.faces.ViewState``, ``jsessionid``);
2. ``POST`` na ``action`` formuláře jako PrimeFaces Ajax
   (``javax.faces.source=shopForm_ladeAbPreiseRC``, ``partial.render`` =
   id prvků s třídou ``updatableForAbPreis``) – stejné HTML doplněné o ceny:
   „Tickets from CHF 26.50“, u úsporných „Supersaver ticket available CHF 23.40“.

Dál (výběr jízdenky) nejdeme – tlačítko nákupu chrání reCAPTCHA.

**Jaká je to cena:** SBB anonymnímu návštěvníkovi ukazuje ceny **s polovičním
předplatným Halbtax** (Curych → Bern „od CHF 26.50“, plné jízdné je kolem
CHF 53–56). Halbtax je přesně 50 %, proto: dospělý bez Halbtaxu = 2 × cena
„od“, dítě 6–15 let = cena „od“, do 6 let zdarma. U úsporných jízdenek
(Sparbillett) platí Halbtax také polovinu, takže přepočet sedí i tam. Spoj
nese poznámku s původní cenou s Halbtaxem.

Jízdní řád (mezizastávky, nástupiště) bere **transport.opendata.ch**
(``/v1/connections``, komunitní API nad daty SBB bez klíče); ceny se
k nim přiřadí podle času odjezdu a příjezdu.

Na co si dát pozor:

* Ceny se načítají ze stránky na 5–6 spojů od zadaného času; pozdější
  spoje bez ceny se neukazují (jako u ostatních dopravců s cenou spoj po
  spoji). Když se ceny nenačtou vůbec, zůstane jízdní řád s poznámkou.
* Stránka je JSF – když se změní id prvků (``shopForm_…``), ceny se
  nenačtou a spoje zůstanou jen s jízdním řádem (poznámka).

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Švýcarsku.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from .. import geo
from decimal import Decimal

from ..errors import ProviderError, StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://transport.opendata.ch/v1"
#: Česká jména → jak je zná opendata.ch.
ALIASES = {"curych": "Zürich HB", "zurich": "Zürich HB", "zeneva": "Genève", "geneva": "Genève",
           "basilej": "Basel SBB", "basel": "Basel SBB", "bern": "Bern", "luzern": "Luzern", "lucern": "Luzern"}
WEB = "https://www.sbb.ch/"
TIMETABLE = WEB + "en/buying/pages/fahrplan/fahrplan.xhtml"
PRICE_SOURCE = "shopForm_ladeAbPreiseRC"
CHILD_FREE, CHILD_MAX = 6, 15
MODES = {"B": Mode.BUS, "BUS": Mode.BUS, "T": Mode.TRAM, "TRAM": Mode.TRAM, "BAT": Mode.FERRY, "FUN": Mode.TRAIN}


@PROVIDERS.register()
class SbbProvider(HttpProvider):
    name = "SBB"
    label = "SBB – Švýcarské dráhy (ceny ze sbb.ch přepočtené z Halbtaxu)"
    provides_stops = True
    currency = "CHF"
    countries = ("CH",)
    international_only = True
    hack_legs = False
    referer = None
    min_interval = 0.4

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "CH" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Švýcarsku")
        from ..normalize import normalize

        local = query.departure.astimezone(DEFAULT_TZ)
        data = await self.client.get_json(f"{API}/connections", params={
            "from": ALIASES.get(normalize(query.origin), query.origin),
            "to": ALIASES.get(normalize(query.destination), query.destination),
            "date": local.date().isoformat(), "time": local.strftime("%H:%M"), "limit": 6})
        connections = (data or {}).get("connections") or []
        if not connections:
            raise StationNotFoundError(self.name, "spojení nenalezeno")
        journeys = []
        for conn in connections:
            journey = to_journey(conn)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        try:
            prices = await self._prices(query, journeys)
        except ProviderError as exc:
            self.note(f"ceny SBB se nepodařilo načíst: {exc.message}")
            prices = []
        for journey in journeys:
            self.apply_price(journey, prices, query)
        if prices:      # stránka dá ceny k 5–6 spojům – spoje bez ceny za nimi neukazujeme
            journeys = [j for j in journeys if j.fare_status is FareStatus.PRICED]
        return journeys

    async def _prices(self, query: SearchQuery, journeys: list[Journey]) -> list[tuple[str, str, Decimal, bool]]:
        """Ceny „od“ ze starého jízdního řádu sbb.ch (PrimeFaces Ajax)."""
        if not journeys:
            return []
        local = query.departure.astimezone(DEFAULT_TZ)
        # švýcarská jména z jízdního řádu – české („Curych“) by sbb.ch spletl (Chur)
        params = {"von": journeys[0].legs[0].origin, "nach": journeys[0].legs[-1].destination,
                  "datum": local.strftime("%d.%m.%Y"),
                  "zeit": local.strftime("%H:%M"), "an": "false", "suche": "true"}
        page = await self.client.get_text(TIMETABLE, params=params, referer=WEB)
        fields, action, render = parse_form(page)
        if not action:
            return parse_prices(page)
        data = dict(fields)
        data.update({"javax.faces.partial.ajax": "true", "javax.faces.source": PRICE_SOURCE,
                     "javax.faces.partial.execute": PRICE_SOURCE, "javax.faces.partial.render": " ".join(render),
                     PRICE_SOURCE: PRICE_SOURCE, "shopForm": "shopForm"})
        response = await self.client.request("POST", WEB.rstrip("/") + action, data=data, referer=TIMETABLE,
                                             expect_json=False,
                                             headers={"Faces-Request": "partial/ajax",
                                                      "X-Requested-With": "XMLHttpRequest", "Origin": WEB.rstrip("/"),
                                                      # bez hlaviček XHR vrátí stránku bez cen
                                                      "Accept": "application/xml, text/xml, */*; q=0.01",
                                                      "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"})
        return parse_prices(response.text)

    def apply_price(self, journey: Journey, prices: list[tuple[str, str, Decimal, bool]],
                    query: SearchQuery) -> None:
        dep = journey.departure.strftime("%H:%M")
        arr = journey.arrival.strftime("%H:%M")
        match = next((p for p in prices if p[0] == dep and p[1] in ("", arr)), None)
        if match is None:
            return
        _, _, halbtax, saver = match
        total, tariff = group_price(halbtax, query.passengers, saver)
        journey.fares = [self.make_fare(total, "CHF", label="SBB", tariff=tariff)]
        journey.fare_status = FareStatus.PRICED
        journey.notes[:] = [f"SBB uvádí cenu s Halbtaxem od CHF {halbtax:.2f}; bez předplatného platí dospělý "
                            "dvojnásobek (tak je cena spočítaná)."]


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value))
    except ValueError:
        return None


def to_journey(conn: dict[str, Any]) -> Journey | None:
    legs = []
    for section in conn.get("sections") or []:
        journey = section.get("journey")
        if not journey:
            continue            # pěší přesun
        dep, arr = _time((section.get("departure") or {}).get("departure")), \
            _time((section.get("arrival") or {}).get("arrival"))
        if dep is None or arr is None:
            continue
        stops = []
        for s in journey.get("passList") or []:
            name = str((s.get("station") or {}).get("name") or "")
            if name:
                stops.append(Stop(name=name, arrival=_time(s.get("arrival")), departure=_time(s.get("departure")),
                                  platform=s.get("platform") or None))
        origin = str((section["departure"].get("station") or {}).get("name") or "")
        destination = str((section["arrival"].get("station") or {}).get("name") or "")
        category = str(journey.get("category") or "")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=MODES.get(category.upper(), Mode.TRAIN), carrier=str(journey.get("operator") or "SBB"),
                        line=f"{category} {journey.get('number') or ''}".strip(), stops=stops))
    if not legs:
        return None
    result = Journey(provider="SBB", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link="https://www.sbb.ch/")
    result.notes.append("Cenu SBB se nepodařilo načíst – ověřte ji na sbb.ch.")
    return result


# ---- ceny ze starého jízdního řádu (bez sítě, kvůli testům) ------------------------
def parse_form(page: str) -> tuple[dict[str, str], str, list[str]]:
    """Pole formuláře ``shopForm``, jeho ``action`` a id prvků, kam se načtou ceny."""
    import html as htmlmod

    form = re.search(r'<form id="shopForm".*?</form>', page, re.S)
    if not form:
        return {}, "", []
    action = re.search(r'action="([^"]+)"', form.group(0))
    fields = {}
    for tag in re.findall(r"<input[^>]*>", form.group(0)):
        name = re.search(r'name="([^"]+)"', tag)
        kind = re.search(r'type="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name and (not kind or kind.group(1) in ("hidden", "text")):
            fields[name.group(1)] = htmlmod.unescape(value.group(1)) if value else ""
    render = re.findall(r'<[^>]*id="([^"]+)"[^>]*class="[^"]*updatableForAbPreis', page)
    render += re.findall(r'<[^>]*class="[^"]*updatableForAbPreis[^"]*"[^>]*id="([^"]+)"', page)
    return fields, htmlmod.unescape(action.group(1)) if action else "", list(dict.fromkeys(render))


def parse_prices(page: str) -> list[tuple[str, str, Decimal, bool]]:
    """(odjezd, příjezd, cena „od“ v CHF s Halbtaxem, úsporná jízdenka) pro každý spoj."""
    import html as htmlmod

    text = htmlmod.unescape(page)
    rows: dict[str, dict] = {}
    marks = list(re.finditer(r'id="shopForm_aktuelleVerbindungen_(\d+)_(verbindung|leistungOfferierenWrapper)"',
                             text))
    for k, m in enumerate(marks):
        chunk = text[m.end(): marks[k + 1].start() if k + 1 < len(marks) else m.end() + 8000]
        plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", chunk))
        row = rows.setdefault(m.group(1), {})
        if m.group(2) == "verbindung":
            dep = re.search(r"Departure ?(\d{2}:\d{2})", plain)
            arr = re.search(r"Arrival ?(\d{2}:\d{2})", plain)
            row["dep"], row["arr"] = (dep.group(1) if dep else ""), (arr.group(1) if arr else "")
        else:
            price = re.search(r"from CHF (\d+(?:\.\d{2})?)", plain)
            if price:
                row["price"] = Decimal(price.group(1))
                row["saver"] = "Supersaver" in plain
    return [(r.get("dep", ""), r.get("arr", ""), r["price"], r.get("saver", False))
            for r in rows.values() if r.get("dep") and r.get("price")]


def group_price(halbtax: Decimal, passengers, saver: bool) -> tuple[Decimal, str]:
    """Cena skupiny bez předplatného: dospělý 2× cena s Halbtaxem, dítě 6–15 jako s Halbtaxem."""
    adults = sum(1 for p in passengers if p.age > CHILD_MAX)
    children = sum(1 for p in passengers if CHILD_FREE <= p.age <= CHILD_MAX)
    if not adults and not children:
        adults = 1
    total = halbtax * 2 * adults + halbtax * children
    parts = ["2. třída", "úsporná jízdenka" if saver else "běžná jízdenka", "bez Halbtaxu"]
    if children:
        parts.append("děti 6–15 let −50 %")
    if any(p.age < CHILD_FREE for p in passengers):
        parts.append("děti do 6 let zdarma")
    return total, ", ".join(parts)
