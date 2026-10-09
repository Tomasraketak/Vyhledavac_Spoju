"""Jízdenky integrovaných systémů místo jízdenky ČD – celá cesta, nebo její části.

Na regionálních vlacích ČD platí i jízdenky IDS a bývají výrazně
levnější. (ČD je prodávají jen v aplikaci Můj vlak – webový e-shop je
nenabízí, proto se ceny IDS počítají z tarifů systémů, viz ``vyhledavac.ids``.)

Hack u každého vlaku ČD zjistí skutečnou trasu (zastávky a časy z cd.cz,
jinak síť z jízdního řádu) a zkusí:

1. **celou cestu na jízdenku jednoho systému** (IDESKA, PID);
2. **dva systémy za sebou** – lom ve stanici, která patří do obou
   (Tábor → Praha: IDESKA do Votic, dál PID);
3. **systém + ČD** – lom v poslední stanici systému (Třeboň → Havlíčkův
   Brod: IDESKA do Počátek-Žirovnice, zbytek u ČD), nebo obráceně.

Vybere nejlevnější variantu. Cestující nikde nepřestupuje – jen má víc
jízdenek. Na ČD se pro lom ptá nejvýš jednou na každou stanici lomu (hledání
přes prohlížeč je pomalé), výsledek poslouží všem vlakům najednou.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

import httpx

from .. import cdtrain, railnet, stations
from ..ids import IdsFare, group_fare, ideska, idpk, idsok, jmk, pid
from ..ids.online import duk, idol, idzk, iredo, vdv
from ..models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery
from ..normalize import normalize
from ..registry import HACKS
from .base import SearchFn, TravelHack

log = logging.getLogger(__name__)

#: Systémy, jejichž tarif umíme spočítat.
SYSTEMS = {m.SYSTEM: m for m in (ideska, pid, idpk, vdv, iredo, idol, jmk, duk, idzk, idsok)}
LINKS = {name: m.URL for name, m in SYSTEMS.items()}
#: Systémy, u kterých se zóny drobných zastávek nemusí dát dohledat (pásmo
#: nádraží se bere z autobusové zastávky v obci). Mezilehlá zastávka bez
#: jakýchkoli údajů tu trasu systému nepřeruší; cenu určují jen zóny konců
#: (u IDPK i projeté zóny, které známe).
LENIENT = {idpk.SYSTEM, vdv.SYSTEM, iredo.SYSTEM, idol.SYSTEM, jmk.SYSTEM, duk.SYSTEM, idzk.SYSTEM,
           idsok.SYSTEM}
#: Na těchto vlacích jízdenka IDS neplatí (dálkové a komerční spoje).
NOT_IDS = re.compile(r"^\s*(EC|IC|SC|EN|ES|Ex|rj|RJ|RJX|LE|LET|NJ|ICE|IR|AEx|TLX)\b")
#: Kolik vlaků nejvýš prověřit (každý = jeden dotaz na cd.cz).
MAX_JOURNEYS = 8


@dataclass
class RouteStop:
    name: str                       # jak ho píše ČD (pro dotaz na úsek)
    time: dt.datetime | None
    systems: dict[str, tuple[str, ...]] = field(default_factory=dict)
    kraj: str = ""
    node: str = ""                  # jméno v železniční síti (pro projeté stanice)
    estimated: bool = False         # čas je odhad ze sítě, ne z jízdního řádu vlaku


@dataclass
class Plan:
    """Rozpis jízdenek pro jeden vlak."""
    total: Decimal
    pieces: list[tuple[str, int, int, IdsFare | Journey]]   # (systém/"ČD", od, do, cena)


def _stop(name: str, display: str | None = None, time: dt.datetime | None = None) -> RouteStop:
    place = stations.rail_place(name)
    systems = {k: v for k, v in (place.systems if place else {}).items() if k in SYSTEMS}
    return RouteStop(display or name, time, systems, place.kraj if place else "", name)


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


def _minutes(a: dt.datetime | None, b: dt.datetime | None, fallback: float) -> float:
    if a is None or b is None:
        return fallback          # bez časů počítáme s celou cestou (bezpečně)
    return max(0.0, (b - a).total_seconds() / 60)


@HACKS.register()
class IdsTicketHack(TravelHack):
    name = "ids_ticket"
    label = "Jízdenky IDS místo ČD (IDESKA, PID, IDPK, VDV, IREDO, IDOL, IDS JMK)"
    default_min_saving_pct = 3.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        if not stations.is_domestic(query.origin, query.destination):
            return []
        base = sorted((j for j in journeys if eligible(j)), key=lambda j: j.departure)
        base = base[: int(self.options.get("max_journeys", MAX_JOURNEYS))]
        if not base:
            return []
        fallback = [_stop(n) for n in railnet.route(query.origin, query.destination)]
        async with httpx.AsyncClient() as client:
            routes = await asyncio.gather(*(self._route(client, query, j) for j in base))
        routes = [r or _with_times(fallback, j) for r, j in zip(routes, base)]
        await self._prefetch(routes)

        # Úseky, na které se musí zeptat ČD – jedna otázka na stanici lomu.
        tails: dict[str, list[Journey]] = {}
        heads: dict[str, list[Journey]] = {}
        for journey, route in zip(base, routes):
            if len(route) < 2:
                continue
            for system in SYSTEMS:
                k = self._last_inside(route, system)
                if k is not None and 0 < k < len(route) - 1:
                    tails.setdefault(route[k].name, []).append(journey)
                k = self._first_inside(route, system)
                if k is not None and 0 < k < len(route) - 1:
                    heads.setdefault(route[k].name, []).append(journey)
        earliest = base[0].departure - dt.timedelta(minutes=2)
        cd_tails = {name: await search(query.leg_query(name, query.destination, earliest),
                                       allow_browser=True) for name in list(tails)[:2]}
        cd_heads = {name: await search(query.leg_query(query.origin, name, earliest),
                                       allow_browser=True) for name in list(heads)[:2]}

        found: list[Journey] = []
        for journey, route in zip(base, routes):
            if len(route) < 2:
                continue
            try:
                plan = self._best_plan(query, journey, route, cd_tails, cd_heads)
            except Exception:
                log.exception("ids_ticket: vyhodnocení spoje selhalo")
                continue
            if plan is not None and self.is_worth_it(plan.total, journey.price):
                found.append(self._result(journey, route, plan))
        return found

    async def _prefetch(self, routes: list[list[RouteStop]]) -> None:
        """Systémy, jejichž ceny se zjišťují online (VDV), si je stáhnou předem.

        Ptáme se jen na dvojice zón, které může plán použít: konce trasy
        a stanice na hranicích systémů.
        """
        for system, module in SYSTEMS.items():
            if not hasattr(module, "prefetch"):
                continue
            pairs: set[tuple[str, str]] = set()
            for route in routes:
                n = len(route) - 1
                if n < 1:
                    continue
                points = {0, n} | {k for k in range(1, n) if len(route[k].systems) >= 2}
                for other in SYSTEMS:
                    for k in (self._last_inside(route, other), self._first_inside(route, other)):
                        if k is not None:
                            points.add(k)
                points = sorted(i for i in points if system in route[i].systems)
                for a in points:
                    for b in points:
                        if a < b:
                            pairs.update((x, y) for x in route[a].systems[system]
                                         for y in route[b].systems[system])
            try:
                await module.prefetch(pairs)
            except Exception:
                log.exception("ids_ticket: ceny %s se nepodařilo stáhnout", system)

    # ---- trasa vlaku ------------------------------------------------------------
    async def _route(self, client: httpx.AsyncClient, query: SearchQuery,
                     journey: Journey) -> list[RouteStop]:
        """Zastávky vlaku od nástupu do výstupu se systémy (jen přímé vlaky)."""
        rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
        if len(rides) != 1:
            return []
        stops = await cdtrain.train_stops(
            client, rides[0].line, journey.departure, journey.arrival,
            railnet.station_code(query.origin), railnet.station_code(query.destination))
        start = next((i for i, s in enumerate(stops) if _close(s.departure, journey.departure, 120)), None)
        end = next((i for i, s in enumerate(stops) if _close(s.arrival, journey.arrival, 120)), None)
        if start is None or end is None or end <= start:
            return []     # vlak nejede celou cestu (přestup) – použije se síť
        route = []
        for i, s in enumerate(stops[start:end + 1]):
            ours = railnet.name_for_code(s.code) or s.name
            route.append(_stop(ours, s.name, s.departure if i == 0 else s.arrival or s.departure))
        return route

    # ---- plánování ----------------------------------------------------------------
    @staticmethod
    def _inside(stop: RouteStop, system: str) -> bool:
        return system in stop.systems or (system in LENIENT and not stop.systems)

    @classmethod
    def _last_inside(cls, route: list[RouteStop], system: str) -> int | None:
        if system not in route[0].systems:
            return None
        k = 0
        while k + 1 < len(route) and cls._inside(route[k + 1], system):
            k += 1
        while k > 0 and system not in route[k].systems:
            k -= 1             # lom jen ve stanici se známou zónou
        return k

    @classmethod
    def _first_inside(cls, route: list[RouteStop], system: str) -> int | None:
        if system not in route[-1].systems:
            return None
        k = len(route) - 1
        while k - 1 >= 0 and cls._inside(route[k - 1], system):
            k -= 1
        while k < len(route) - 1 and system not in route[k].systems:
            k += 1
        return k

    def _ids_fare(self, query: SearchQuery, route: list[RouteStop], system: str,
                  i: int, j: int, minutes: float) -> IdsFare | None:
        part = route[i:j + 1]
        if system not in part[0].systems or system not in part[-1].systems:
            return None
        if any(not self._inside(s, system) for s in part):
            return None
        if system == pid.SYSTEM and not any(s.kraj in pid.HOME_REGIONS for s in part):
            return None        # PID neplatí na cesty jen po jiném kraji
        module = SYSTEMS[system]
        if hasattr(module, "best_for_route"):        # cena podle všech projetých zón
            passed = _passed_zones(part, system)
            return group_fare(query.passengers,
                              lambda p: module.best_for_route(passed, p, minutes))
        return group_fare(query.passengers, lambda p: module.best_between(
            route[i].systems[system], route[j].systems[system], p, minutes))

    def _best_plan(self, query: SearchQuery, journey: Journey, route: list[RouteStop],
                   cd_tails: dict[str, list[Journey]],
                   cd_heads: dict[str, list[Journey]]) -> Plan | None:
        n = len(route) - 1
        whole = journey.duration.total_seconds() / 60
        plans: list[Plan] = []

        def ids(system: str, i: int, j: int, a=None, b=None) -> IdsFare | None:
            a = a if a is not None else (journey.departure if i == 0 else route[i].time)
            b = b if b is not None else (journey.arrival if j == n else route[j].time)
            minutes = _minutes(a, b, whole)
            if (0 < i and route[i].estimated) or (j < n and route[j].estimated):
                minutes = min(whole, minutes * 1.15 + 5)     # rezerva u odhadnutého času
            return self._ids_fare(query, route, system, i, j, minutes)

        # 1+2) jízdenky IDS za sebou – jeden systém, nebo řetěz několika
        #      (Praha → Brno: PID, IREDO, IDS JMK). Lom jen ve stanici, která
        #      patří do více systémů; nejlevnější řetěz hledá dynamické programování.
        nodes = [0] + [k for k in range(1, n) if len(route[k].systems) >= 2] + [n]
        best: dict[int, Plan] = {0: Plan(Decimal(0), [])}
        for b in nodes[1:]:
            for a in nodes:
                if a >= b or a not in best:
                    continue
                for system in SYSTEMS:
                    if system not in route[a].systems or system not in route[b].systems:
                        continue
                    fare = ids(system, a, b)
                    if fare is None:
                        continue
                    total = best[a].total + fare.price
                    if b not in best or total < best[b].total:
                        best[b] = Plan(total, best[a].pieces + [(system, a, b, fare)])
        if n in best and best[n].pieces:
            plans.append(best[n])

        # 3) systém od začátku, zbytek u ČD
        for system in SYSTEMS:
            k = self._last_inside(route, system)
            if k is None or not 0 < k < n:
                continue
            other = _same_train_tail(cd_tails.get(route[k].name, ()), journey, route[k].time)
            if other is None:
                continue
            fare = ids(system, 0, k, b=other.departure)
            if fare is not None:
                plans.append(Plan(fare.price + other.price, [(system, 0, k, fare), ("ČD", k, n, other)]))

        # 4) začátek u ČD, dál systém
        for system in SYSTEMS:
            k = self._first_inside(route, system)
            if k is None or not 0 < k < n:
                continue
            other = _same_train_head(cd_heads.get(route[k].name, ()), journey, route[k].time)
            if other is None:
                continue
            fare = ids(system, k, n, a=other.arrival)
            if fare is not None:
                plans.append(Plan(fare.price + other.price, [("ČD", 0, k, other), (system, k, n, fare)]))

        return min(plans, key=lambda p: p.total, default=None)

    # ---- výsledek -------------------------------------------------------------------
    def _result(self, journey: Journey, route: list[RouteStop], plan: Plan) -> Journey:
        segments: list[Journey] = []
        lines: list[str] = []
        for system, i, j, price in plan.pieces:
            origin, destination = route[i].name, route[j].name
            if isinstance(price, Journey):
                segment = price
                lines.append(f"{origin} → {destination}: jízdenka ČD {segment.price:.0f} Kč")
            else:
                segment = _ids_segment(journey, origin, destination, route[i].time, route[j].time,
                                       price)
                lines.append(f"{origin} → {destination}: jízdenka {system} ({price.name}, "
                             f"zóny {_zones(price)}) {price.price:.0f} Kč")
            segments.append(segment)
        saving = journey.price - plan.total
        if len(segments) == 1:
            fare = plan.pieces[0][3]
            note = (f"🎫 Na celou cestu stačí jízdenka {fare.system} ({fare.name}, zóny "
                    f"{_zones(fare)}) za {plan.total:.0f} Kč místo {journey.price:.0f} Kč u ČD – "
                    f"ušetříte {saving:.0f} Kč.")
        else:
            note = (f"✂️ Nepřestupujete – stejný vlak, jen {len(segments)} jízdenky: "
                    + "; ".join(lines) + f". Celkem {plan.total:.0f} Kč místo "
                    f"{journey.price:.0f} Kč za jednu jízdenku ČD – ušetříte {saving:.0f} Kč.")
        notes = [note, "Jízdenky IDS platí v regionálních vlacích ČD (Os, Sp, R) zařazených do "
                       "systému; koupíte je v aplikaci systému, v aplikaci ČD Můj vlak nebo u průvodčího."]
        label = " + ".join(dict.fromkeys(p[0] if p[0] != "ČD" else "ČD" for p in plan.pieces))
        return Journey(
            provider=label, legs=list(journey.legs),
            fares=[Fare(amount=plan.total, currency="CZK", amount_czk=plan.total,
                        label="součet jízdenek" if len(segments) > 1 else label,
                        tariff=" + ".join(s.fares[0].tariff or s.provider for s in segments if s.fares))],
            fare_status=FareStatus.PRICED, notes=notes, segments=segments,
            deep_link=journey.deep_link,
        )


# ---- pomůcky ---------------------------------------------------------------------
def _with_times(route: list[RouteStop], journey: Journey) -> list[RouteStop]:
    """Odhad časů na trase ze sítě, když trasu vlaku z cd.cz nemáme.

    Jízdní doby úseků se škálují na skutečnou dobu cesty. U jízdenky, která
    končí v bodě lomu, se počítá s rezervou (+15 % a 5 min), aby odhad nikdy
    nebyl kratší než skutečnost.
    """
    cumulative = railnet.cumulative_minutes([s.node or s.name for s in route])
    if not cumulative or cumulative[-1] <= 0:
        return route
    scale = journey.duration.total_seconds() / 60 / cumulative[-1]
    timed = []
    for stop, minutes in zip(route, cumulative):
        when = journey.departure + dt.timedelta(minutes=minutes * scale)
        timed.append(RouteStop(stop.name, when, stop.systems, stop.kraj, stop.node, True))
    return timed


def _passed_zones(part: list[RouteStop], system: str) -> list[tuple[str, ...]]:
    """Zóny všech stanic, kterými vlak projede – i těch, kde nestaví."""
    zones: list[tuple[str, ...]] = [part[0].systems.get(system, ())]
    for a, b in zip(part, part[1:]):
        between = railnet.physical_route(a.node or a.name, b.node or b.name)[1:-1]
        for name in between:
            place = stations.rail_place(name)
            zones.append(place.systems.get(system, ()) if place else ())
        zones.append(b.systems.get(system, ()))
    return zones


def _zones(fare: IdsFare) -> str:
    return fare.zone_from if fare.zone_from == fare.zone_to else f"{fare.zone_from}→{fare.zone_to}"


def _ids_segment(journey: Journey, origin: str, destination: str, departure: dt.datetime | None,
                 arrival: dt.datetime | None, fare: IdsFare) -> Journey:
    first = next((leg for leg in journey.legs if leg.mode is not Mode.WALK), journey.legs[0])
    leg = Leg(origin=origin, destination=destination,
              departure=departure or journey.departure, arrival=arrival or journey.arrival,
              mode=first.mode, carrier=first.carrier, line=first.line, stops=[])
    return Journey(
        provider=fare.system, legs=[leg],
        fares=[Fare(amount=fare.price, currency="CZK", amount_czk=fare.price,
                    label=fare.system, tariff=f"{fare.name}, zóny {_zones(fare)}")],
        fare_status=FareStatus.PRICED, deep_link=LINKS.get(fare.system),
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
