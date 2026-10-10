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
from decimal import Decimal
from pathlib import Path
from typing import Any

from .. import idos, railnet, stations
from ..errors import StationNotFoundError, TransientProviderError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

#: Od jaké ceny IDS na cestujícího (Kč) se u spoje s vlakem ČD zkusí i jízdenka ČD.
TRAIN_CHECK_FROM = Decimal(80)
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


def _place(name: str, rail: bool, near: set[str]):
    """Místo podle jména; u stejnojmenných obcí (Domanín u Třeboně × Domanín
    na Moravě) vyhraje to, které leží ve stejném systému jako sousední zastávky."""
    from ..normalize import normalize

    db = stations.database()
    if rail:
        place = stations.rail_place(name)
        if place is not None:
            return place
    if db is not None and near:
        hits = db.exact.get(normalize(name), [])
        same = [p for p in hits if set(p.systems) & near]
        if same:
            return max(same, key=lambda p: (p.kind != "b", p.score))
    return stations.place(name)


@PROVIDERS.register()
class IdsIdosProvider(HttpProvider):
    name = "IDS"
    label = "Integrované systémy – vlaky i autobusy z IDOS, cena podle tarifu IDS"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ",)
    referer = "https://idos.cz/"
    min_interval = 0.7
    hack_legs = False        # na dílčí úseky hacků by IDOS dostával desítky dotazů

    @staticmethod
    def _common_systems(query: SearchQuery) -> set[str]:
        from ..hacks.ids_ticket import SYSTEMS

        a, b = stations.place(query.origin), stations.place(query.destination)
        if a is None or b is None or a.country or b.country:
            return set()
        return set(a.systems) & set(b.systems) & set(SYSTEMS)

    @staticmethod
    def _worth_asking(query: SearchQuery) -> bool:
        """IDOS se ptáme, když jde o cestu v jednom IDS, nebo když je některý
        konec obyčejná zastávka / obec bez nádraží – tu ostatní dopravci neznají."""
        a, b = stations.place(query.origin), stations.place(query.destination)
        if a is None or b is None or a.country or b.country:
            return False
        if IdsIdosProvider._common_systems(query):
            return True
        return any(p.kind == "b" or railnet.station_for(name) is None
                   for p, name in ((a, query.origin), (b, query.destination)))

    async def search(self, query: SearchQuery) -> list[Journey]:
        if not self._worth_asking(query):
            raise StationNotFoundError(self.name, "spoj mezi nádražími najdou jiní dopravci")
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
        """Ocení spoj jízdenkami IDS – celý, nebo po úsecích; zbytek bez ceny.

        Úseky, kde IDS neplatí (dálkový autobus, EC/IC, RegioJet…), zůstanou
        bez ceny. Spoj pak nese v ``raw["plans"]`` několik rozpisů jízdenek
        (IDS po celých úsecích / IDS co nejdál, i uprostřed vlaku v zastávce,
        kde staví) a hack ``idos_complete`` dokoupí zbytek u dopravců a vybere
        nejlevnější rozpis.
        """
        from ..hacks.ids_ticket import NOT_IDS, SYSTEMS, RouteStop

        infos = []          # pro každý úsek: (stops, povolené systémy | None = podle zón, IDS platí?)
        route_systems: list[dict] = []
        legs: list[Leg] = []
        for leg in conn.legs:
            raw_stops = [(leg.origin, "", ""), (leg.destination, "", "")]
            ok = True
            if leg.is_bus or leg.is_city:
                systems = set()
                if leg.detail_url:
                    page = await self._detail(leg.detail_url)
                    licence, tagged = idos.parse_line_info(page)
                    found = line_systems(licence or leg.licence, tagged)
                    systems = found if found is not None else None
                    raw_stops = idos.parse_stops(page) or raw_stops
                ok = systems is None or bool(systems)
                rail = False
            else:
                ok = not NOT_IDS.match(leg.train_label) and "České dráhy" in leg.carrier
                systems = None
                rail = True
                if ok and leg.detail_url:       # zastávky vlaku – jen v nich smí být lom jízdenky
                    try:
                        raw_stops = idos.parse_stops(await self._detail(leg.detail_url)) or raw_stops
                    except TransientProviderError:
                        pass
            if len(raw_stops) < 2:
                raw_stops = [(leg.origin, "", ""), (leg.destination, "", "")]
            stops = []
            near: set[str] = set(route_systems[-1]) if route_systems else set()
            last = len(raw_stops) - 1
            prev = leg.departure
            for i, (name, arr, dep) in enumerate(raw_stops):
                place = _place(name, rail, near)
                if place is not None and place.systems:
                    near = set(place.systems)
                zones = {k: v for k, v in (place.systems if place else {}).items() if k in SYSTEMS}
                if i == 0:
                    time = leg.departure
                elif i == last:
                    time = leg.arrival
                else:
                    time = idos._at(leg.departure.date(), dep or arr, prev)
                    prev = time or prev
                stops.append(RouteStop(idos.display_name(name), time, zones,
                                       place.kraj if place else "",
                                       idos.display_name(name) if rail else "", rail=rail))
            infos.append((stops, systems, ok))
            route_systems.extend(st.systems for st in stops if st.systems)
            legs.append(Leg(origin=idos.display_name(leg.origin),
                            destination=idos.display_name(leg.destination),
                            departure=leg.departure, arrival=leg.arrival,
                            mode=Mode.TRAM if leg.is_city else Mode.BUS if leg.is_bus else Mode.TRAIN,
                            carrier=leg.carrier, line=leg.train_label,
                            stops=[Stop(name=s.name, arrival=s.time if k else None,
                                        departure=s.time if k < len(stops) - 1 else None)
                                   for k, s in enumerate(stops)]))

        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=conn.share_url or idos.results_url())
        plans = self._plans(query, infos)
        whole = [plan for plan in plans if all(seg["kind"] == "ids" for seg in plan)]
        if whole:
            plan = min(whole, key=lambda pl: sum(Decimal(seg["price"]) for seg in pl))
            total = sum((Decimal(seg["price"]) for seg in plan), Decimal(0))
            tickets = describe(legs, plan)
            journey.fares = [self.make_fare(total, "CZK", label=" + ".join(dict.fromkeys(seg["system"] for seg in plan)),
                                            tariff=tickets)]
            journey.fare_status = FareStatus.PRICED
            journey.notes.append(f"🎫 Jízdenky IDS: {tickets} – cena podle tarifu systému; koupíte je "
                                 "v aplikaci systému, u řidiče nebo v automatu. Spoj z IDOS.")
            # Delší cesta vlakem ČD: jízdenka ČD (se slevou, akční) může vyjít
            # levněji než IDS – hack idos_complete ji zkusí, porovná a levnější nabídne.
            if total >= TRAIN_CHECK_FROM * len(query.passengers):
                trains = self._trains_bought(query, infos)
                if trains:
                    journey.raw = {"plans": trains}
            return journey
        first = plans[0]
        priced = [seg for seg in first if seg["kind"] == "ids"]
        missing = ", ".join(f"{seg_name(legs, seg)}" for seg in first if seg["kind"] == "buy")
        journey.notes.append("Spoj z IDOS. " + (f"Jízdenky IDS: {describe(legs, priced)}. " if priced else "")
                             + f"Cenu úseku {missing} IDS nezná – je potřeba jízdenka dopravce.")
        journey.raw = {"plans": plans}
        return journey

    @staticmethod
    def _trains_bought(query: SearchQuery, infos) -> list[list[dict]]:
        """Rozpis, kde se vlaky ČD koupí celé u dopravce a IDS jen zbytek (autobusy)."""
        if not any(st and st[0].rail and ok for st, _, ok in infos):
            return []
        no_trains = [(st, systems, ok and not (st and st[0].rail)) for st, systems, ok in infos]
        coarse = [(k, 0, len(st) - 1) for k, (st, _, _) in enumerate(infos)]
        return [_cover(query, no_trains, coarse)]

    @staticmethod
    def _plans(query: SearchQuery, infos) -> list[list[dict]]:
        """Rozpisy jízdenek spoje: úseky IDS (s cenou) a úseky „buy“ k dokoupení.

        * po celých úsecích (vlak/autobus se nedělí) – nejméně jízdenek;
        * nejlevnější pokrytí IDS – úsek se smí rozdělit v zastávce, kde vozidlo staví
          (Domanín → Tábor jízdenkou IDESKA i rychlíkem, dál do Prahy ČD);
        * IDS co nejdál jen na začátku cesty, zbytek vlaku jednou jízdenkou.

        Pozice v rozpisu jsou ``[úsek, index zastávky]`` do ``Leg.stops``.
        """
        coarse = [(k, 0, len(st) - 1) for k, (st, _, _) in enumerate(infos)]
        plans = [_cover(query, infos, coarse)]
        # i když jde spoj celý v IDS: lom v zastávce může vyjít levněji
        fine = [(k, i, i + 1) if ok else (k, 0, len(st) - 1)
                for k, (st, _, ok) in enumerate(infos)
                for i in (range(len(st) - 1) if ok else [0])]
        if len(fine) > len(coarse):
            detailed = _cover(query, infos, fine)
            plans.append(detailed)
            if any(seg["kind"] == "buy" for seg in detailed):
                plans.append(_head_only(detailed, infos))
        plans += IdsIdosProvider._trains_bought(query, infos)
        unique: list[list[dict]] = []
        for plan in plans:
            if plan not in unique:
                unique.append(plan)
        return unique


def _route(infos, atoms) -> list:
    """Zastávky po sobě jdoucích atomů (přestupní zastávka jen jednou v rámci úseku)."""
    route = []
    for k, i, j in atoms:
        stops = infos[k][0][i:j + 1]
        if route and route[-1] is stops[0]:
            stops = stops[1:]
        route.extend(stops)
    return route


def _cover(query: SearchQuery, infos, atoms) -> list[dict]:
    """Nejlevnější pokrytí jízdenkami IDS (dynamické programování přes atomy).

    Minimalizuje nejdřív počet atomů, které IDS nepokryje (ty se musí
    dokoupit), pak cenu jízdenek IDS. Projde i kombinace dvou systémů
    v přesahu (IDESKA do Tábora + PID do Prahy vs. IDESKA do Benešova + PID).
    """
    from ..hacks.ids_ticket import IdsTicketHack, SYSTEMS

    hack = IdsTicketHack()
    n = len(atoms)
    INF = (10 ** 9, Decimal(10 ** 9))
    best: list[tuple] = [INF] * (n + 1)
    step: list[tuple | None] = [None] * (n + 1)
    best[0] = (0, Decimal(0))
    for a in range(n):
        if best[a] == INF:
            continue
        # atom nepokrytý IDS
        cand = (best[a][0] + 1, best[a][1])
        if cand < best[a + 1]:
            best[a + 1], step[a + 1] = cand, (a, None)
        for b in range(a, n):
            k = atoms[b][0]
            if not infos[k][2]:
                break
            part = atoms[a:b + 1]
            route = _route(infos, part)
            allowed = set(SYSTEMS)
            for kk in {kk for kk, _, _ in part}:
                if infos[kk][1] is not None:
                    allowed &= set(infos[kk][1])
            allowed &= set(route[0].systems) & set(route[-1].systems)
            if not allowed:
                continue
            start, end = route[0].time, route[-1].time
            minutes = (end - start).total_seconds() / 60 if start and end else 60.0
            fares = [f for f in (hack._ids_fare(query, route, sys_, 0, len(route) - 1, minutes)
                                 for sys_ in sorted(allowed)) if f is not None]
            if not fares:
                continue
            fare = min(fares, key=lambda f: f.price)
            cand = (best[a][0], best[a][1] + fare.price)
            if cand < best[b + 1]:
                best[b + 1], step[b + 1] = cand, (a, fare)
    chosen = []
    pos = n
    while pos > 0:
        a, fare = step[pos]
        chosen.append((a, pos - 1, fare))
        pos = a
    plan: list[dict] = []
    for a, b, fare in reversed(chosen):
        k, i, j = atoms[a]
        if fare is None:
            if plan and plan[-1]["kind"] == "buy" and (plan[-1]["to"] == [k, i] or (
                    plan[-1]["to"][0] == k - 1 and i == 0
                    and plan[-1]["to"][1] == len(infos[k - 1][0]) - 1)):
                plan[-1]["to"] = [k, j]
            else:
                plan.append({"kind": "buy", "from": [k, i], "to": [k, j]})
            continue
        plan.append({"kind": "ids", "from": [k, i], "to": [atoms[b][0], atoms[b][2]],
                     "price": str(fare.price), "system": fare.system, "name": fare.name})
    return plan


def _head_only(plan: list[dict], infos) -> list[dict]:
    """Rozpis s IDS co nejdál na začátku, ale zbytek úseku (vlaku), kde začíná
    první dokupovaný kus, jednou jízdenkou – bez dalších lomů."""
    out: list[dict] = []
    for n, seg in enumerate(plan):
        if seg["kind"] != "buy":
            out.append(dict(seg))
            continue
        k = seg["from"][0]
        end = [k, len(infos[k][0]) - 1]
        out.append({"kind": "buy", "from": seg["from"], "to": end})
        for rest in plan[n + 1:]:
            if rest["from"][0] > k:
                out.append(dict(rest))
            elif rest["to"][0] > k:          # kus začíná v tomtéž vlaku a pokračuje dál
                out.append({"kind": "buy", "from": [k + 1, 0], "to": rest["to"]})
        break
    return out


def seg_name(legs: list[Leg], seg: dict) -> str:
    (k0, i0), (k1, i1) = seg["from"], seg["to"]
    lines = " + ".join(dict.fromkeys(legs[k].line for k in range(k0, k1 + 1)))
    return f"{legs[k0].stops[i0].name} → {legs[k1].stops[i1].name} ({lines})"


def describe(legs: list[Leg], plan: list[dict]) -> str:
    return "; ".join(f"{legs[s['from'][0]].stops[s['from'][1]].name} → {legs[s['to'][0]].stops[s['to'][1]].name}: "
                     f"{s['system']} ({s['name']}) {Decimal(s['price']):.0f} Kč" for s in plan if s["kind"] == "ids")
