"""SŽ – Slovinské dráhy (Slovenske železnice, e-shop eshop.sz.si).

Web ``potniski.sz.si`` je za Cloudflare (výsledky vyhledávání chrání
interaktivní výzva Turnstile – ta se neobchází). Prodejní e-shop
``eshop.sz.si`` (ASP.NET MVC) ale odpovídá obyčejným HTTP požadavkům
a vrací jízdní řád i cenu pro celou skupinu v jednom HTML:

* ``POST /Home/TravelAutocomplete`` (formulář ``SearchText``, ``DropdownId=1``)
  – našeptávač stanic, HTML ``<li data-value="136755" data-text="Ljubljana"
  data-abroad="False">``; ID nejsou kódy UIC, ale interní čísla e-shopu;
* ``POST /Home/FilterSingle`` – formulář jednosměrné jízdenky: datum
  (``DepartureDate=14.10.2026``, vrací celý den), ``TravelFromId``/
  ``TravelToId``, počty cestujících ``NumOfPassengers[0..2]`` (dospělí,
  děti 6–14 let, děti do 6 let), ``SeatClass=SecondClass`` a ``IcMcVc``.
  Odpověď je HTML (~2 MB) s řádkem na spoj (``filter-table-main``: časy,
  vlaky, cena „36,00 EUR“) a rozpisem úseků mezi zastávkami
  (``filter-table-detail``: „Vstopna postaja“, „Vlak: LP 2251“,
  „Izhodna postaja“).

Postup: dvě hledání na den – ``IcMcVc=false`` (regionální vlaky LP/RG
a náhradní autobusy) a ``IcMcVc=true`` (spoje s vlakem IC/ICS/EC/MV, cena
včetně příplatku) – a jejich sloučení.

Na co si dát pozor:

* **Jen vnitrostátní jízdenky.** E-shop zná jen slovinské stanice; na
  mezinárodní cesty (Ljubljana → Zagreb, Villach) se neptáme vůbec.
* ``IcMcVc`` není „i IC vlaky“, ale „jen spoje s IC vlakem“ – bez druhého
  dotazu by chyběly buď rychlé, nebo regionální spoje.
* Cena je za celou skupinu: dospělí od 15 let plné jízdné, děti 6–14 let
  poloviční, do 6 let zdarma (vlastní kolonka, cena 0). Slevy (senioři,
  mládež, IZLETka) neuplatňujeme.
* U spojů s výlukou/náhradní dopravou e-shop píše upozornění – vlak
  „BUS 60606“ je náhradní autobus (``Mode.BUS``).

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Slovinsku
a žádný není známý jako zahraniční.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html as htmllib
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

ESHOP = "https://eshop.sz.si"
WEB = ESHOP + "/"
PLACE_TTL = 30 * 24 * 3600
#: České a německé názvy slovinských měst, které e-shop nezná.
EXONYMS = {"lublan": "Ljubljana", "laibach": "Ljubljana", "marburg": "Maribor", "cilli": "Celje",
           "koprivnik": "Koper"}

_ITEM = re.compile(r'<li data-value="(\d+)" data-text="([^"]*)" data-abroad="(\w+)"')
_ROW = re.compile(r'<div class="filter-table-main ')
_TOKEN = re.compile(
    r'<b class="go-medium">(?P<time>\d{1,2}:\d{2})</b>\s*<br\s*/?>\s*<span>(?P<date>[\d. ]+)</span>'
    r'|(?P<kind>Vstopna|Izhodna) postaja:\s*<b class="go-medium">(?P<station>[^<]+)</b>'
    r'|Vlak:\s*<b class="go-medium">(?P<train>[^<]+)</b>')
_PRICE = re.compile(r'([\d.]+,\d{2})\s*EUR')
_NOTICE = re.compile(r'<b class="go-medium" style="margin-left[^"]*">([^<]+)</b>')


@PROVIDERS.register()
class SzProvider(HttpProvider):
    name = "SŽ"
    label = "SŽ – Slovinské dráhy (jen cesty po Slovinsku)"
    multi_passenger = True
    currency = "EUR"
    countries = ("SI",)
    international_only = True
    referer = WEB
    min_interval = 0.5

    async def _post(self, action: str, form: dict[str, str]) -> str:
        # Bez balíčku brotli by httpx odpověď „br“ nerozbalil – e-shop ji jinak posílá.
        response = await self.client.request("POST", f"{ESHOP}/Home/{action}", data=form, referer=WEB,
                                             expect_json=False,
                                             headers={"Origin": ESHOP, "Accept-Encoding": "gzip, deflate"})
        return response.text

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        wanted = EXONYMS.get(normalize(place).split(" ")[0], place) if place else place
        items: list[tuple[str, str, str]] = []
        for text in dict.fromkeys([wanted, wanted.split(",")[0].strip(), wanted.split(" ")[0]]):
            if not text:
                continue
            page = await self._post("TravelAutocomplete", {"SearchText": text, "DropdownId": "1"})
            items = parse_stations(page)
            if items:
                break
        if not items:
            raise StationNotFoundError(self.name, f"stanici {place!r} e-shop SŽ nezná")
        chosen = pick_station(wanted, items)
        out = {"id": chosen[0], "name": chosen[1], "abroad": chosen[2]}
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "SI" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Slovinsku")
        if any(c not in (None, "SI") for c in known):
            raise StationNotFoundError(self.name, "e-shop SŽ prodává jen jízdenky po Slovinsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if origin["abroad"] and destination["abroad"]:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Slovinsku")
        start = query.departure.astimezone(DEFAULT_TZ)
        days = [start.date()]
        end = query.window_end.astimezone(DEFAULT_TZ)
        if end.date() > start.date() and end.hour >= 4:
            days.append(end.date())
        calls = [self._day(origin, destination, day, ic, query) for day in days for ic in (False, True)]
        results = await asyncio.gather(*calls, return_exceptions=True)
        journeys: dict[tuple, Journey] = {}
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == len(results):
            raise errors[0]
        if errors:
            self.note(f"část spojů SŽ se nepodařilo načíst: {getattr(errors[0], 'message', errors[0])}")
        for result in results:
            if isinstance(result, BaseException):
                continue
            for journey in result:
                if query.departure <= journey.departure <= query.window_end:
                    journeys.setdefault(journey.signature(), journey)
        return sorted(journeys.values(), key=lambda j: j.departure)

    async def _day(self, origin: dict[str, Any], destination: dict[str, Any], day: dt.date, ic: bool,
                   query: SearchQuery) -> list[Journey]:
        adults, children, infants = passenger_counts(query)
        form = {
            "FilterModel.FilterType": "0", "SubSelect": "0", "TicketDirection": "OneWay",
            "DepartureDate": day.strftime("%d.%m.%Y"),
            "NumOfPassengers[0]": str(adults), "NumOfPassengers[1]": str(children),
            "NumOfPassengers[2]": str(infants),
            "TravelFrom": origin["name"], "TravelFromId": str(origin["id"]),
            "TravelTo": destination["name"], "TravelToId": str(destination["id"]),
            "IcMcVc": "true" if ic else "false", "SeatClass": "SecondClass", "NoTransfer": "false",
            "FromPotniski": "",
        }
        journeys = []
        for row in parse_results(await self._post("FilterSingle", form)):
            journeys.append(self.to_journey(row, ic, children))
        return journeys

    def to_journey(self, row: dict[str, Any], ic: bool, children: int = 0) -> Journey:
        journey = Journey(provider=self.name, legs=row["legs"], fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        journey.notes.extend(f"Upozornění SŽ: {n}" for n in row["notices"])
        if row["price"] is not None:
            tariff = "2. razred" + (", příplatek IC/ICS/EC" if ic else "")
            if children:
                tariff += ", děti 6–14 let poloviční"
            journey.fares = [self.make_fare(row["price"], "EUR", label="SŽ e-vozovnica", tariff=tariff,
                                            deep_link=WEB)]
            journey.fare_status = FareStatus.PRICED
        return journey


def passenger_counts(query: SearchQuery) -> tuple[int, int, int]:
    adults = sum(1 for p in query.passengers if p.age >= 15)
    children = sum(1 for p in query.passengers if 6 <= p.age < 15)
    infants = sum(1 for p in query.passengers if p.age < 6)
    if adults + children == 0:
        adults = 1
    return adults, children, infants


def parse_stations(page: str) -> list[tuple[str, str, bool]]:
    out, seen = [], set()
    for sid, name, abroad in _ITEM.findall(page or ""):
        if sid not in seen:
            seen.add(sid)
            out.append((sid, htmllib.unescape(name).strip(), abroad.lower() == "true"))
    return out


def pick_station(place: str, items: list[tuple[str, str, bool]]) -> tuple[str, str, bool]:
    """Přesná shoda, pak hlavní stanice města (krátký název), pak nejpodobnější."""
    wanted = normalize(place)
    for item in items:
        if normalize(item[1]) == wanted:
            return item
    first = wanted.split(" ")[0]
    same_city = [i for i in items if normalize(i[1]).split(" ")[0] == first]
    if same_city and wanted == first:
        return min(same_city, key=lambda i: len(i[1]))
    idx = best_match(place, [i[1] for i in items])
    return items[idx if idx is not None else 0]


def parse_results(page: str) -> list[dict[str, Any]]:
    """HTML výsledků → ``[{"legs": [Leg], "price": Decimal|None, "notices": [str]}]``."""
    rows = []
    for block in _ROW.split(page or "")[1:]:
        cut = block.find("filter-table-detail")
        head, detail = (block[:cut], block[cut:]) if cut >= 0 else (block, "")
        legs = parse_legs(detail)
        if not legs:
            continue
        price = _PRICE.search(head)
        amount = Decimal(price.group(1).replace(".", "").replace(",", ".")) if price else None
        notices = [re.sub(r"\s+", " ", htmllib.unescape(n)).strip() for n in _NOTICE.findall(head)]
        rows.append({"legs": legs, "price": amount, "notices": list(dict.fromkeys(n for n in notices if n))})
    return rows


def parse_legs(detail: str) -> list[Leg]:
    """Úseky mezi sousedními zastávkami sloučí do jízd jednotlivými vlaky."""
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    last_time: dt.datetime | None = None
    for m in _TOKEN.finditer(detail or ""):
        if m.group("time"):
            last_time = _when(m.group("time"), m.group("date"))
        elif m.group("kind") == "Vstopna":
            current = {"from": _clean(m.group("station")), "dep": last_time}
        elif m.group("kind") == "Izhodna" and current:
            current.update(to=_clean(m.group("station")), arr=last_time)
            if current.get("dep") and current.get("arr") and current.get("train"):
                segments.append(current)
            current = {}
        elif m.group("train") and current:
            current["train"] = _clean(m.group("train"))
    legs: list[Leg] = []
    group: list[dict[str, Any]] = []
    for seg in segments + [None]:
        if group and (seg is None or seg["train"] != group[-1]["train"] or seg["from"] != group[-1]["to"]):
            legs.append(_leg(group))
            group = []
        if seg is not None:
            group.append(seg)
    return legs


def _leg(group: list[dict[str, Any]]) -> Leg:
    first, last = group[0], group[-1]
    stops = [Stop(name=first["from"], departure=first["dep"])]
    for prev, nxt in zip(group, group[1:]):
        stops.append(Stop(name=prev["to"], arrival=prev["arr"], departure=nxt["dep"]))
    stops.append(Stop(name=last["to"], arrival=last["arr"]))
    train = first["train"]
    bus = train.upper().startswith(("BUS", "AV "))
    return Leg(origin=first["from"], destination=last["to"], departure=first["dep"], arrival=last["arr"],
               mode=Mode.BUS if bus else Mode.TRAIN, carrier="SŽ", line=train, stops=stops)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(text or "")).strip()


def _when(clock: str, date: str) -> dt.datetime | None:
    try:
        day = dt.datetime.strptime(re.sub(r"\s+", "", date), "%d.%m.%Y").date()
        hour, minute = (int(x) for x in clock.split(":"))
    except ValueError:
        return None
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=DEFAULT_TZ)
