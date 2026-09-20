"""FlixBus přes veřejné vyhledávací API.

Původní verze načítala flixbus.cz v Chromiu a pak procházela *všechny*
elementy DOMu JavaScriptem a hádala, který vypadá jako karta spoje. To se
rozbilo při každé změně webu a vracelo to duplicity.

FlixBus má přitom otevřené API na ``global.api.flixbus.com`` – to samé,
které volá jejich vlastní web. Vrací strukturovaný JSON s cenami, počtem
volných míst i přestupy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

from ..errors import StationNotFoundError
from ..models import (
    DEFAULT_TZ,
    DiscountCard,
    FareStatus,
    Journey,
    Leg,
    Mode,
    Passenger,
    SearchQuery,
    Stop,
)
from ..normalize import best_match
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

SEARCH_URL = "https://global.api.flixbus.com/search/service/v4/search"
AUTOCOMPLETE_URL = "https://global.api.flixbus.com/search/autocomplete/cities"
CITY_TTL = 30 * 24 * 3600


@PROVIDERS.register()
class FlixBusProvider(HttpProvider):
    name = "FlixBus"
    label = "FlixBus / FlixTrain (celá Evropa)"
    currency = "CZK"
    provides_prices = True
    provides_stops = False
    countries = ("CZ", "SK", "AT", "DE", "PL", "HU", "FR", "IT", "NL", "BE", "ES")
    referer = "https://www.flixbus.cz/"
    min_interval = 0.4

    def _passenger_products(self, passengers: tuple[Passenger, ...]) -> dict[str, int]:
        """Rozdělí cestující do kategorií FlixBusu.

        Slevové kategorie platí jen na vnitrostátních linkách některých trhů;
        když je API odmítne, ``search`` to pozná a zopakuje dotaz jako dospělé.
        """
        products = {"adult": 0, "children": 0, "bike_slot": 0}
        for passenger in passengers:
            if passenger.age < 15:
                products["children"] += 1
            else:
                products["adult"] += 1
        return {k: v for k, v in products.items() if v}

    async def resolve_city(self, place: str, country_hint: str | None = None) -> tuple[str, str]:
        """Najde ID města ve FlixBusu. Vrací ``(uuid, oficiální název)``."""
        key = f"city:{place.lower()}:{country_hint or ''}"
        cached = self.cache.get(self.name, key)
        if cached:
            return tuple(cached)  # type: ignore[return-value]

        params: dict[str, Any] = {"q": place, "lang": self.options.get("language", "cs")}
        if country_hint:
            params["country"] = country_hint

        data = await self.client.get_json(
            AUTOCOMPLETE_URL, params=params, referer=self.referer
        )
        if not isinstance(data, list) or not data:
            raise StationNotFoundError(self.name, f"město {place!r} nenalezeno")

        names = [str(item.get("name", "")) for item in data]
        idx = best_match(place, names, threshold=0.4)
        if idx is None:
            # Našeptávač řadí podle skóre – když se neshodneme, vezmeme jeho první.
            idx = 0
        chosen = data[idx]
        city_id = str(chosen.get("id") or "")
        if not city_id:
            raise StationNotFoundError(self.name, f"město {place!r} nemá ID")
        result = (city_id, str(chosen.get("name") or place))
        self.cache.set(self.name, key, list(result), ttl=CITY_TTL)
        return result

    async def search(self, query: SearchQuery) -> list[Journey]:
        (from_id, from_name), (to_id, to_name) = await asyncio.gather(
            self.resolve_city(query.origin), self.resolve_city(query.destination)
        )

        products = self._passenger_products(query.passengers)
        payload = await self._call(query, from_id, to_id, products)

        trips = (payload or {}).get("trips") or []
        journeys: list[Journey] = []
        for trip in trips:
            results = trip.get("results") or {}
            for raw in results.values():
                journey = self._to_journey(raw, payload, query, from_name, to_name)
                if journey is not None:
                    journeys.append(journey)
        return journeys

    async def _call(
        self, query: SearchQuery, from_id: str, to_id: str, products: dict[str, int]
    ) -> dict[str, Any]:
        params = {
            "from_city_id": from_id,
            "to_city_id": to_id,
            # API chce DD.MM.YYYY, ne ISO.
            "departure_date": query.departure.strftime("%d.%m.%Y"),
            "products": json.dumps(products, separators=(",", ":")),
            "currency": query.currency,
            "locale": self.options.get("language", "cs"),
            "search_by": "cities",
            "include_after_midnight_rides": 1,
        }
        return await self.client.get_json(SEARCH_URL, params=params, referer=self.referer)

    def _to_journey(
        self,
        raw: dict[str, Any],
        payload: dict[str, Any],
        query: SearchQuery,
        from_name: str,
        to_name: str,
    ) -> Journey | None:
        departure = _parse_flix_time(raw.get("departure"))
        arrival = _parse_flix_time(raw.get("arrival"))
        if departure is None or arrival is None:
            return None

        cities = (payload.get("cities") or {}) if isinstance(payload, dict) else {}
        stations = (payload.get("stations") or {}) if isinstance(payload, dict) else {}

        legs = self._build_legs(raw, stations, cities, departure, arrival,
                                from_name, to_name)

        status = FareStatus.PRICED
        fares = []
        price_block = raw.get("price") or {}
        total = price_block.get("total")
        if raw.get("status") in {"sold_out", "unavailable"} or raw.get("available", {}).get("seats") == 0:
            status = FareStatus.SOLD_OUT
        if total is not None:
            seats = (raw.get("available") or {}).get("seats")
            fares.append(
                self.make_fare(
                    Decimal(str(total)),
                    str(price_block.get("currency") or query.currency),
                    label="FlixBus",
                    bookable=status is FareStatus.PRICED,
                    seats_left=seats if isinstance(seats, int) else None,
                )
            )
        elif status is not FareStatus.SOLD_OUT:
            status = FareStatus.UNKNOWN

        return Journey(
            provider=self.name,
            legs=legs,
            fares=fares,
            fare_status=status,
            journey_id=str(raw.get("uid") or ""),
            deep_link=raw.get("provider_deeplink") or self.referer,
            raw={"result": raw},
        )

    def _build_legs(
        self,
        raw: dict[str, Any],
        stations: dict[str, Any],
        cities: dict[str, Any],
        departure: dt.datetime,
        arrival: dt.datetime,
        from_name: str,
        to_name: str,
    ) -> list[Leg]:
        """Přestupní spoj má více úseků v ``transfers``; přímý jeden."""

        def station_name(sid: Any, fallback: str) -> str:
            info = stations.get(str(sid)) or cities.get(str(sid)) or {}
            return str(info.get("name") or fallback)

        transfers = raw.get("transfers") or []
        legs: list[Leg] = []
        if transfers:
            for part in transfers:
                part_dep = _parse_flix_time(part.get("departure"))
                part_arr = _parse_flix_time(part.get("arrival"))
                if part_dep is None or part_arr is None:
                    continue
                origin = station_name(part.get("from_station_id"), from_name)
                destination = station_name(part.get("to_station_id"), to_name)
                legs.append(
                    Leg(
                        origin=origin,
                        destination=destination,
                        departure=part_dep,
                        arrival=part_arr,
                        mode=Mode.BUS,
                        carrier="FlixBus",
                        line=str(part.get("line_code") or "FlixBus"),
                        stops=[
                            Stop(name=origin, departure=part_dep),
                            Stop(name=destination, arrival=part_arr),
                        ],
                        reservation_required=True,
                    )
                )
        if not legs:
            origin = station_name(raw.get("departure", {}).get("station_id"), from_name)
            destination = station_name(raw.get("arrival", {}).get("station_id"), to_name)
            legs = [
                Leg(
                    origin=origin,
                    destination=destination,
                    departure=departure,
                    arrival=arrival,
                    mode=Mode.BUS,
                    carrier="FlixBus",
                    line=str(raw.get("line_code") or "FlixBus"),
                    stops=[
                        Stop(name=origin, departure=departure),
                        Stop(name=destination, arrival=arrival),
                    ],
                    reservation_required=True,
                )
            ]
        return legs


def _parse_flix_time(node: Any) -> dt.datetime | None:
    """FlixBus posílá čas jako objekt s ``date`` (ISO 8601 se zónou)."""
    if not node:
        return None
    if isinstance(node, dict):
        node = node.get("date") or node.get("datetime")
    if not isinstance(node, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(node.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TZ)
    return parsed
