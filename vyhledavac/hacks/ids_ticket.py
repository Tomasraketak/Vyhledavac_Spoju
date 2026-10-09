"""Jízdenka integrovaného systému místo jízdenky ČD – celá cesta, nebo její část.

Na regionálních vlacích ČD platí i jízdenky IDS a bývají až o polovinu
levnější (ČD je ostatně v e-shopu nabízí jako jednu z variant ceny).
Hack pro každý nalezený vlak ČD zjistí jeho skutečnou trasu a:

* **celá cesta v IDESKA** -> spočítá cenu jízdenky IDESKA a nabídne ji,
  je-li levnější;
* **cesta z IDESKA ven** (Třeboň -> Havlíčkův Brod) -> najde poslední
  stanici, kde ještě platí IDESKA (díky přesahům třeba Počátky-Žirovnice
  na Vysočině), na první úsek spočítá jízdenku IDESKA a na zbytek se zeptá
  ČD (stejný vlak, jen od té stanice). Součet porovná s jednou jízdenkou;
* **cesta do IDESKA** obráceně.

Cestující nikde nepřestupuje – jen má dvě jízdenky. Trasu vlaku i časy
bere z cd.cz (``cdtrain``), jinak ze sítě sestavené z jízdního řádu
(``railnet``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

import httpx

from .. import cdtrain, railnet, stations
from ..ids import IdsFare, ideska
from ..models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery
from ..normalize import normalize
from ..registry import HACKS
from .base import SearchFn, TravelHack

log = logging.getLogger(__name__)

#: Na těchto vlacích jízdenka IDS neplatí (dálkové a komerční spoje).
NOT_IDS = re.compile(r"^\s*(EC|IC|SC|EN|ES|Ex|rj|RJ|RJX|LE|LET|NJ|ICE|IR|AEx|TLX)\b")
#: Kolik vlaků nejvýš prověřit (každý = jeden dotaz na cd.cz).
MAX_JOURNEYS = 8


@dataclass
class RouteStop:
    name: str
    time: dt.datetime | None
    zones: tuple[str, ...]          # zóny IDESKA, prázdné = mimo systém


def _ideska_zones(name: str) -> tuple[str, ...]:
    place = stations.place(name)
    return place.systems.get(ideska.SYSTEM, ()) if place else ()


def eligible(journey: Journey) -> bool:
    """Spoj ČD, na kterém může platit jízdenka IDS."""
    if journey.is_hack or journey.price is None:
        return False
    rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
    if not rides:
        return False
    for leg in rides:
        if leg.mode not in (Mode.TRAIN, Mode.BUS):
            return False
        carrier = normalize(leg.carrier or journey.provider)
        if "cd" not in carrier.split() and "ceske drahy" not in carrier:
            return False
        if NOT_IDS.match(leg.line or ""):
            return False
    return True


@HACKS.register()
class IdsTicketHack(TravelHack):
    name = "ids_ticket"
    label = "Jízdenka IDS místo ČD (IDESKA, i lom na hranici systému)"
    default_min_saving_pct = 3.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        if not stations.is_domestic(query.origin, query.destination):
            return []
        base = sorted((j for j in journeys if eligible(j)), key=lambda j: j.departure)
        base = base[: int(self.options.get("max_journeys", MAX_JOURNEYS))]
        if not base:
            return []
        fallback = [RouteStop(n, None, _ideska_zones(n))
                    for n in railnet.route(query.origin, query.destination)]
        async with httpx.AsyncClient() as client:
            routes = await asyncio.gather(*(self._route(client, query, j) for j in base))

        found: list[Journey] = []
        for journey, route in zip(base, routes):
            route = route or fallback
            if len(route) < 2 or not any(stop.zones for stop in route):
                continue
            try:
                hacked = await self._evaluate(query, journey, route, search)
            except Exception:
                log.exception("ids_ticket: vyhodnocení spoje selhalo")
                continue
            if hacked is not None:
                found.append(hacked)
        return found

    # ---- trasa vlaku ------------------------------------------------------------
    async def _route(self, client: httpx.AsyncClient, query: SearchQuery,
                     journey: Journey) -> list[RouteStop]:
        """Zastávky vlaku od nástupu do výstupu se zónami IDESKA (jen přímé vlaky)."""
        rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
        if len(rides) != 1:
            return []
        leg = rides[0]
        stops = await cdtrain.train_stops(
            client, leg.line, journey.departure, journey.arrival,
            railnet.station_code(query.origin), railnet.station_code(query.destination))
        if not stops:
            return []
        # výřez trasy mezi nástupem a výstupem podle časů
        start = next((i for i, s in enumerate(stops)
                      if s.departure and abs((s.departure - journey.departure).total_seconds()) <= 120), None)
        end = next((i for i, s in enumerate(stops)
                    if s.arrival and abs((s.arrival - journey.arrival).total_seconds()) <= 120), None)
        if start is None or end is None or end <= start:
            return []     # vlak nejede celou cestu (přestup) – použije se síť
        route = []
        for i, s in enumerate(stops[start:end + 1]):
            name = railnet.name_for_code(s.code) or s.name
            time = s.departure if i == 0 else s.arrival or s.departure
            route.append(RouteStop(s.name, time, _ideska_zones(name)))
        return route

    # ---- vyhodnocení ------------------------------------------------------------
    async def _evaluate(self, query: SearchQuery, journey: Journey, route: list[RouteStop],
                        search: SearchFn) -> Journey | None:
        inside = [bool(stop.zones) for stop in route]
        passenger = query.lead
        baseline = journey.price

        if all(inside):
            minutes = journey.duration.total_seconds() / 60
            fare = ideska.best_between(route[0].zones, route[-1].zones, passenger, minutes)
            if fare is None or not self.is_worth_it(fare.price, baseline):
                return None
            segment = _ids_segment(journey, route[0].name, route[-1].name,
                                   journey.departure, journey.arrival, fare)
            note = (f"🎫 Na celou cestu stačí jízdenka {fare.system} ({fare.name}, zóny "
                    f"{_zones(fare)}) za {fare.price:.0f} Kč místo {baseline:.0f} Kč u ČD – "
                    f"ušetříte {baseline - fare.price:.0f} Kč. Platí v regionálních vlacích "
                    f"ČD (Os, Sp, R), koupíte ji u průvodčího nebo v aplikaci IDS JK.")
            return _result(journey, [segment], fare.price, [note], f"{fare.system} místo ČD")

        if inside[0]:
            k = next(i for i, ok in enumerate(inside) if not ok) - 1
            if k <= 0:
                return None
            split = route[k]
            rest = await search(query.leg_query(split.name, query.destination,
                                                (split.time or journey.departure) - dt.timedelta(minutes=2)),
                                allow_browser=True)
            other = _same_train_tail(rest, journey, split.time)
            if other is None:
                return None
            minutes = (other.departure - journey.departure).total_seconds() / 60
            fare = ideska.best_between(route[0].zones, split.zones, passenger, minutes)
            if fare is None or other.price is None:
                return None
            first = _ids_segment(journey, route[0].name, split.name,
                                 journey.departure, other.departure, fare)
            return self._split_result(journey, [first, other], fare, other, split.name)

        if inside[-1]:
            k = max(i for i, ok in enumerate(inside) if not ok) + 1
            if k >= len(route) - 1:
                return None
            split = route[k]
            head = await search(query.leg_query(query.origin, split.name,
                                                journey.departure - dt.timedelta(minutes=2)),
                                allow_browser=True)
            other = _same_train_head(head, journey, split.time)
            if other is None:
                return None
            minutes = (journey.arrival - other.arrival).total_seconds() / 60
            fare = ideska.best_between(split.zones, route[-1].zones, passenger, minutes)
            if fare is None or other.price is None:
                return None
            second = _ids_segment(journey, split.name, route[-1].name,
                                  other.arrival, journey.arrival, fare)
            return self._split_result(journey, [other, second], fare, other, split.name)
        return None

    def _split_result(self, journey: Journey, segments: list[Journey], fare: IdsFare,
                      other: Journey, split: str) -> Journey | None:
        total = fare.price + other.price
        baseline = journey.price
        if not self.is_worth_it(total, baseline):
            return None
        parts = " + ".join(
            f"{s.origin} → {s.destination} ({s.provider}) {s.price:.0f} Kč" for s in segments)
        note = (f"✂️ Nepřestupujete – stejný vlak, jen dvě jízdenky: {parts}. "
                f"Celkem {total:.0f} Kč místo {baseline:.0f} Kč u ČD na celou cestu "
                f"– ušetříte {baseline - total:.0f} Kč. Jízdenka {fare.system} "
                f"({fare.name}, zóny {_zones(fare)}) platí až do stanice {split}.")
        return _result(journey, segments, total, [note], f"{fare.system} + {other.provider}")


# ---- pomůcky ---------------------------------------------------------------------
def _zones(fare: IdsFare) -> str:
    return fare.zone_from if fare.zone_from == fare.zone_to else f"{fare.zone_from}→{fare.zone_to}"


def _ids_segment(journey: Journey, origin: str, destination: str, departure: dt.datetime,
                 arrival: dt.datetime, fare: IdsFare) -> Journey:
    first = next((leg for leg in journey.legs if leg.mode is not Mode.WALK), journey.legs[0])
    leg = Leg(origin=origin, destination=destination, departure=departure, arrival=arrival,
              mode=first.mode, carrier=first.carrier, line=first.line, stops=[])
    return Journey(
        provider=fare.system, legs=[leg],
        fares=[Fare(amount=fare.price, currency="CZK", amount_czk=fare.price,
                    label=fare.system, tariff=f"{fare.name}, zóny {_zones(fare)}")],
        fare_status=FareStatus.PRICED, deep_link=ideska.URL,
    )


def _result(journey: Journey, segments: list[Journey], total: Decimal, notes: list[str],
            label: str) -> Journey:
    return Journey(
        provider=label, legs=list(journey.legs),
        fares=[Fare(amount=total, currency="CZK", amount_czk=total,
                    label="součet jízdenek" if len(segments) > 1 else segments[0].provider,
                    tariff=" + ".join(s.fares[0].tariff or s.provider for s in segments if s.fares))],
        fare_status=FareStatus.PRICED, notes=notes, segments=segments,
        deep_link=journey.deep_link,
    )


def _close(a: dt.datetime | None, b: dt.datetime | None, seconds: int = 180) -> bool:
    return a is not None and b is not None and abs((a - b).total_seconds()) <= seconds


def _same_train_tail(options: Sequence[Journey], journey: Journey,
                     split_time: dt.datetime | None) -> Journey | None:
    """Zbytek téhož vlaku: stejný příjezd, odjezd mezi odjezdem a příjezdem celé cesty."""
    best = None
    for option in options:
        if option.price is None or not _close(option.arrival, journey.arrival):
            continue
        if not (journey.departure < option.departure < journey.arrival):
            continue
        if split_time is not None and not _close(option.departure, split_time, 300):
            continue
        if best is None or option.price < best.price:
            best = option
    return best


def _same_train_head(options: Sequence[Journey], journey: Journey,
                     split_time: dt.datetime | None) -> Journey | None:
    """Začátek téhož vlaku: stejný odjezd, příjezd do bodu lomu před koncem cesty."""
    best = None
    for option in options:
        if option.price is None or not _close(option.departure, journey.departure):
            continue
        if not (journey.departure < option.arrival < journey.arrival):
            continue
        if split_time is not None and not _close(option.arrival, split_time, 300):
            continue
        if best is None or option.price < best.price:
            best = option
    return best
