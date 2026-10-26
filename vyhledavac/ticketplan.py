"""Nejlevnější skladba jízdenek na konkrétní spojení (dynamické programování).

Hacky skládají cenu spoje z dílčích jízdenek po kusech a o každém kusu
rozhodují zvlášť: IDESKA do Strakonic, IDPK do Plzně, z Plzně SJT. Celek pak
nemusí být nejlevnější – když se v Plzni přesedne na spěšný vlak místo IC,
platí v něm IDPK a síťová jízdenka IDPK ze Strakonic (240 minut) pokryje celý
zbytek cesty: 294 Kč za pětičlennou skupinu místo 294 + 602 Kč.

Spojení se tu rozloží na uzly – zastávky jízd, přestup je jeden uzel – a ze
všech kandidátů se vybere nejlevnější pokrytí celé cesty:

* dílčí jízdenky, které hack už našel (prodejci, e-shop IDOS, tarif IDS),
* jízdenky IDS podle tarifu na libovolný souvislý kus, kde systém platí –
  vlak ČD/SJT kategorie, ve které IDS platí (``ids_ticket.train_systems``),
  autobus linky systému, MHD; jízda se musí vejít do platnosti jízdenky a lom
  jen v zastávce, kde vůz staví,
* odpovědi prodejců, které už přišly (``options``), přesně na kus mezi dvěma
  uzly – tentýž odjezd i příjezd.

Na dopravce se nic znovu neptá; online tarify (VDV, IREDO, IDOL, DÚK, IDZK)
si dotáhnou ceny dvojic zón, které ještě nemají v paměti.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence

from .models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from .normalize import normalize

log = logging.getLogger(__name__)

#: Každá další jízdenka „stojí“ navíc tolik – při stejné ceně vyhraje méně jízdenek.
TICKET_PENALTY = Decimal("0.5")
#: Nová skladba se přijme, jen když ušetří aspoň tolik Kč (jinak zůstane původní).
MIN_SAVING = Decimal(1)
#: Nejvýš tolik uzlů, mezi kterými se zkoušejí jízdenky IDS (dlouhé autobusové linky).
MAX_CANDIDATES = 40
#: Tentýž spoj u prodejce: odjezd a příjezd do pár minut (zaokrouhlení jízdních řádů).
SAME_DEPARTURE = dt.timedelta(minutes=3)
SAME_ARRIVAL = dt.timedelta(minutes=6)

_LICENCE = re.compile(r"\b(\d{6})\b")


@dataclass
class Node:
    """Místo na cestě, kde může jízdenka začít nebo skončit."""

    arrive: tuple[int, int] | None = None   # (jízda, zastávka) příjezdu sem
    depart: tuple[int, int] | None = None   # (jízda, zastávka) odjezdu odsud


@dataclass
class Piece:
    """Jedna jízdenka od uzlu ``start`` do uzlu ``end``."""

    start: int
    end: int
    price: Decimal
    segment: Journey


def _stops(ride: Leg) -> list[Stop]:
    if len(ride.stops) >= 2:
        return list(ride.stops)
    return [Stop(ride.origin, departure=ride.departure), Stop(ride.destination, arrival=ride.arrival)]


class Path:
    """Spojení rozložené na uzly: zastávky jízd, přestup je jeden uzel
    (příjezd jedné jízdy a odjezd další, i když se mezi nimi jde pěšky)."""

    def __init__(self, rides: Sequence[Leg]) -> None:
        self.rides = list(rides)
        self.stops: list[list[Stop]] = [_stops(ride) for ride in self.rides]
        self.nodes: list[Node] = []
        self._index: dict[tuple[int, int], int] = {}
        for k, stops in enumerate(self.stops):
            for i in range(len(stops)):
                if k and i == 0:
                    self.nodes[-1].depart = (k, 0)
                    self._index[(k, 0)] = len(self.nodes) - 1
                    continue
                self.nodes.append(Node(arrive=(k, i) if (k or i) else None,
                                       depart=(k, i) if i < len(stops) - 1 else None))
                self._index[(k, i)] = len(self.nodes) - 1

    @property
    def last(self) -> int:
        return len(self.nodes) - 1

    def node(self, k: int, i: int) -> int:
        """Uzel zastávky ``i`` jízdy ``k`` (záporné ``i`` od konce)."""
        if i < 0:
            i += len(self.stops[k])
        return self._index[(k, i)]

    def transfers(self) -> list[int]:
        return [self.node(k, 0) for k in range(1, len(self.rides))]

    def departure(self, n: int) -> dt.datetime | None:
        pos = self.nodes[n].depart
        if pos is None:
            return None
        k, i = pos
        stop = self.stops[k][i]
        return stop.departure or stop.arrival or (self.rides[k].departure if i == 0 else None)

    def arrival(self, n: int) -> dt.datetime | None:
        pos = self.nodes[n].arrive
        if pos is None:
            return None
        k, i = pos
        stop = self.stops[k][i]
        last = i == len(self.stops[k]) - 1
        return stop.arrival or stop.departure or (self.rides[k].arrival if last else None)

    def name_from(self, n: int) -> str:
        pos = self.nodes[n].depart
        return self.stops[pos[0]][pos[1]].name if pos else ""

    def name_to(self, n: int) -> str:
        pos = self.nodes[n].arrive
        return self.stops[pos[0]][pos[1]].name if pos else ""

    def slice(self, a: int, b: int) -> list[Leg]:
        """Jízdy od uzlu ``a`` do uzlu ``b`` (krajní oříznuté na zastávky lomu)."""
        start, end = self.nodes[a].depart, self.nodes[b].arrive
        if start is None or end is None:
            return []
        (k0, i0), (k1, i1) = start, end
        out: list[Leg] = []
        for k in range(k0, k1 + 1):
            ride, stops = self.rides[k], self.stops[k]
            lo = i0 if k == k0 else 0
            hi = i1 if k == k1 else len(stops) - 1
            if lo == 0 and hi == len(stops) - 1:
                out.append(ride)
                continue
            dep = stops[lo].departure or stops[lo].arrival or ride.departure
            arr = stops[hi].arrival or stops[hi].departure or ride.arrival
            out.append(dataclasses.replace(ride, origin=stops[lo].name, destination=stops[hi].name,
                                           departure=dep, arrival=arr,
                                           stops=list(stops[lo:hi + 1]) if ride.stops else []))
        return out


# ---- jízdenky IDS -----------------------------------------------------------------
def _cd_like(carrier: str) -> bool:
    """Vlak ČD – i „jiný dopravce (SJT)“ od Arrivy: vlak v jednotném tarifu
    (objednaný státem/krajem), tedy i v IDS kraje, je-li regionální."""
    words = normalize(carrier).split()
    return "ceske drahy" in normalize(carrier) or "cd" in words or "sjt" in words


def ride_systems(ride: Leg, hint: set[str] | frozenset[str] = frozenset()) -> set[str] | None:
    """Systémy IDS, jejichž jízdenka platí na téhle jízdě; ``None`` = kterýkoli,
    rozhodnou zóny zastávek (MHD, osobní vlak). ``hint`` = systémy, které tu
    uznal už rozpis jízdenek (IDOS zná linku i bez čísla licence)."""
    from .hacks.ids_ticket import train_systems
    from .ids import odis
    from .providers.ids_idos import line_systems

    if ride.mode is Mode.BUS:
        found = _LICENCE.search(ride.line or "")
        systems = line_systems(found.group(1), set()) if found else set()
        if systems is None:
            return None
        return set(systems) | set(hint)
    if ride.mode is Mode.TRAIN:
        carrier = ride.carrier or ""
        systems = train_systems(ride.line or "")
        if not _cd_like(carrier):
            odis_ok = systems is None and any(c in carrier for c in odis.TRAIN_CARRIERS)
            systems = {odis.SYSTEM} if odis_ok else set()
        if systems is None:
            return None
        return set(systems) | set(hint)
    if ride.mode in (Mode.TRAM, Mode.METRO, Mode.FERRY):
        return None
    return set(hint)


def route_stops(query: SearchQuery, rides: Sequence[Sequence[Stop]], modes: Sequence[Leg]) -> list[list]:
    """Zastávky jízd se zónami IDS. Stejnojmenné obce rozliší sousední
    zastávky: „Štěpánovice“ na lince Třeboň → Budějovice jsou ty u Lišova
    (IDESKA), ne jihomoravské (IDS JMK)."""
    from . import city
    from .hacks.ids_ticket import SYSTEMS, RouteStop, limit_region
    from .providers.ids_idos import _place

    towns = tuple(dict.fromkeys(c[0] for c in (city.city_of(query.origin), city.city_of(query.destination))
                                if c is not None and not c[1]))
    near: set[str] = set()
    out: list[list] = []
    for stops, ride in zip(rides, modes):
        rail = ride.mode is Mode.TRAIN
        row = []
        for i, stop in enumerate(stops):
            place = _place(stop.name, rail, near, towns)
            if place is not None and place.systems:
                near = set(place.systems)
            systems = {s: z for s, z in (place.systems if place else {}).items() if s in SYSTEMS}
            end = i in (0, len(stops) - 1)
            row.append(RouteStop(stop.name, stop.departure or stop.arrival, systems,
                                 place.kraj if place else "", stop.name if rail else "",
                                 halts=end or stop.arrival is not None or stop.departure is not None,
                                 rail=rail))
        if rail:
            limit_region(row, ride.line or "")       # EC Jižní expres: IDESKA jen v jižních Čechách
        out.append(row)
    return out


def _candidates(path: Path, stops: list[list], keep: set[int]) -> list[int]:
    """Uzly, mezi kterými se zkoušejí jízdenky IDS: konce, přestupy, konce kusů
    z rozpisu a zastávky, kde se mění systémy (hranice zón a krajů)."""
    must = {0, path.last, *path.transfers(), *keep}
    extra: list[int] = []
    for k, row in enumerate(stops):
        for i, stop in enumerate(row):
            prev = set(row[i - 1].systems) if i else None
            nxt = set(row[i + 1].systems) if i + 1 < len(row) else None
            here = set(stop.systems)
            if len(here) >= 2 or (prev is not None and here != prev) or (nxt is not None and here != nxt):
                extra.append(path.node(k, i))
    chosen = sorted(must | set(extra))
    if len(chosen) > MAX_CANDIDATES:
        rest = sorted(set(extra) - must)
        step = max(1, len(rest) // max(1, MAX_CANDIDATES - len(must)))
        chosen = sorted(must | set(rest[::step]))
    return chosen


def _route_between(path: Path, stops: list[list], a: int, b: int, system: str,
                   allowed: list[set[str] | None], inside: Callable) -> list | None:
    """Zastávky jízdenky systému z uzlu ``a`` do ``b``; ``None``, když tam neplatí."""
    start, end = path.nodes[a].depart, path.nodes[b].arrive
    if start is None or end is None:
        return None
    (k0, i0), (k1, i1) = start, end
    route = []
    for k in range(k0, k1 + 1):
        if allowed[k] is not None and system not in allowed[k]:
            return None
        lo = i0 if k == k0 else 0
        hi = i1 if k == k1 else len(stops[k]) - 1
        for i in range(lo, hi + 1):
            stop = stops[k][i]
            if not inside(stop, system):
                return None
            if route and i == lo and normalize(route[-1].name) == normalize(stop.name):
                continue                     # přestup ve stejné zastávce – jednou
            route.append(stop)
    return route if len(route) >= 2 else None


async def ids_pieces(query: SearchQuery, path: Path, hints: Sequence[set[str]] = (),
                     keep: set[int] | frozenset[int] = frozenset()) -> list[Piece]:
    """Jízdenky IDS podle tarifu na souvislé kusy cesty, kde systém platí."""
    from .hacks.ids_ticket import SYSTEMS, IdsTicketHack

    allowed = [ride_systems(ride, set(hints[k]) if k < len(hints) else set())
               for k, ride in enumerate(path.rides)]
    if all(a == set() for a in allowed):
        return []
    stops = route_stops(query, path.stops, path.rides)
    present = {s for row in stops for stop in row for s in stop.systems}
    if not present:
        return []
    nodes = _candidates(path, stops, set(keep))
    hack = IdsTicketHack()

    def side(n: int, depart: bool):
        pos = path.nodes[n].depart if depart else path.nodes[n].arrive
        return stops[pos[0]][pos[1]] if pos is not None else None

    out: list[Piece] = []
    for system in sorted(present):
        module = SYSTEMS[system]
        pairs: list[tuple[int, int, list]] = []
        for x, a in enumerate(nodes):
            first = side(a, True)
            if first is None or system not in first.systems or not first.halts:
                continue
            for b in nodes[x + 1:]:
                last = side(b, False)
                if last is None or system not in last.systems or not last.halts:
                    continue
                route = _route_between(path, stops, a, b, system, allowed, hack._inside)
                if route is None:
                    if not _route_between(path, stops, a, b, system, allowed, lambda s, y: True):
                        break                # systém dál neplatí ani na jízdě – dál nemá smysl
                    continue
                pairs.append((a, b, route))
        if not pairs:
            continue
        if hasattr(module, "prefetch"):      # online tarify: ceny dvojic zón předem
            try:
                await module.prefetch({(z1, z2) for _, _, route in pairs
                                       for z1 in route[0].systems.get(system, ())
                                       for z2 in route[-1].systems.get(system, ())})
            except Exception:
                log.exception("ticketplan: ceny %s se nepodařilo stáhnout", system)
        for a, b, route in pairs:
            dep, arr = path.departure(a), path.arrival(b)
            if dep is None or arr is None or arr < dep:
                continue
            minutes = (arr - dep).total_seconds() / 60
            try:
                fare = hack._ids_fare(query, route, system, 0, len(route) - 1, minutes)
            except Exception:
                log.exception("ticketplan: tarif %s selhal", system)
                continue
            if fare is not None:
                out.append(Piece(a, b, fare.price, ids_segment(path.slice(a, b), fare)))
    return out


def ids_segment(legs: list[Leg], fare) -> Journey:
    """Dílčí jízdenka IDS podle tarifu (pro výpis a součet ceny)."""
    from .hacks.ids_ticket import LINKS

    zones = fare.zone_from if fare.zone_from == fare.zone_to else f"{fare.zone_from}→{fare.zone_to}"
    return Journey(
        provider=fare.system, legs=legs, fare_status=FareStatus.PRICED,
        fares=[Fare(amount=fare.price, currency="CZK", amount_czk=fare.price, label=fare.system,
                    tariff=f"{fare.name}, zóny {zones}")],
        deep_link=LINKS.get(fare.system),
        notes=[f"Jízdenka {fare.system} ({fare.name}) podle tarifu – koupíte v aplikaci systému, "
               "v automatu, u řidiče nebo ve vlaku ČD (i v aplikaci Můj vlak)."],
        raw={"ids_tariff": True},
    )


# ---- odpovědi prodejců na kusy cesty ----------------------------------------------------
def option_pieces(path: Path, options: Sequence[Journey],
                  same_place: Callable[[str, str], bool]) -> list[Piece]:
    """Odpovědi prodejců přesně na kus mezi dvěma uzly: tentýž odjezd i příjezd
    (Plzeň → Mariánské Lázně spěšným vlakem, Linz → Salzburg u ÖBB)."""
    departures = [(n, path.departure(n)) for n in range(len(path.nodes)) if path.nodes[n].depart]
    arrivals = [(n, path.arrival(n)) for n in range(len(path.nodes)) if path.nodes[n].arrive]
    out: list[Piece] = []
    seen: set[tuple[int, int, str, Decimal]] = set()
    for option in options:
        if option.price is None or not option.legs or option.is_hack:
            continue
        rides = [leg for leg in option.legs if leg.mode is not Mode.WALK] or option.legs
        starts = [n for n, when in departures if when is not None
                  and abs(rides[0].departure - when) <= SAME_DEPARTURE
                  and same_place(rides[0].origin, path.name_from(n))]
        if not starts:
            continue
        ends = [n for n, when in arrivals if when is not None
                and abs(rides[-1].arrival - when) <= SAME_ARRIVAL
                and same_place(rides[-1].destination, path.name_to(n))]
        for a in starts:
            for b in ends:
                key = (a, b, option.provider, option.price)
                if a < b and key not in seen:
                    seen.add(key)
                    out.append(Piece(a, b, option.price, on_path(option, path.slice(a, b))))
    return out


def on_path(option: Journey, legs: list[Leg]) -> Journey:
    """Jízdenka prodejce na kus cesty – úseky z cesty (zastávky, dopravci úseků)."""
    provider = option.provider
    if provider == "IDS" and option.fares and option.fares[0].label == "IDOS":
        provider = "ČD"                      # nabídka e-shopu IDOS je jízdenka ČD
    copy = Journey(provider=provider, legs=list(legs), fares=list(option.fares),
                   fare_status=option.fare_status, deep_link=option.deep_link, notes=list(option.notes),
                   raw=dict(option.raw))
    copy.raw.pop("other_route", None)
    return copy


# ---- výběr ---------------------------------------------------------------------------
def cheapest(path: Path, pieces: Sequence[Piece]) -> list[Piece] | None:
    """Nejlevnější řada jízdenek od začátku do konce cesty (``None`` = nejde pokrýt)."""
    by_end: dict[int, list[Piece]] = defaultdict(list)
    for piece in pieces:
        if 0 <= piece.start < piece.end <= path.last and piece.price is not None:
            by_end[piece.end].append(piece)
    best: dict[int, tuple[Decimal, Piece | None]] = {0: (Decimal(0), None)}
    for b in range(1, path.last + 1):
        for piece in by_end.get(b, ()):
            if piece.start not in best:
                continue
            cost = best[piece.start][0] + piece.price + TICKET_PENALTY
            if b not in best or cost < best[b][0]:
                best[b] = (cost, piece)
    if path.last not in best:
        return None
    plan: list[Piece] = []
    n = path.last
    while n:
        piece = best[n][1]
        assert piece is not None
        plan.append(piece)
        n = piece.start
    return plan[::-1]


def reach(path: Path, pieces: Sequence[Piece]) -> tuple[int, int]:
    """Kam až dojede řetěz jízdenek ``pieces`` od začátku cesty (nejvzdálenější
    uzel) a odkud dojede do cíle (nejbližší uzel) – tam se vyplatí zeptat
    prodejců na zbytek (začátek) cesty."""
    forward = {0}
    for piece in sorted(pieces, key=lambda p: p.start):
        if piece.start in forward:
            forward.add(piece.end)
    backward = {path.last}
    for piece in sorted(pieces, key=lambda p: -p.end):
        if piece.end in backward:
            backward.add(piece.start)
    return max(forward), min(backward)


def total(segments: Sequence[Journey]) -> Decimal | None:
    prices = [s.price for s in segments]
    return None if any(p is None for p in prices) else sum(prices, Decimal(0))


async def improve(query: SearchQuery, segments: Sequence[Journey], *,
                  options: Sequence[Journey] = (),
                  same_place: Callable[[str, str], bool] | None = None) -> list[Journey]:
    """Levnější skladba jízdenek na tutéž cestu (tytéž spoje), jinak původní.

    Dílčí jízdenka bez ceny nevadí – když ji nahradí jiné (tarif IDS na
    autobus, který žádný e-shop neprodá), vrátí se cesta s cenou celá."""
    from .hacks.ids_ticket import SYSTEMS

    if any(segment.raw.get("linked") for segment in segments):
        return list(segments)                # jízdenka na víc oddělených úseků – nedělit
    rides: list[Leg] = []
    spans: list[tuple[int, int, Journey]] = []
    for segment in segments:
        own = [leg for leg in segment.legs if leg.mode is not Mode.WALK]
        if not own:
            return list(segments)            # jízdenka bez jízdy – nevíme, kam patří
        spans.append((len(rides), len(rides) + len(own) - 1, segment))
        rides.extend(own)
    if not rides:
        return list(segments)
    path = Path(rides)
    pieces: list[Piece] = []
    hints: list[set[str]] = [set() for _ in rides]
    keep: set[int] = set()
    for first, last, segment in spans:
        a, b = path.node(first, 0), path.node(last, -1)
        keep |= {a, b}
        if segment.price is not None:
            pieces.append(Piece(a, b, segment.price, segment))
        if segment.provider in SYSTEMS:
            for k in range(first, last + 1):
                hints[k].add(segment.provider)
    try:
        pieces += await ids_pieces(query, path, hints, keep)
    except Exception:
        log.exception("ticketplan: jízdenky IDS se nepodařilo spočítat")
    if options and same_place is not None:
        pieces += option_pieces(path, options, same_place)
    plan = cheapest(path, pieces)
    if plan is None:
        return list(segments)
    old = total(segments)
    new = sum((p.price for p in plan), Decimal(0))
    if old is not None and (new > old - MIN_SAVING or len(plan) == len(segments) and new >= old):
        return list(segments)
    return [p.segment for p in plan]
