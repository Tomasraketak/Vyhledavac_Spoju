"""Spoje integrovaných systémů z IDOS – i autobusy, které web ČD neumí.

Jízdenku IDS na autobus (IDESKA, PID, IDS JMK …) žádný e-shop, na který se
ptáme, nenabídne – ČD hledá jen vlaky. IDOS ale zná všechny spoje včetně
autobusů a MHD. Tenhle „dopravce“ tedy:

1. Zeptá se jen, když obě místa leží ve stejném integrovaném systému,
   který umíme spočítat (zóny míst z databáze zastávek).
2. Načte z IDOS spojení (``vyhledavac.idos``, nejvýš 2 stránky po 3).
3. U každého úseku ověří, že platí jízdenka IDS: vlak ČD kategorie Os/Sp/R
   (ne EC/IC/Ex…), autobus/MHD jen linka zařazená do systému – podle
   detailu spoje v IDOS („linka 300 [IDESKA]“) nebo seznamu linek z JDF
   (``data/ids_lines.json.gz``). Dálkové autobusy (UNITED BUSES…) ne.
4. Cenu spočítá tarif systému (``vyhledavac.ids``) podle zón – u tarifů
   podle projetých zón ze všech zastávek z detailu spoje. Skupina,
   předplatné i slevy jako u hacku „jízdenky IDS“.

Cena tedy není od prodejce, ale z tarifu – jízdenku koupíte v aplikaci
systému, u řidiče nebo v automatu.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import idos, stations
from ..errors import StationNotFoundError, TransientProviderError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

LINES = Path(__file__).resolve().parent.parent / "data" / "ids_lines.json.gz"
PAGES = 2
DETAIL_TTL = 24 * 3600


@lru_cache(maxsize=1)
def _lines() -> dict[str, Any]:
    try:
        with gzip.open(LINES, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        return {"systems": {k: set(v) for k, v in data["systems"].items()},
                "zoned": set(data["zoned"])}
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Seznam linek IDS chybí: %s", exc)
        return {"systems": {}, "zoned": set()}


def line_systems(licence: str, tagged: set[str]) -> set[str] | None:
    """Systémy, ve kterých platí jízdenka na lince; ``None`` = nevíme, jen „má pásma“."""
    if tagged:
        return tagged
    data = _lines()
    found = {name for name, lines in data["systems"].items() if licence in lines}
    if found:
        return found
    return None if licence in data["zoned"] else set()


@PROVIDERS.register()
class IdsIdosProvider(HttpProvider):
    name = "IDS"
    label = "Integrované systémy – vlaky i autobusy z IDOS, cena podle tarifu IDS"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ",)
    referer = "https://idos.cz/"
    min_interval = 0.7

    @staticmethod
    def _common_systems(query: SearchQuery) -> set[str]:
        from ..hacks.ids_ticket import SYSTEMS

        a, b = stations.place(query.origin), stations.place(query.destination)
        if a is None or b is None or a.country or b.country:
            return set()
        return set(a.systems) & set(b.systems) & set(SYSTEMS)

    async def search(self, query: SearchQuery) -> list[Journey]:
        if not self._common_systems(query):
            raise StationNotFoundError(self.name, "místa neleží ve stejném integrovaném systému")
        connections: list[idos.IdosConnection] = []
        when = query.departure
        for _ in range(PAGES):
            page = await self.client.get_text(idos.results_url(),
                                              params=idos.params(query.origin, query.destination, when),
                                              referer=self.referer)
            batch = [c for c in idos.parse_results(page, when.astimezone(query.departure.tzinfo).date())
                     if c.departure >= query.departure - dt.timedelta(minutes=1)]
            new = [c for c in batch if all(c.departure != o.departure or c.legs[0].line != o.legs[0].line
                                           for o in connections)]
            if not new:
                break
            connections.extend(new)
            when = max(c.departure for c in new) + dt.timedelta(minutes=1)
            if when > query.window_end:
                break
        journeys = []
        for conn in connections:
            try:
                journey = await self._priced(query, conn)
            except TransientProviderError as exc:
                log.debug("IDS: detail spoje: %s", exc.message)
                continue
            if journey is not None:
                journeys.append(journey)
        return journeys

    async def _detail(self, url: str) -> str:
        cached = self.cache.get(self.name, f"detail:{url}")
        if cached is not None:
            return cached
        page = await self.client.get_text(url, referer=self.referer)
        self.cache.set(self.name, f"detail:{url}", page, ttl=DETAIL_TTL)
        return page

    async def _priced(self, query: SearchQuery, conn: idos.IdosConnection) -> Journey | None:
        from ..hacks.ids_ticket import NOT_IDS, SYSTEMS, IdsTicketHack, RouteStop

        allowed: set[str] | None = None        # systémy platné na všech úsecích
        route: list[RouteStop] = []
        legs: list[Leg] = []
        for leg in conn.legs:
            names = [leg.origin, leg.destination]
            if leg.is_bus or leg.is_city:
                if not leg.detail_url:
                    return None
                page = await self._detail(leg.detail_url)
                licence, tagged = idos.parse_line_info(page)
                systems = line_systems(licence or leg.licence, tagged)
                if systems is not None and not systems:
                    return None                  # linka mimo IDS (dálkový autobus…)
                names = idos.parse_route(page) or names
                rail = False
            else:
                if NOT_IDS.match(leg.train_label) or "České dráhy" not in leg.carrier:
                    return None
                systems = None
                rail = True
            stops = []
            for i, name in enumerate(names):
                place = (stations.rail_place(name) if rail else None) or stations.place(name)
                zones = {k: v for k, v in (place.systems if place else {}).items() if k in SYSTEMS}
                time = leg.departure if i == 0 else (leg.arrival if i == len(names) - 1 else None)
                stop = RouteStop(idos.display_name(name), time, zones, place.kraj if place else "",
                                 idos.display_name(name) if rail else "", rail=rail)
                stops.append(stop)
            if systems is not None:
                allowed = set(systems) if allowed is None else allowed & set(systems)
            route.extend(stops)
            legs.append(Leg(origin=idos.display_name(leg.origin),
                            destination=idos.display_name(leg.destination),
                            departure=leg.departure, arrival=leg.arrival,
                            mode=Mode.TRAM if leg.is_city else Mode.BUS if leg.is_bus else Mode.TRAIN,
                            carrier=leg.carrier, line=leg.train_label,
                            stops=[Stop(name=s.name, arrival=s.time if k else None,
                                        departure=s.time if not k else None)
                                   for k, s in enumerate(stops)]))
        candidates = self._common_systems(query) if allowed is None else allowed & self._common_systems(query)
        minutes = (conn.arrival - conn.departure).total_seconds() / 60
        hack = IdsTicketHack()
        best = None
        for system in sorted(candidates):
            fare = hack._ids_fare(query, route, system, 0, len(route) - 1, minutes)
            if fare is not None and (best is None or fare.price < best.price):
                best = fare
        if best is None:
            return None
        journey = Journey(
            provider=self.name, legs=legs, fare_status=FareStatus.PRICED,
            fares=[self.make_fare(best.price, "CZK", label=best.system,
                                  tariff=f"{best.system}: {best.name}, zóny {best.zone_from}→{best.zone_to}")],
            deep_link=conn.share_url or idos.results_url(),
            notes=[f"🎫 Jízdenka {best.system} ({best.name}) – cena podle tarifu systému; koupíte ji "
                   f"v aplikaci systému, u řidiče nebo v automatu. Spoj z IDOS."])
        return journey
