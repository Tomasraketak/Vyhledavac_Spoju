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
jízdenek. **Lom jen ve stanici, kde vlak zastavuje:** podle SPPO ČD musí mít
cestující jízdenku aspoň do nejbližší stanice, kde jeho vlak staví – jízdenka
„do“ stanice, kterou vlak projíždí, nestačí. Když trasu vlaku z cd.cz nemáme
(vlak cestou mění číslo) a bere se ze sítě, smí být lom jen v zastávce
potvrzené jízdním řádem spoje (ČD, Arriva, cd.cz po částech). Na ČD se pro lom ptá nejvýš jednou na každou stanici lomu (hledání
přes prohlížeč je pomalé), výsledek poslouží všem vlakům najednou.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Sequence

import httpx

from .. import cdtrain, railnet, stations
from ..ids import IdsFare, group_fare, ideska, idpk, idsok, jmk, pid
from ..ids.online import duk, idol, idzk, iredo, vdv
from ..models import Fare, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery
from ..normalize import normalize
from ..registry import HACKS
from .base import SearchFn, TravelHack

log = logging.getLogger(__name__)

#: Systémy, jejichž tarif umíme spočítat.
SYSTEMS = {m.SYSTEM: m for m in (ideska, pid, idpk, vdv, iredo, idol, jmk, duk, idzk, idsok)}
LINKS = {name: m.URL for name, m in SYSTEMS.items()}
#: Příklady zón pro nastavení předplatného ve webovém rozhraní.
PASS_EXAMPLES = {"PID": "P, 0, B", "IDESKA": "100", "IDPK": "001", "VDV": "1", "IREDO": "600",
                 "IDOL": "0001", "IDS JMK": "100, 101", "DÚK": "101", "IDZK": "1344",
                 "IDSOK": "71, 710"}
#: Systémy, u kterých se zóny drobných zastávek nemusí dát dohledat (pásmo
#: nádraží se bere z autobusové zastávky v obci). Mezilehlá zastávka bez
#: jakýchkoli údajů tu trasu systému nepřeruší; cenu určují jen zóny konců
#: (u IDPK i projeté zóny, které známe).
LENIENT = {idpk.SYSTEM, vdv.SYSTEM, iredo.SYSTEM, idol.SYSTEM, jmk.SYSTEM, duk.SYSTEM, idzk.SYSTEM,
           idsok.SYSTEM}
#: Na těchto vlacích jízdenka IDS neplatí (dálkové a komerční spoje).
NOT_IDS = re.compile(r"^\s*(EC|IC|SC|EN|ES|Ex|rj|RJ|RJX|LE|LET|NJ|ICE|IR|AEx|TLX)\b")
#: Dálkové vlaky ČD, na které platí jízdenka jen některých IDS. IDESKA platí
#: ve 2. třídě vybraných dálkových vlaků ČD na tratích 190/191, 194, 196, 220
#: (až do Benešova u Prahy), 225 a 226 – tedy i na Ex/IC (Jižní expres) uvnitř
#: zón IDESKA; EC a railjety ne. Zdroj: cd.cz, IDESKA (2026).
LONG_DISTANCE = re.compile(r"^\s*(Ex|IC)\b")
LONG_DISTANCE_SYSTEMS = frozenset({"IDESKA"})


def train_systems(label: str) -> set[str] | None:
    """Systémy, ve kterých platí jízdenka IDS na vlak ČD; ``None`` = ve všech."""
    if LONG_DISTANCE.match(label or ""):
        return set(LONG_DISTANCE_SYSTEMS)
    if NOT_IDS.match(label or ""):
        return set()
    return None


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
    halts: bool = True              # vlak tu opravdu zastavuje (jen tady smí být lom)
    rail: bool = True               # železniční stanice (projeté stanice dohledá síť)


@dataclass
class Plan:
    """Rozpis jízdenek pro jeden vlak."""
    total: Decimal
    pieces: list[tuple[str, int, int, IdsFare | Journey]]   # (systém/dopravce, od, do, cena)


def _stop(name: str, display: str | None = None, time: dt.datetime | None = None) -> RouteStop:
    place = stations.rail_place(name)
    systems = {k: v for k, v in (place.systems if place else {}).items() if k in SYSTEMS}
    return RouteStop(display or name, time, systems, place.kraj if place else "", name)


def eligible(journey: Journey) -> bool:
    """Spoj ČD, na kterém může platit jízdenka IDS."""
    if journey.is_hack or journey.price is None or journey.provider == "IDS":
        return False             # spoje od „dopravce“ IDS už jsou za cenu IDS
    rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
    if not rides:
        return False
    for leg in rides:
        if leg.mode not in (Mode.TRAIN, Mode.BUS):
            return False
        carrier = normalize(leg.carrier or journey.provider)
        if "cd" not in carrier.split() and "ceske drahy" not in carrier:
            return False
        if train_systems(leg.line or "") == set():
            return False
    return True


def allowed_systems(journey: Journey) -> set[str] | None:
    """Systémy, které smějí platit na všech vlacích spoje (``None`` = bez omezení)."""
    allowed = None
    for leg in journey.legs:
        only = train_systems(leg.line or "") if leg.mode is Mode.TRAIN else None
        if only is not None:
            allowed = only if allowed is None else allowed & only
    return allowed


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
            estimated = [not r for r in routes]
            routes = [r or _with_times(fallback, j) for r, j in zip(routes, base)]
            for route, journey, guess in zip(routes, base, estimated):
                if guess and route:
                    known = await self._known_stops(client, journey, journeys)
                    _mark_halts(route, known)
        await self._prefetch(routes, query)

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
                self._allowed = allowed_systems(journey)
                plan = self._best_plan(query, journey, route, cd_tails, cd_heads)
            except Exception:
                log.exception("ids_ticket: vyhodnocení spoje selhalo")
                continue
            if plan is not None and self.is_worth_it(plan.total, journey.price):
                found.append(self._result(journey, route, plan))
        return found

    async def _prefetch(self, routes: list[list[RouteStop]], query: SearchQuery) -> None:
        """Systémy, jejichž ceny se zjišťují online (VDV), si je stáhnou předem.

        Ptáme se jen na dvojice zón, které může plán použít: konce trasy,
        stanice na hranicích systémů a hranice předplatného cestujících.
        """
        for system, module in SYSTEMS.items():
            if not hasattr(module, "prefetch"):
                continue
            pairs: set[tuple[str, str]] = set()
            for route in routes:
                n = len(route) - 1
                if n < 1:
                    continue
                points = {0, n} | {k for k in range(1, n) if len(route[k].systems) >= 2 and route[k].halts}
                for other in SYSTEMS:
                    for k in (self._last_inside(route, other), self._first_inside(route, other)):
                        if k is not None:
                            points.add(k)
                points = sorted(i for i in points if system in route[i].systems)
                passes = {p.pass_zones(system) for p in query.passengers} - {None}
                for a in points:
                    for b in points:
                        if a < b:
                            pairs.update((x, y) for x in route[a].systems[system]
                                         for y in route[b].systems[system])
                            for zones in passes:
                                for _, _, passed in _uncovered_runs(route, system, a, b, zones):
                                    if passed:
                                        pairs.update((x, y) for x in passed[0] for y in passed[-1])
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

    async def _known_stops(self, client: httpx.AsyncClient, journey: Journey,
                           others: Sequence[Journey]) -> set[str]:
        """Stanice, kde vlak spoje podle jízdního řádu opravdu zastavuje.

        Zdroje: zastávky v samotném spoji, tentýž vlak od jiného dopravce
        (Arriva vrací celou trasu i přes změnu čísla vlaku) a cd.cz pro
        každý úsek zvlášť (i když vlak cestou změní číslo, každé číslo zvlášť
        ukáže svůj kus trasy).
        """
        names: set[str] = set()
        rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
        for leg in rides:
            names.update(s.name for s in leg.stops)
            names.update((leg.origin, leg.destination))
        for other in others:
            if other is journey or not _close(other.departure, journey.departure) \
                    or not _close(other.arrival, journey.arrival):
                continue
            if any(leg.mode is not Mode.TRAIN for leg in other.legs if leg.mode is not Mode.WALK):
                continue
            for leg in other.legs:
                names.update(s.name for s in leg.stops)
        for leg in rides:
            for number in dict.fromkeys(_numbers(leg.line)):
                stops = await cdtrain.train_stops(client, number, leg.departure)
                names.update(s.name for s in stops if s.arrival or s.departure)
        return {_station_key(n) for n in names if n}

    # ---- plánování ----------------------------------------------------------------
    @staticmethod
    def _split_point(stop: RouteStop, system: str) -> bool:
        """Smí tu jízdenka systému končit/začínat? Známá zóna a vlak tu staví."""
        return system in stop.systems and stop.halts

    @staticmethod
    def _inside(stop: RouteStop, system: str) -> bool:
        # autobusová zastávka bez známé zóny (malá obec) trasu systému nepřeruší
        return system in stop.systems or (not stop.systems and (system in LENIENT or not stop.rail))

    @classmethod
    def _last_inside(cls, route: list[RouteStop], system: str) -> int | None:
        if system not in route[0].systems:
            return None
        k = 0
        while k + 1 < len(route) and cls._inside(route[k + 1], system):
            k += 1
        while k > 0 and not cls._split_point(route[k], system):
            k -= 1             # lom jen ve stanici se známou zónou, kde vlak staví
        return k

    @classmethod
    def _first_inside(cls, route: list[RouteStop], system: str) -> int | None:
        if system not in route[-1].systems:
            return None
        k = len(route) - 1
        while k - 1 >= 0 and cls._inside(route[k - 1], system):
            k -= 1
        while k < len(route) - 1 and not cls._split_point(route[k], system):
            k += 1
        return k

    def _ids_fare(self, query: SearchQuery, route: list[RouteStop], system: str,
                  i: int, j: int, minutes: float) -> IdsFare | None:
        part = route[i:j + 1]
        if getattr(self, "_allowed", None) is not None and system not in self._allowed:
            return None
        if system not in part[0].systems or system not in part[-1].systems:
            return None
        if any(not self._inside(s, system) for s in part):
            return None
        return group_fare(query.passengers,
                          lambda p: self._passenger_fare(route, system, i, j, minutes, p))

    def _single_fare(self, route: list[RouteStop], system: str, i: int, j: int,
                     minutes: float, passenger: Passenger) -> IdsFare | None:
        """Jízdenka jednoho cestujícího bez ohledu na předplatné."""
        part = route[i:j + 1]
        if system == pid.SYSTEM and not any(s.kraj in pid.HOME_REGIONS for s in part):
            return None        # PID neplatí na cesty jen po jiném kraji
        return _price(system, _passed_zones(part, system), passenger, minutes)

    def _passenger_fare(self, route: list[RouteStop], system: str, i: int, j: int,
                        minutes: float, passenger: Passenger) -> IdsFare | None:
        """Jízdenka cestujícího s ohledem na jeho předplatné zóny.

        Úsek uvnitř předplacených zón je zdarma; mimo ně se kupuje jízdenka
        jen na zbytek – na zóny za hranicí předplatného (i ty, kterými vlak
        jen projede). Vede-li cesta ven a zase zpět, jsou to dvě jízdenky.
        Pokud by to vyšlo dráž než jedna jízdenka na celou cestu, platí ta.
        """
        full = self._single_fare(route, system, i, j, minutes, passenger)
        zones = passenger.pass_zones(system)
        if not zones:
            return full
        first, last = route[i].systems[system][0], route[j].systems[system][0]
        runs = _uncovered_runs(route, system, i, j, zones)
        if not runs:
            return IdsFare(system, Decimal(0), None, "předplatné", first, last)
        total, pieces = Decimal(0), []
        for a, b, passed in runs:
            fare = _price(system, passed, passenger, _part_minutes(route, a, b, minutes))
            if fare is None:
                return full
            total += fare.price
            pieces.append(fare)
        if full is not None and full.price <= total:
            return full
        name = "předplatné + " + " + ".join(f"{f.name} ({_zones(f)})" for f in pieces)
        return IdsFare(system, total, min((f.minutes for f in pieces if f.minutes), default=None),
                       name, first, last)

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
        nodes = [0] + [k for k in range(1, n) if len(route[k].systems) >= 2 and route[k].halts] + [n]
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
                plans.append(Plan(fare.price + other.price, [(system, 0, k, fare), (other.provider, k, n, other)]))

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
                plans.append(Plan(fare.price + other.price, [(other.provider, 0, k, other), (system, k, n, fare)]))

        return min(plans, key=lambda p: p.total, default=None)

    # ---- výsledek -------------------------------------------------------------------
    def _result(self, journey: Journey, route: list[RouteStop], plan: Plan) -> Journey:
        segments: list[Journey] = []
        lines: list[str] = []
        for system, i, j, price in plan.pieces:
            origin, destination = route[i].name, route[j].name
            if isinstance(price, Journey):
                segment = price
                lines.append(f"{origin} → {destination}: jízdenka {segment.provider} {segment.price:.0f} Kč")
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
            rides = sum(1 for leg in journey.legs if leg.mode is not Mode.WALK)
            same = ("Nepřestupujete – stejný vlak" if rides == 1
                    else "Stejné vlaky a přestupy jako u ČD")
            note = (f"✂️ {same}, jen {len(segments)} jízdenky (lom jen ve stanicích, kde vlak staví): "
                    + "; ".join(lines) + f". Celkem {plan.total:.0f} Kč místo "
                    f"{journey.price:.0f} Kč za jednu jízdenku ČD – ušetříte {saving:.0f} Kč.")
        notes = [note, "Jízdenky IDS platí v regionálních vlacích ČD (Os, Sp, R) zařazených do "
                       "systému; koupíte je v aplikaci systému, v aplikaci ČD Můj vlak nebo u průvodčího."]
        label = " + ".join(dict.fromkeys(p[0] for p in plan.pieces))
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


def _station_key(name: str) -> str:
    """Jedna podoba jména stanice pro porovnání („Náměšť n.Oslavou“ = „Náměšť nad Oslavou“)."""
    place = stations.rail_place(name)
    return normalize(place.name if place else name)


def _numbers(line: str) -> list[str]:
    """Čísla vlaků z označení spoje („R 653“, „R 653 ➔ R 96653“)."""
    return re.findall(r"\b\d{2,6}\b", line or "")


def _mark_halts(route: list[RouteStop], known: set[str]) -> None:
    """Trasa odhadnutá ze sítě obsahuje i stanice, kterými vlak jen projíždí.

    Zastavení se počítá jen tam, kde ho potvrdil jízdní řád (``known``);
    konce cesty jsou vždy zastávky.
    """
    last = len(route) - 1
    for k, stop in enumerate(route):
        halts = k in (0, last) or _station_key(stop.node or stop.name) in known \
            or _station_key(stop.name) in known
        route[k] = replace(stop, halts=halts)


def _passed_zones(part: list[RouteStop], system: str) -> list[tuple[str, ...]]:
    """Zóny všech stanic, kterými vlak projede – i těch, kde nestaví."""
    zones: list[tuple[str, ...]] = [part[0].systems.get(system, ())]
    for a, b in zip(part, part[1:]):
        # autobus: trasa je úplná (všechny zastávky z jízdního řádu), síť se neptá
        between = (railnet.physical_route(a.node or a.name, b.node or b.name)[1:-1]
                   if a.rail and b.rail else [])
        for name in between:
            place = stations.rail_place(name)
            zones.append(place.systems.get(system, ()) if place else ())
        zones.append(b.systems.get(system, ()))
    return zones


def _price(system: str, passed: list[tuple[str, ...]], passenger: Passenger,
           minutes: float) -> IdsFare | None:
    """Nejlevnější jízdenka systému přes dané zóny (projeté zastávky v pořadí)."""
    module = SYSTEMS[system]
    if hasattr(module, "best_for_route"):        # cena podle všech projetých zón
        return module.best_for_route(passed, passenger, minutes)
    known = [z for z in passed if z]
    if not known:
        return None
    return module.best_between(known[0], known[-1], passenger, minutes)


def _covers(zones: frozenset[str], own: tuple[str, ...]) -> bool:
    """Leží zastávka v předplacených zónách? Ve dvou zónách stačí jedna z nich."""
    return "*" in zones or bool(zones.intersection(own))


def _uncovered_runs(route: list[RouteStop], system: str, i: int, j: int,
                    zones: frozenset[str]) -> list[tuple[int, int, list[tuple[str, ...]]]]:
    """Souvislé úseky trasy mimo předplatné: (od, do, projeté zóny mimo předplatné).

    Úsek začíná poslední stanicí v předplatném a končí první další v něm;
    zóny se pak oříznou na ty za hranicí předplatného, aby se počítaly i
    zóny stanic, kterými vlak jen projede.
    """
    state: list[bool] = []
    for stop in route[i:j + 1]:
        own = stop.systems.get(system)
        state.append(_covers(zones, own) if own else (state[-1] if state else False))
    runs = []
    k = 0
    while k < len(state):
        if state[k]:
            k += 1
            continue
        start = k
        while k < len(state) and not state[k]:
            k += 1
        lo, hi = i + max(start - 1, 0), i + min(k, len(state) - 1)
        passed = _passed_zones(route[lo:hi + 1], system)
        while passed and (not passed[0] or _covers(zones, passed[0])):
            passed.pop(0)
        while passed and (not passed[-1] or _covers(zones, passed[-1])):
            passed.pop()
        runs.append((i + start, i + k - 1, passed))
    return runs


def _part_minutes(route: list[RouteStop], a: int, b: int, whole: float) -> float:
    minutes = _minutes(route[a].time, route[b].time, whole)
    if route[a].estimated or route[b].estimated:
        minutes = minutes * 1.15 + 5                     # rezerva u odhadnutého času
    return min(whole, minutes)


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
        if any(leg.mode is not Mode.TRAIN for leg in option.legs if leg.mode is not Mode.WALK):
            continue          # jiný dopravce (Arriva SJT) ano, ale jen tentýž vlak
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
        if any(leg.mode is not Mode.TRAIN for leg in option.legs if leg.mode is not Mode.WALK):
            continue          # jiný dopravce (Arriva SJT) ano, ale jen tentýž vlak
        if option.price is None or not _close(option.departure, journey.departure):
            continue
        if not (journey.departure < option.arrival < journey.arrival):
            continue
        if split_time is not None and not _close(option.arrival, split_time, 300):
            continue
        if best is None or option.price < best.price:
            best = option
    return best
