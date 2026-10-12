"""CP – Portugalské dráhy (Comboios de Portugal, www.cp.pt).

Web www.cp.pt (React) si při startu stáhne ``/fe-config.json`` – adresy
API brány ``api-gateway.cp.pt`` a klíče pro web (``travelApiKey``, ``xcck``,
``xccs``). Ty posíláme stejně jako web (hlavičky ``X-Api-Key``,
``x-cp-connect-id``, ``x-cp-connect-secret``); konfiguraci čteme jednou za
den, takže výměnu klíčů program přežije.

* ``GET travel-api/stations`` – všechny stanice (``code`` „94-30007“ =
  Lisboa Santa Apolónia, ``designation``);
* ``POST travel-api/journeys`` ``{"departureStationCode", "arrivalStationCode",
  "travelDate", "classes": [2], "quantities": [{"quantity": n, "type": 1}],
  "timeLimit": {"startTime": "06:00", …}, …}`` – spoje celého dne se
  zastávkami (``travelSections[].trainStops``) **i s cenou**
  (``basePrices``: ``priceType`` 1 = za osobu, 100 = za všech ``n``).

Cestující: plné jízdné od 13 let; děti 4–12 let mají u CP 50 %, do 4 let
zdarma – dětskou cenu dopočítáme z ceny za osobu (API ji neumí bez dalšího
kroku nákupu). Senioři 65+ mají u CP slevu 50 % jen s dokladem – počítáme
je jako dospělé (horní odhad).

Na co si dát pozor:

* Hledá se mezi stanicemi, ne městy – „Lisboa“ a „Porto“ mají víc nádraží;
  bereme hlavní (Santa Apolónia, Campanhã), viz ``MAIN``.
* ``saleableOnline: false`` = spoj nejde koupit online (cenu přesto má).
* Časy jsou místní (Europe/Lisbon, o hodinu méně než u nás).

Ptáme se jen na cesty po Portugalsku.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

WEB = "https://www.cp.pt"
CONFIG_TTL = 24 * 3600
STATIONS_TTL = 7 * 24 * 3600
TZ = ZoneInfo("Europe/Lisbon")
#: Město → hlavní nádraží.
MAIN = {"lisboa": "Lisboa Santa Apolonia", "lisabon": "Lisboa Santa Apolonia", "lisbon": "Lisboa Santa Apolonia",
        "porto": "Porto Campanha", "coimbra": "Coimbra-B", "faro": "Faro", "braga": "Braga", "aveiro": "Aveiro"}
SERVICES = {"AP": "Alfa Pendular", "IC": "Intercidades", "IR": "Inter-Regional", "R": "Regional", "U": "Urbano"}
CHILD_FREE, CHILD_MAX = 4, 12


@PROVIDERS.register()
class CpProvider(HttpProvider):
    name = "CP"
    label = "CP – Portugalské dráhy (jen po Portugalsku)"
    multi_passenger = True
    currency = "EUR"
    countries = ("PT",)
    international_only = True
    referer = WEB + "/"
    min_interval = 0.3

    async def config(self) -> dict[str, str]:
        cached = self.cache.get(self.name, "config")
        if cached:
            return cached
        # jako text: konfigurace obsahuje „recaptchaSiteKey“ a detekce blokace by ji odmítla
        import json

        cfg = json.loads(await self.client.get_text(f"{WEB}/fe-config.json", referer=self.referer))
        if not (cfg or {}).get("travelApiUrl"):
            raise TransientProviderError(self.name, "konfigurace webu CP nemá adresu API")
        self.cache.set(self.name, "config", cfg, ttl=CONFIG_TTL)
        return cfg

    async def _headers(self) -> dict[str, str]:
        cfg = await self.config()
        return {"X-Api-Key": str(cfg.get("travelApiKey") or ""), "x-cp-connect-id": str(cfg.get("xcck") or ""),
                "x-cp-connect-secret": str(cfg.get("xccs") or ""), "Origin": WEB}

    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        cfg = await self.config()
        data = await self.client.get_json(f"{cfg['travelApiUrl']}/stations", referer=self.referer,
                                          headers=await self._headers())
        items = [{"code": s["code"], "name": s["designation"]} for s in data or [] if s.get("code")]
        self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, Any]:
        found = pick_station(place, await self.stations())
        if found is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} CP nezná")
        return found

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if any(c not in (None, "PT") for c in known):
            raise StationNotFoundError(self.name, "CP – jen cesty po Portugalsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        local = query.departure.astimezone(TZ)
        paying = max(1, sum(1 for p in query.passengers if p.age >= CHILD_FREE))
        cfg = await self.config()
        data = await self.client.post_json(f"{cfg['travelApiUrl']}/journeys", {
            "departureStationCode": origin["code"], "arrivalStationCode": destination["code"],
            "travelDate": local.date().isoformat(), "returnDate": None, "classes": [2], "configID": 200,
            "lang": "PT", "quantities": [{"quantity": 1, "type": 1}], "searchType": 3, "services": [],
            "timeLimit": {"startTime": local.strftime("%H:%M"), "endTime": "23:59", "limitType": 0},
            "returnTimeLimit": {"startTime": "00:00", "endTime": "23:59", "limitType": 0},
            "saleableOnly": False, "username": "sivNetticket"}, referer=self.referer,
            headers=await self._headers())
        journeys = []
        for trip in (data or {}).get("outwardTrip") or []:
            journey = self.to_journey(trip, local.date(), query)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        if paying > 9:
            self.note("CP prodá online nejvýš 9 jízdenek najednou")
        return journeys

    def to_journey(self, trip: dict[str, Any], day: dt.date, query: SearchQuery) -> Journey | None:
        legs = parse_legs(trip, day)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=f"{WEB}/pt/comprar-bilhetes")
        each = person_price(trip)
        if each is not None:
            total, tariff = group_price(each, query.passengers)
            journey.fares = [self.make_fare(total, "EUR", label="CP", tariff=tariff)]
            journey.fare_status = FareStatus.PRICED
        if trip.get("saleableOnline") is False:
            journey.notes.append("Tento spoj CP online neprodává – jízdenku koupíte v pokladně.")
        return journey


def pick_station(place: str, stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = normalize(place)
    target = normalize(MAIN.get(wanted, place))
    for s in stations:
        if normalize(s["name"]) == target:
            return s
    starts = [s for s in stations if normalize(s["name"]).startswith(target)]
    if starts:
        return starts[0]
    idx = best_match(place, [s["name"] for s in stations])
    return stations[idx] if idx is not None else None


def person_price(trip: dict[str, Any]) -> Decimal | None:
    """Cena 2. třídy za jednoho dospělého (``priceType`` 1) v eurech."""
    for price in trip.get("basePrices") or []:
        if int(price.get("priceType") or 0) == 1 and str(price.get("travelClass")) == "2" \
                and price.get("centsValue") is not None:
            return Decimal(int(price["centsValue"])) / 100
    return None


def group_price(each: Decimal, passengers: tuple[Passenger, ...]) -> tuple[Decimal, str]:
    adults = sum(1 for p in passengers if p.age > CHILD_MAX) or (0 if passengers else 1)
    children = sum(1 for p in passengers if CHILD_FREE <= p.age <= CHILD_MAX)
    if not adults and not children:
        adults = 1
    total = each * adults + (each / 2).quantize(Decimal("0.01"), ROUND_HALF_UP) * children
    parts = ["2. třída"]
    if children:
        parts.append("děti 4–12 let −50 %")
    if any(p.age < CHILD_FREE for p in passengers):
        parts.append("děti do 4 let zdarma")
    return total, ", ".join(parts)


def _at(day: dt.date, hhmm: str, after: dt.datetime | None) -> dt.datetime | None:
    try:
        hour, minute = (int(x) for x in hhmm.split(":")[:2])
    except (ValueError, AttributeError):
        return None
    value = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    while after is not None and value < after:
        value += dt.timedelta(days=1)
    return value


def parse_legs(trip: dict[str, Any], day: dt.date) -> list[Leg]:
    legs = []
    prev: dt.datetime | None = _at(day, str(trip.get("departureTime") or ""), None)
    if prev is None:
        return []
    prev -= dt.timedelta(minutes=1)
    for section in trip.get("travelSections") or []:
        stops = []
        raw = section.get("trainStops") or []
        for i, s in enumerate(raw):
            arrival = None if i == 0 else _at(day, str(s.get("arrival") or ""), prev)
            departure = None if i == len(raw) - 1 else _at(day, str(s.get("departure") or ""), arrival or prev)
            prev = departure or arrival or prev
            stops.append(Stop(name=str((s.get("station") or {}).get("designation") or ""),
                              arrival=arrival, departure=departure, platform=s.get("platform") or None))
        if len(stops) < 2 or stops[0].departure is None or stops[-1].arrival is None:
            continue
        service = section.get("serviceCode") or {}
        code = str(service.get("code") or "")
        legs.append(Leg(origin=stops[0].name, destination=stops[-1].name, departure=stops[0].departure,
                        arrival=stops[-1].arrival, mode=Mode.TRAIN, carrier="CP",
                        line=f"{code} {section.get('trainNumber') or ''}".strip(), stops=stops))
    return legs
