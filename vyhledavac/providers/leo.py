"""Leo Express (leoexpress.com) – vlaky Praha – Ostrava – Kraków / Košice a autobusy.

Web Leo Express volá veřejné GraphQL API ``graph.leoexpress.com/le``:

* ``stations`` – seznam ~190 stanic, které Leo Express obsluhuje (s městem,
  ``parent_station.short`` = „PRAHA“);
* ``searchResults`` (dotaz ``searchConnections``) – spoje s cenou pro skupinu
  podle tříd (Economy, Economy Plus, Business); bereme nejlevnější třídu.

Ptáme se **jen na trasách, kde Leo Express jezdí**: obě místa musí být
v jeho seznamu stanic (Praha, Pardubice, Olomouc, Ostrava, Bohumín,
Kraków, Žilina, Košice …). Brno, Plzeň a další dotaz vůbec nedostanou.

Tarify (``tariffsTree``): dítě 0–5 ``child``, junior 6–17 ``child6``,
student 18–25 ``student``, senior 65+ ``senior``, jinak ``adult``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

GRAPH = "https://graph.leoexpress.com/le"
WEB = "https://www.leoexpress.com/cs"
STATIONS_TTL = 7 * 24 * 3600
STATIONS_QUERY = """query allStations($locale: String) { stations(locale: $locale) {
  id name name_master country type parent_station { short name } } }"""
SEARCH_QUERY = """query searchConnections($from: String, $to: String, $date: String,
  $persons: [RateArgument], $services: [ServiceArgument], $currency: String, $timestamp: Int,
  $locale: String, $token: String) {
  searchResults(from: $from, to: $to, date: $date, persons: $persons, services: $services,
    currency: $currency, timestamp: $timestamp, locale: $locale, token: $token) {
    return_code
    connections {
      departure arrival currency
      origin { id name } destination { id name }
      lines { line_id type carrier_id departure arrival origin destination
              line_stops { station { name } } }
      classes { id short name }
      class_info { record_id capacity total }
    }
  }
}"""


def tariff(passenger: Passenger) -> str:
    if passenger.age < 6:
        return "child"
    if passenger.age < 18:
        return "child6"
    if passenger.age >= 65:
        return "senior"
    if passenger.is_student:
        return "student"
    return "adult"


@PROVIDERS.register()
class LeoExpressProvider(HttpProvider):
    name = "LeoExpress"
    label = "Leo Express (Praha – Ostrava – Kraków / Košice)"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ", "SK", "PL")
    czech_names = True
    referer = WEB
    min_interval = 0.3

    async def _graph(self, query: str, variables: dict[str, Any]) -> Any:
        data = await self.client.post_json(GRAPH, {"query": query, "variables": variables},
                                           referer=WEB, headers={"Origin": "https://www.leoexpress.com",
                                                             "Accept-Encoding": "gzip, deflate"})
        return (data or {}).get("data") or {}

    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        data = await self._graph(STATIONS_QUERY, {"locale": "cs"})
        items = [s for s in data.get("stations") or [] if s.get("id")]
        if items:
            self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> tuple[str, str]:
        """Kód místa pro hledání: město („PRAHA“) nebo ID stanice."""
        code = pick_station(place, await self.stations())
        if code is None:
            raise StationNotFoundError(self.name, f"Leo Express do {place!r} nejezdí")
        return code

    async def search(self, query: SearchQuery) -> list[Journey]:
        (from_code, _), (to_code, _) = await self.resolve(query.origin), await self.resolve(query.destination)
        when = query.departure.astimezone(DEFAULT_TZ)
        data = await self._graph(SEARCH_QUERY, {
            "from": from_code, "to": to_code, "date": when.strftime("%d.%m.%Y"),
            "persons": [{"name": tariff(p), "cards": []} for p in query.passengers],
            "services": [], "currency": "CZK", "timestamp": int(time.time()),
            "locale": "cs", "token": None})
        result = data.get("searchResults") or {}
        journeys = []
        for raw in result.get("connections") or []:
            journey = self._to_journey(raw, from_code, to_code, when)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def _to_journey(self, raw: dict[str, Any], from_code: str, to_code: str,
                    when: dt.datetime) -> Journey | None:
        legs = []
        for line in raw.get("lines") or []:
            dep, arr = _ts(line.get("departure")), _ts(line.get("arrival"))
            if dep is None or arr is None:
                continue
            stops = [Stop(name=str((s.get("station") or {}).get("name") or ""))
                     for s in line.get("line_stops") or []]
            stops = [s for s in stops if s.name]
            if stops:
                stops[0].departure, stops[-1].arrival = dep, arr
            line_id = str(line.get("line_id") or "")
            label = re.sub(r"-[A-Z]{2}$", "", line_id)          # „LE411-PL“ -> „LE411“
            legs.append(Leg(
                origin=stops[0].name if stops else "", destination=stops[-1].name if stops else "",
                departure=dep, arrival=arr,
                mode=Mode.BUS if line.get("type") == "bus" else Mode.TRAIN,
                carrier="Leo Express", line=label.replace("LE", "LE ", 1) if label.startswith("LE") else label,
                stops=stops, reservation_required=True))
        if not legs:
            return None
        origin = (raw.get("origin") or {}).get("name")
        destination = (raw.get("destination") or {}).get("name")
        if origin:
            legs[0].origin = origin
        if destination:
            legs[-1].destination = destination
        price, cls, seats = cheapest_class(raw)
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=f"{WEB}/vysledky-vyhledavani?from={from_code}&to={to_code}"
                                    f"&date={when:%Y-%m-%d}")
        if price is not None:
            journey.fares = [self.make_fare(price, str(raw.get("currency") or "CZK"),
                                            label="Leo Express", tariff=cls, seats_left=seats)]
            journey.fare_status = FareStatus.PRICED
        elif raw.get("class_info"):
            journey.fare_status = FareStatus.SOLD_OUT
        return journey


def pick_station(place: str, stations: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Město (všechny jeho stanice) nebo konkrétní stanice Leo Expressu."""
    wanted = normalize(place)
    for s in stations:
        if normalize(s.get("name") or "") == wanted or normalize(s.get("name_master") or "") == wanted:
            return str(s["id"]), str(s["name"])
    town = normalize(place.split(",")[0]).split(" ")[0] if place else ""
    for s in stations:
        parent = s.get("parent_station") or {}
        if parent.get("short") and normalize(parent.get("name") or "") in (wanted, town):
            return str(parent["short"]), str(parent.get("name") or place)
    candidates = []
    for s in stations:
        for name in (normalize(s.get("name") or ""), normalize(s.get("name_master") or "")):
            if name.startswith(wanted + " "):
                candidates.append(s)
                break
    if candidates:      # „Kraków“ -> „Kraków Główny“ (vlak má přednost před autobusem)
        best = min(candidates, key=lambda s: (s.get("type") != "train", len(s.get("name") or "")))
        return str(best["id"]), str(best["name"])
    return None


def cheapest_class(raw: dict[str, Any]) -> tuple[Decimal | None, str, int | None]:
    names = {str(c.get("id")): str(c.get("name") or c.get("short") or "") for c in raw.get("classes") or []}
    best = None
    for info in raw.get("class_info") or []:
        total, capacity = info.get("total"), info.get("capacity")
        if total is None or (capacity is not None and capacity <= 0):
            continue
        if best is None or Decimal(str(total)) < best[0]:
            best = (Decimal(str(total)), names.get(str(info.get("record_id")), ""), capacity)
    return best if best is not None else (None, "", None)


def _ts(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value), tz=DEFAULT_TZ)
    except (TypeError, ValueError):
        return None
