"""Přestup v Praze nebo Brně mezi dvěma různými dopravci.

Plzeň → Liberec: vlakem do Prahy (ČD/SJT u Arrivy, RegioJet, Leo…), metrem
na Černý Most a dál autobusem RegioJet. Dopravce takovou kombinaci sám
nenabídne, protože neprodává jízdenku toho druhého.

Postup (spouští se až po přímém hledání a hacku IDS):

1. Vede-li některý z nalezených spojů přes Prahu/Brno (zastávka na trase),
   zeptá se dopravců na úsek *odkud → město* a *město → kam* – dva dotazy na
   město, sdílené s ostatními hacky (cache enginu).
2. Spáruje příjezdy s odjezdy: přestup musí stihnout přejezd mezi nádražími
   / terminály (``citytransfer``: metro, tramvaj, pěšky) plus rezervu.
3. Kombinace musí být od dvou různých dopravců (stejný dopravce by ji
   nabídl sám) a musí být levnější než nejlevnější přímý spoj, nebo
   rychlejší než nejrychlejší přímý spoj a přitom ne dražší než on.
   K ceně se připočte jízdenka MHD (pěší přesun zdarma).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from .. import citytransfer, railnet
from ..models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery
from ..registry import HACKS
from .base import SearchFn, TravelHack, combine

log = logging.getLogger(__name__)

MAX_FIRST = 8          # kolik příjezdů do města zkoušet
MAX_RESULTS = 6


@HACKS.register()
class HubTransferHack(TravelHack):
    name = "hub_transfer"
    label = "Přestup v Praze / Brně mezi dopravci (vlak + bus …)"
    default_min_saving_pct = 3.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        hubs = self._hubs_on_route(query, journeys)
        found: list[Journey] = []
        for hub in hubs:
            try:
                found.extend(await self._via(query, hub, journeys, search))
            except Exception:
                log.exception("hub_transfer: %s selhalo", hub)
        return found

    # ---- přes které město ---------------------------------------------------
    @staticmethod
    def _hubs_on_route(query: SearchQuery, journeys: Sequence[Journey]) -> list[str]:
        ends = {citytransfer.hub_of(query.origin), citytransfer.hub_of(query.destination)}
        seen: dict[str, int] = {}
        for journey in journeys:
            for leg in journey.legs:
                for name in [leg.origin, leg.destination, *(s.name for s in leg.stops)]:
                    hub = citytransfer.hub_of(name or "")
                    if hub and hub not in ends:
                        seen[hub] = seen.get(hub, 0) + 1
        if not seen:      # bez zastávek ve výsledcích: trasa ze železniční sítě
            for name in railnet.route(query.origin, query.destination):
                hub = citytransfer.hub_of(name)
                if hub and hub not in ends:
                    seen[hub] = seen.get(hub, 0) + 1
        return sorted(seen, key=lambda h: -seen[h])

    # ---- kombinace přes jedno město -------------------------------------------
    async def _via(self, query: SearchQuery, hub: str, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        city = citytransfer.HUBS[hub]
        first_legs = [j for j in await search(query.leg_query(query.origin, city))
                      if j.price is not None and citytransfer.hub_of(j.destination) == hub]
        if not first_legs:
            return []
        first_legs.sort(key=lambda j: j.arrival)
        first_legs = _cheapest_per_arrival(first_legs)[:MAX_FIRST]
        earliest = first_legs[0].arrival
        second_legs = [j for j in await search(query.leg_query(city, query.destination, earliest))
                       if j.price is not None and citytransfer.hub_of(j.origin) == hub]
        if not second_legs:
            return []

        direct = [j for j in journeys if j.price is not None and not j.is_hack]
        cheapest = min((j.price for j in direct), default=None)
        fastest = min(direct, key=lambda j: j.duration, default=None)

        options: list[Journey] = []
        for first in first_legs:
            best: tuple[Decimal, Journey] | None = None
            for second in second_legs:
                if _same_seller(first, second):
                    continue
                move = citytransfer.transfer(first.destination, second.origin)
                if move is None:
                    continue
                gap = (second.departure - first.arrival).total_seconds() / 60
                if gap < move.needed or gap > 180:
                    continue
                fare = citytransfer.city_fare(move, query.passengers)
                if fare is None:
                    continue
                total = first.price + second.price + fare
                if best is None or (total, second.arrival) < (best[0], best[1].arrival):
                    best = (total, self._build(first, second, move, fare, query))
            if best is None:
                continue
            total, journey = best
            cheaper = cheapest is None or self.is_worth_it(total, cheapest)
            quicker = (fastest is not None and journey.duration < fastest.duration
                       and total <= fastest.price)
            if cheaper or quicker:
                options.append(journey)
        options.sort(key=lambda j: (j.price, j.arrival))
        return _best_per_departure(options)[:MAX_RESULTS]

    @staticmethod
    def _build(first: Journey, second: Journey, move: citytransfer.Transfer, fare: Decimal,
               query: SearchQuery) -> Journey:
        segments = [first]
        if move.minutes:
            start = first.arrival
            leg = Leg(origin=first.destination, destination=second.origin, departure=start,
                      arrival=start + dt.timedelta(minutes=move.minutes),
                      mode=Mode.WALK if move.walk else Mode.METRO if "metro" in move.how else Mode.TRAM,
                      carrier="pěšky" if move.walk else ("PID" if move.city == "praha" else "IDS JMK"),
                      line=move.how)
            label = "pěšky" if move.walk else ("MHD Praha (PID)" if move.city == "praha" else "MHD Brno (IDS JMK)")
            segments.append(Journey(
                provider=label, legs=[leg],
                fares=[Fare(amount=fare, currency="CZK", amount_czk=fare, label=label,
                            tariff="jízdenka MHD" if fare else "zdarma")],
                fare_status=FareStatus.PRICED))
        segments.append(second)
        city = citytransfer.HUBS[move.city]
        hop = ("stejné místo" if not move.minutes else
               f"{move.origin} → {move.destination}: {move.how}, asi {move.minutes} min"
               + (f", jízdenka MHD {fare:.0f} Kč" if fare else ", zdarma"))
        wait = int((second.departure - first.arrival).total_seconds() // 60)
        note = (f"🔀 Přestup v {_locative(city)} mezi dopravci: {first.provider} do "
                f"{first.destination} ({first.arrival:%H:%M}), pak {second.provider} z "
                f"{second.origin} ({second.departure:%H:%M}). Přesun {hop}; na přestup {wait} min.")
        warn = ("⚠️ Dvě samostatné jízdenky – při zpoždění prvního spoje druhý dopravce "
                "na přestup nečeká a jízdenka nepřechází.")
        merged = combine(f"{first.provider} + {second.provider}", segments, notes=[note, warn])
        return merged


def _locative(city: str) -> str:
    return {"Praha": "Praze", "Brno": "Brně"}.get(city, city)


def _same_seller(a: Journey, b: Journey) -> bool:
    return a.provider.split(" ")[0] == b.provider.split(" ")[0]


def _cheapest_per_arrival(journeys: list[Journey]) -> list[Journey]:
    best: dict[tuple, Journey] = {}
    for j in journeys:
        key = (j.arrival, j.destination)
        if key not in best or j.price < best[key].price:
            best[key] = j
    return sorted(best.values(), key=lambda j: j.arrival)


def _best_per_departure(journeys: list[Journey]) -> list[Journey]:
    best: dict[dt.datetime, Journey] = {}
    for j in journeys:
        if j.departure not in best or j.price < best[j.departure].price:
            best[j.departure] = j
    return sorted(best.values(), key=lambda j: j.departure)
