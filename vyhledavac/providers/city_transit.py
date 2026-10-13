"""MHD po zahraničním městě (Vídeň, Bratislava, Budapešť …) z Transitous.

Česká města zná IDOS – cestu po Praze, Brně nebo Ostravě najde zdroj
``IDS`` a ocení ji tarif systému (PID, IDS JMK, ODIS …). Zahraniční MHD IDOS
nezná, Transitous ano (data Wiener Linien, DPB, BKK …). Tenhle zdroj:

1. Ptá se jen u cesty po jednom zahraničním městě (``vyhledavac.city``).
2. Zastávky najde geokodérem Transitous v okruhu 25 km od středu města
   („Stephansplatz“, „Deák Ferenc tér“); samotné město = jeho střed.
3. Spojení vrátí celé – tramvaj, metro, autobus i pěší přesuny.
4. Cenu spočítá podle jízdenky místní MHD (``city.FARES``): Vídeň jednotně,
   Bratislava a Budapešť podle doby jízdy (30/60/90 min), v Budapešti
   jízdenka na jednu linku, když se nepřestupuje. Město bez ceníku = spoj
   bez ceny s poznámkou.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .. import city, stations, transitous
from ..errors import StationNotFoundError
from ..models import FareStatus, Journey, Mode, SearchQuery
from ..normalize import tokens
from ..registry import PROVIDERS
from ..trace import trace
from .base import HttpProvider
from .planner import _km, parse_leg

log = logging.getLogger(__name__)

NEAR_KM = 25.0
PLACE_TTL = 30 * 24 * 3600
ITINERARIES = 5


@PROVIDERS.register()
class CityTransitProvider(HttpProvider):
    name = "MHD"
    label = "MHD po zahraničním městě (Transitous) – Vídeň, Bratislava, Budapešť … s cenou místní jízdenky"
    multi_passenger = True
    currency = "EUR"
    countries = ()
    czech_names = True
    hack_legs = False
    exact_places = True
    min_interval = 0.6

    async def _place(self, name: str, stop: str, center: tuple[float, float]) -> str:
        """Zastávka pro Transitous („lat,lon“) – nejpodobnější jménu poblíž středu města."""
        if not stop:
            return f"{center[0]},{center[1]}"
        key = f"mhd-place:{name}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        found = await self.client.get_json(f"{transitous.API}/v1/geocode", params={"text": name, "language": "en"},
                                           headers={"User-Agent": transitous.USER_AGENT}) or []
        if not any(r.get("type") == "STOP" for r in found):
            found = await self.client.get_json(f"{transitous.API}/v1/geocode",
                                               params={"text": stop, "language": "en"},
                                               headers={"User-Agent": transitous.USER_AGENT}) or []
        wanted = tokens(stop)
        best, best_key = None, None
        for r in found:
            if r.get("lat") is None or r.get("type") not in ("STOP", "PLACE", "ADDRESS"):
                continue
            km = _km(center[0], center[1], float(r["lat"]), float(r["lon"]))
            if km > NEAR_KM:
                continue
            common = len(wanted & tokens(str(r.get("name") or "")))
            rank = (-common, r.get("type") != "STOP", km)
            if best_key is None or rank < best_key:
                best, best_key = f"{r['lat']},{r['lon']}", rank
        if best is None:
            raise StationNotFoundError(self.name, f"zastávku {stop!r} v MHD nenašel")
        trace(f"MHD: {name} → {best}")
        self.cache.set(self.name, key, best, ttl=PLACE_TTL)
        return best

    async def search(self, query: SearchQuery) -> list[Journey]:
        trip = city.trip(query.origin, query.destination)
        if trip is None or not trip.code:
            raise StationNotFoundError(self.name, "jen cesty po jednom zahraničním městě (česká města hledá IDOS)")
        place = stations.place(trip.city)
        if place is None or place.lat is None:
            raise StationNotFoundError(self.name, f"střed města {trip.city} neznám")
        center = (place.lat, place.lon)
        origin = await self._place(query.origin, trip.origin_stop, center)
        destination = await self._place(query.destination, trip.destination_stop, center)
        params = {"fromPlace": origin, "toPlace": destination, "numItineraries": ITINERARIES,
                  "time": query.departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if query.trains_only:
            params["transitModes"] = "RAIL"
        data = await self.client.get_json(f"{transitous.API}/v5/plan", params=params,
                                          headers={"User-Agent": transitous.USER_AGENT}) or {}
        journeys: list[Journey] = []
        for it in data.get("itineraries") or []:
            journey = self.to_journey(it, query, trip)
            if journey is None or journey.departure > query.window_end:
                continue
            if any((journey.departure, journey.arrival) == (o.departure, o.arrival) for o in journeys):
                continue
            journeys.append(journey)
        return journeys

    def to_journey(self, it: dict[str, Any], query: SearchQuery, trip: city.CityTrip) -> Journey | None:
        legs = [leg for leg in (parse_leg(raw) for raw in it.get("legs") or []) if leg is not None]
        rides = [leg for leg in legs if leg.mode is not Mode.WALK]
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link="https://transitous.org/")
        if not rides:
            journey.notes.append("Pěšky – jízdenka není potřeba.")
            return journey
        minutes = (rides[-1].arrival - rides[0].departure).total_seconds() / 60
        found = city.ticket_for(trip, len(rides), minutes)
        if found is None:
            journey.notes.append(f"Jízdenku MHD ({trip.city}) koupíte na místě – ceník MHD tohoto města "
                                 "vyhledávač nezná.")
            return journey
        currency, ticket = found
        factor = city.passenger_factor(trip.city, [p.age for p in query.passengers])
        journey.fares = [self.make_fare(ticket.price * factor, currency, label=f"MHD {trip.city}",
                                        tariff=ticket.label)]
        journey.fare_status = FareStatus.PRICED
        journey.notes.append(f"🎫 {ticket.label} – koupíte v automatu, aplikaci nebo trafice; "
                             "cena podle ceníku MHD.")
        return journey
