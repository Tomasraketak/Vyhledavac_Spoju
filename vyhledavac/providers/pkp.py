"""PKP a polští dopravci přes Koleo (koleo.pl).

Koleo prodává jízdenky PKP Intercity, Polregio, Kolejí Śląskich a dalších
a zná i přeshraniční spoje (Praha/Ostrava – Kraków, Leo Express, RegioJet).
Jeho web volá veřejné JSON API ``koleo.pl/api/v2/main`` (hlavičky
``X-KOLEO-Version`` a ``X-KOLEO-Client``):

* ``GET /ls?q=`` – našeptávač stanic (skupina = celé město, např. „praha“);
* ``GET /api/v2/main/stations`` – seznam stanic se zemí (jednou za měsíc);
* ``GET /api/v2/main/connections`` – spoje;
* ``GET /api/v2/main/connections/{id}/price`` – cena (normální jízdné pro
  jednoho dospělého; polské slevy jsou jen pro polské nároky).

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Polsku – jinak je
dotaz zbytečný (šetří to limity Koleo).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

BASE = "https://koleo.pl"
API = f"{BASE}/api/v2/main"
HEADERS = {"X-KOLEO-Version": "1", "X-KOLEO-Client": "Nuxt-1", "Accept": "application/json"}
STATIONS_TTL = 30 * 24 * 3600
PLACE_TTL = 30 * 24 * 3600
MAX_PRICED = 10


@PROVIDERS.register()
class PkpProvider(HttpProvider):
    name = "PKP"
    label = "PKP a polští dopravci (Koleo)"
    currency = "PLN"
    countries = ("PL", "CZ", "SK", "DE")
    international_only = True          # po Česku nemá smysl
    referer = f"{BASE}/"
    min_interval = 0.35

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get_json(url, params=params, referer=self.referer, headers=HEADERS)

    async def countries_by_id(self) -> dict[int, str]:
        cached = self.cache.get(self.name, "countries")
        if cached:
            return {int(k): v for k, v in cached.items()}
        data = await self._get(f"{API}/stations")
        out = {int(s["id"]): str(s.get("country") or "") for s in data or [] if s.get("id")}
        self.cache.set(self.name, "countries", {str(k): v for k, v in out.items()}, ttl=STATIONS_TTL)
        return out

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self._get(f"{BASE}/ls", {"q": place, "language": "pl"})
        items = [s for s in (data or {}).get("stations") or [] if s.get("name_slug")]
        town = place.split(",")[0].strip()
        if not items and town != place:
            data = await self._get(f"{BASE}/ls", {"q": town, "language": "pl"})
            items = [s for s in (data or {}).get("stations") or [] if s.get("name_slug")]
        items = [s for s in items if s.get("type") in ("group", "railStopPlace")] or items
        if not items:
            raise StationNotFoundError(self.name, f"stanice {place!r} Koleo nezná")
        idx = best_match(place, [str(s.get("name") or "") for s in items], threshold=0.3)
        chosen = items[idx if idx is not None else 0]
        self.cache.set(self.name, key, chosen, ttl=PLACE_TTL)
        return chosen

    async def search(self, query: SearchQuery) -> list[Journey]:
        origin, destination = await asyncio.gather(self.resolve(query.origin),
                                                   self.resolve(query.destination))
        countries = await self.countries_by_id()
        if "Polska" not in (countries.get(int(origin["id"])), countries.get(int(destination["id"]))):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Polsku")
        data = await self._get(f"{API}/connections", {
            "query[date]": query.departure.astimezone(DEFAULT_TZ).strftime("%d-%m-%Y %H:%M:%S"),
            "query[start_station]": origin["name_slug"],
            "query[end_station]": destination["name_slug"],
            "query[only_direct]": "false", "query[only_purchasable]": "false"})
        names, brands = await asyncio.gather(self._names(), self._brands())
        journeys = []
        for raw in (data or {}).get("connections") or []:
            journey = to_journey(raw, names, brands)
            if journey is not None and journey.departure <= query.window_end:
                journeys.append(journey)
        priced = [j for j in journeys if j.raw.get("purchasable")][:MAX_PRICED]
        await asyncio.gather(*(self._price(j) for j in priced))
        if len(query.passengers) > 1 and priced:
            self.note("Koleo: cena je za jednoho dospělého (normální jízdné)")
        return journeys

    async def _names(self) -> dict[int, str]:
        cached = self.cache.get(self.name, "names")
        if cached:
            return {int(k): v for k, v in cached.items()}
        data = await self._get(f"{API}/stations")
        out = {int(s["id"]): str(s.get("name") or "") for s in data or [] if s.get("id")}
        self.cache.set(self.name, "names", {str(k): v for k, v in out.items()}, ttl=STATIONS_TTL)
        return out

    async def _brands(self) -> dict[int, str]:
        cached = self.cache.get(self.name, "brands")
        if cached:
            return {int(k): v for k, v in cached.items()}
        try:
            data = await self._get(f"{API}/brands")
        except TransientProviderError:
            return {}
        out = {int(b["id"]): str(b.get("display_name") or b.get("name") or "") for b in data or []
               if b.get("id")}
        self.cache.set(self.name, "brands", {str(k): v for k, v in out.items()}, ttl=STATIONS_TTL)
        return out

    async def _price(self, journey: Journey) -> None:
        try:
            data = await self._get(f"{API}/connections/{journey.journey_id}/price")
        except TransientProviderError as exc:
            log.debug("Koleo: cena %s: %s", journey.journey_id, exc.message)
            return
        price, tariff = parse_price(data)
        if price is None:
            return
        journey.fares = [self.make_fare(price, "PLN", label="Koleo", tariff=tariff)]
        journey.fare_status = FareStatus.PRICED


def to_journey(raw: dict[str, Any], names: dict[int, str],
               brands: dict[int, str] | None = None) -> Journey | None:
    legs = []
    for train in raw.get("trains") or []:
        dep, arr = _time(train.get("departure")), _time(train.get("arrival"))
        if dep is None or arr is None:
            continue
        stops = [Stop(name=names.get(int(s.get("station_id") or 0), ""),
                      arrival=_time(s.get("arrival")), departure=_time(s.get("departure")),
                      platform=s.get("platform") or None)
                 for s in train.get("stops") or [] if s.get("in_path")]
        stops = [s for s in stops if s.name]
        origin = names.get(int(train.get("start_station_id") or 0), stops[0].name if stops else "")
        destination = names.get(int(train.get("end_station_id") or 0), stops[-1].name if stops else "")
        name = str(train.get("train_full_name") or train.get("train_nr") or "")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if "bus" in name.lower() else Mode.TRAIN,
                        carrier=(brands or {}).get(int(train.get("brand_id") or 0)) or "PKP",
                        line=name, stops=stops))
    if not legs:
        return None
    return Journey(provider="PKP", legs=legs, fare_status=FareStatus.UNKNOWN,
                   journey_id=str(raw.get("id") or ""),
                   deep_link=f"{BASE}/",
                   raw={"purchasable": bool(raw.get("purchasable"))})


def parse_price(data: Any) -> tuple[Decimal | None, str]:
    prices = [p for p in (data or {}).get("prices") or [] if p.get("valid_price", True)
              and p.get("value") not in (None, "")]
    if not prices:
        return None, ""
    best = min(prices, key=lambda p: Decimal(str(p["value"])))
    return Decimal(str(best["value"])), ", ".join(best.get("tariff_names") or [])


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
