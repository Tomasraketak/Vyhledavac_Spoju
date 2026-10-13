"""Spoje integrovaných systémů z IDOS – i autobusy, které web ČD neumí.

Jízdenku IDS na autobus (IDESKA, PID, IDS JMK …) žádný e-shop, na který se
ptáme, nenabídne – ČD hledá jen vlaky. IDOS ale zná všechny spoje včetně
autobusů a MHD. Tenhle „dopravce“ tedy:

1. Zeptá se jen, když obě místa leží ve stejném integrovaném systému,
   který umíme spočítat (zóny míst z databáze zastávek).
2. Načte z IDOS spojení (``vyhledavac.idos``) – oceňuje první 3 spoje do
   4:15 h od odjezdu (cena potřebuje detail každého spoje).
3. U každého úseku ověří, že platí jízdenka IDS: vlak ČD kategorie Os/Sp/R
   (Ex/IC jen IDESKA na vybraných tratích, EC/railjet ne), autobus/MHD jen linka zařazená do systému – podle
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

from .. import geo, idos, railnet, stations, transitous
from ..errors import BlockedError, RateLimitedError, StationNotFoundError, TransientProviderError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import normalize, same_place
from ..registry import PROVIDERS
from ..trace import trace
from .base import PRICE_SCOPE_AHEAD, PRICE_SCOPE_COUNT, HttpProvider

log = logging.getLogger(__name__)

#: Od jaké ceny IDS na cestujícího (Kč) se u spoje s vlakem ČD zkusí i jízdenka ČD.
TRAIN_CHECK_FROM = Decimal(80)
LINES = Path(__file__).resolve().parent.parent / "data" / "ids_lines.json.gz"
PAGES = 2
DETAIL_TTL = 24 * 3600
NAME_TTL = 30 * 24 * 3600


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
    label = "IDOS – spoje vlaků a autobusů (i v cizině), cena IDS podle tarifu, zbytek ocení dopravci"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ",)
    czech_names = True
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
        """IDOS se ptáme vždy, když obě místa známe – i na cesty do ciziny
        (Domanín → Bad Ischl) a po cizině (Stockholm → Amsterdam, Bukurešť →
        Graz): IDOS zná vlaky celé Evropy, FlixBus i RegioJet a dopravce
        každého úseku. Domácí část ocení IDS, zbytek dokoupí hack
        ``idos_complete`` u dopravců."""
        a, b = stations.place(query.origin), stations.place(query.destination)
        return a is not None and b is not None

    async def _idos_name(self, name: str) -> str:
        """Jméno pro IDOS: české místo beze změny, zahraniční město jednoznačně
        z našeptávače IDOS („Košice; Slovensko“, „Kraków; Polsko“) – samotné
        „Košice“ IDOS odmítne jako nejednoznačné."""
        place = stations.place(name)
        if place is None or not place.code:
            return name
        key = f"idos-name:{normalize(name)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        chosen = name
        for text in dict.fromkeys((place.name, name, place.local)):
            if not text:
                continue
            page = await self.client.get_text(idos.SUGGEST, params=idos.suggest_params(text), referer=self.referer)
            found = idos.pick_foreign(idos.parse_suggest(page), place.country)
            if found:
                chosen = found
                break
        self.cache.set(self.name, key, chosen, ttl=NAME_TTL)
        return chosen

    @staticmethod
    def _international(query: SearchQuery) -> bool:
        from .. import geo

        return geo.leaves_czechia(query.origin, query.destination)

    async def search(self, query: SearchQuery) -> list[Journey]:
        if not self._worth_asking(query):
            raise StationNotFoundError(self.name, "místo IDOS nezná")
        try:
            connections = await self._idos(query)
        except (BlockedError, RateLimitedError, TransientProviderError) as exc:
            # IDOS nejde (limit, výpadek) – stejná spojení z otevřených dat přes Transitous
            self.note(f"IDOS neodpověděl ({exc.message}) – spoje z Transitous (data CIS JŘ).")
            connections = await transitous.search(self.client, self.cache, self.name, query.origin,
                                                  query.destination, query.departure, query.window_end)
        # cena = detail každého spoje (zóny, linka) – jen první spoje jako u ostatních
        until = query.departure + PRICE_SCOPE_AHEAD
        connections = sorted((c for c in connections if c.departure <= until),
                             key=lambda c: (c.departure, c.arrival))[:PRICE_SCOPE_COUNT]
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

    async def _idos(self, query: SearchQuery) -> list[idos.IdosConnection]:
        connections: list[idos.IdosConnection] = []
        when = query.departure
        origin, destination = await self._idos_name(query.origin), await self._idos_name(query.destination)
        for _ in range(PAGES):
            if len(connections) >= PRICE_SCOPE_COUNT:
                break
            page = await self.client.get_text(idos.results_url(_combination(query)),
                                              params=idos.params(origin, destination, when),
                                              referer=self.referer)
            batch = [c for c in idos.parse_results(page, when.astimezone(query.departure.tzinfo).date())
                     if c.departure >= query.departure - dt.timedelta(minutes=1)]
            new = []
            for c in batch:          # IDOS někdy vypíše tentýž spoj dvakrát
                if all(c.departure != o.departure or c.legs[0].line != o.legs[0].line
                       for o in connections + new):
                    new.append(c)
            if not new:
                break
            connections.extend(new)
            when = max(c.departure for c in new) + dt.timedelta(minutes=1)
            if when > query.window_end:
                break
        return connections

    async def _detail(self, url: str) -> str:
        cached = self.cache.get(self.name, f"detail:{url}")
        if cached is not None:
            return cached
        page = await self.client.get_text(url, referer=self.referer)
        self.cache.set(self.name, f"detail:{url}", page, ttl=DETAIL_TTL)
        return page

    async def _priced(self, query: SearchQuery, conn: idos.IdosConnection) -> Journey | None:
        """Ocení spoj jízdenkami IDS – celý, nebo po úsecích; zbytek bez ceny.

        Úseky, kde IDS neplatí (dálkový autobus, EC, RegioJet…), zůstanou
        bez ceny. Spoj pak nese v ``raw["plans"]`` několik rozpisů jízdenek
        (IDS po celých úsecích / IDS co nejdál, i uprostřed vlaku v zastávce,
        kde staví) a hack ``idos_complete`` dokoupí zbytek u dopravců a vybere
        nejlevnější rozpis.
        """
        from ..hacks.ids_ticket import SYSTEMS, RouteStop, train_systems

        from ..ids import odis

        infos = []          # pro každý úsek: (stops, povolené systémy | None = podle zón, IDS platí?)
        route_systems: list[dict] = []
        legs: list[Leg] = []
        international = self._international(query)
        offset: float | None = 0.0          # kilometráž celé cesty (tarif ODIS)
        for leg in conn.legs:
            raw_stops = [(leg.origin, "", ""), (leg.destination, "", "")]
            distances: list[float | None] = []
            ok = True
            if leg.is_bus or leg.is_city:
                systems = set()
                if leg.detail_url:
                    page = await self._detail(leg.detail_url)
                    licence, tagged = idos.parse_line_info(page)
                    found = line_systems(licence or leg.licence, tagged)
                    systems = found if found is not None else None
                    raw_stops = idos.parse_stops(page) or raw_stops
                    distances = idos.parse_distances(page)
                elif leg.inline_stops:      # spoj z Transitous: linka podle licence z JDF
                    found = line_systems(leg.licence, set()) if leg.licence else None
                    systems = found if found is not None else None
                    raw_stops = leg.inline_stops
                ok = systems is None or bool(systems)
                rail = False
            else:
                systems = train_systems(leg.train_label)       # IC/Ex jen IDESKA
                if "České dráhy" not in leg.carrier:
                    # ODIS platí i ve vlacích GW Train Regio, MBM rail a RegioJet (R8)
                    odis_ok = systems is None and any(c in leg.carrier for c in odis.TRAIN_CARRIERS)
                    systems = {odis.SYSTEM} if odis_ok else set()
                ok = systems != set()
                rail = True
                crosses = international and _crosses_border(leg.origin, leg.destination)
                if ok and leg.inline_stops:
                    raw_stops = leg.inline_stops
                elif (ok or crosses) and leg.detail_url:
                    # zastávky vlaku – jen v nich smí být lom jízdenky (i na hranici státu)
                    try:
                        page = await self._detail(leg.detail_url)
                        raw_stops = idos.parse_stops(page) or raw_stops
                        distances = idos.parse_distances(page)
                    except TransientProviderError:
                        pass
            if len(raw_stops) < 2:
                raw_stops = [(leg.origin, "", ""), (leg.destination, "", "")]
            if offset is None or len(distances) != len(raw_stops) or None in distances:
                distances = [None] * len(raw_stops)
            else:
                start = distances[0]
                distances = [offset + d - start for d in distances]
            offset = distances[-1]
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
                                       idos.display_name(name) if rail else "", rail=rail,
                                       km=distances[i]))
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
        source = "IDOS" if conn.share_url or any(leg.detail_url for leg in conn.legs) \
            else "Transitous (data CIS JŘ)"
        plans = self._plans(query, infos)
        offer = await self._offer_plan(query, conn, legs, infos)
        if offer:
            plans.insert(0, offer)
        whole = [plan for plan in plans if all(seg["kind"] in ("ids", "offer") for seg in plan)]
        if whole:
            plan = min(whole, key=lambda pl: sum(Decimal(seg["price"]) for seg in pl))
            total = sum((Decimal(seg["price"]) for seg in plan), Decimal(0))
            tickets = describe(legs, plan)
            journey.fares = [self.make_fare(total, "CZK", label=" + ".join(dict.fromkeys(seg["system"] for seg in plan)),
                                            tariff=tickets)]
            journey.fare_status = FareStatus.PRICED
            if any(seg["kind"] == "offer" for seg in plan):
                journey.notes.append(f"🎫 Jízdenky: {tickets}. Jízdenku z IDOS prodává e-shop IDOS (ČD); "
                                     f"jízdenky IDS podle tarifu systému. Spoj z {source}.")
                # zbytek rozpisů zkusí hack idos_complete – lom u jiných dopravců bývá levnější
                rest = [p for p in plans if any(seg["kind"] == "buy" for seg in p)]
                if rest:
                    journey.raw = {"plans": rest}
                journey.legs = with_walks(legs, conn)
                return journey
            journey.notes.append(f"🎫 Jízdenky IDS: {tickets} – cena podle tarifu systému; koupíte je "
                                 "v aplikaci systému, u řidiče nebo v automatu. "
                                 f"Spoj z {source}.")
            # Delší cesta vlakem ČD: jízdenka ČD (se slevou, akční) může vyjít
            # levněji než IDS – hack idos_complete ji zkusí, porovná a levnější nabídne.
            if total >= TRAIN_CHECK_FROM * len(query.passengers):
                trains = self._trains_bought(query, infos)
                if trains:
                    journey.raw = {"plans": trains}
            journey.legs = with_walks(legs, conn)
            return journey
        first = plans[0]
        priced = [seg for seg in first if seg["kind"] == "ids"]
        missing = ", ".join(f"{seg_name(legs, seg)}" for seg in first if seg["kind"] == "buy")
        journey.notes.append(f"Spoj z {source}. " + (f"Jízdenky IDS: {describe(legs, priced)}. " if priced else "")
                             + f"Cenu úseku {missing} IDS nezná – je potřeba jízdenka dopravce.")
        journey.raw = {"plans": plans}
        journey.legs = with_walks(legs, conn)
        return journey

    async def _offer_plan(self, query: SearchQuery, conn: idos.IdosConnection, legs: list[Leg],
                          infos) -> list[dict] | None:
        """Rozpis s jízdenkou z e-shopu IDOS (ČD – i na cesty do ciziny).

        IDOS ke spoji řekne cenu a úseky, na které jízdenka platí
        („Borovany - Bad Ischl“, „České Budějovice - Linz Hbf<br>Salzburg Hbf -
        Ljubljana“). Zbytek (autobus na nádraží, WESTbahn mezi Linzem a
        Salzburgem) pokryje tarif IDS nebo ho dokoupí hack u dopravců.
        Jen pro jednoho dospělého bez slev – jinak by cena z IDOS nebyla jeho."""
        passengers = query.passengers
        if len(passengers) != 1 or passengers[0].cards or not 26 <= passengers[0].age < 65 \
                or not conn.handle or not conn.conn_id:
            return None
        try:
            response = await self.client.request("POST", idos.ajax_url("GetPriceOffer", _combination(query)),
                                                 params={"callback": "cb"},
                                                 data=idos.offer_data(conn), referer=self.referer,
                                                 expect_json=False)
        except TransientProviderError as exc:
            log.debug("IDS: nabídka jízdenky: %s", exc.message)
            return None
        price, parts = idos.parse_offer(response.text)
        if price is None:
            return None
        last = len(legs) - 1
        spans: list[tuple[int, int]] = []
        for start, end in parts or [(legs[0].origin, legs[-1].destination)]:
            a = next((k for k in range(last + 1) if same_place(legs[k].origin, start)), None)
            b = next((k for k in range(last + 1) if same_place(legs[k].destination, end)), None)
            if a is None or b is None or b < a or (spans and a <= spans[-1][1]):
                trace(f"IDOS: jízdenku {price} Kč na {start} → {end} nejde přiřadit k úsekům spoje")
                return None
            spans.append((a, b))
        abroad = [(a, b) for a, b in spans
                  if "CZ" not in (geo.country_of(legs[a].origin), geo.country_of(legs[b].destination))]
        if abroad:
            # jízdenka i na kus celý v cizině (Salzburg → Lublaň): cenu z IDOS tam
            # nebereme – ocení ho dopravci té země (cena platí pro všechny části)
            trace(f"IDOS: jízdenka {price:.0f} Kč zahrnuje i kus v cizině ("
                  + ", ".join(f"{legs[a].origin} → {legs[b].destination}" for a, b in abroad)
                  + ") – nepoužiji, ocení dopravci")
            return None
        trace(f"IDOS nabízí jízdenku {price:.0f} Kč na " + ", ".join(
            f"{legs[a].origin} → {legs[b].destination}" for a, b in spans))
        plan: list[dict] = []
        pos = 0
        for n, (a, b) in enumerate(spans):
            if a > pos:
                plan += _cover(query, infos, [(k, 0, len(infos[k][0]) - 1) for k in range(pos, a)])
            plan.append({"kind": "offer", "from": [a, 0], "to": [b, len(infos[b][0]) - 1],
                         "price": str(price if n == 0 else 0), "system": "IDOS",
                         "name": "jízdenka z e-shopu IDOS" + (" (platí pro všechny části)" if len(spans) > 1 else "")})
            pos = b + 1
        if pos <= last:
            plan += _cover(query, infos, [(k, 0, len(infos[k][0]) - 1) for k in range(pos, last + 1)])
        return plan

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


def _combination(query: SearchQuery) -> str:
    """Jízdní řády IDOS: jen vlaky (``--trains``), jinak vše."""
    return idos.TRAINS if query.trains_only else idos.ALL


def with_walks(legs: list[Leg], conn: idos.IdosConnection) -> list[Leg]:
    """Úseky spoje s pěšími přesuny mezi nimi („Přesun asi 8 min“ v IDOS).

    Rozpisy jízdenek (``raw["plans"]``) počítají jen s jízdami – pozice
    v nich jsou indexy úseků bez přesunů."""
    out: list[Leg] = []
    for k, (leg, raw) in enumerate(zip(legs, conn.legs)):
        if k and raw.walk_before:
            prev = out[-1]
            start = prev.arrival
            out.append(Leg(origin=prev.destination, destination=leg.origin, departure=start,
                           arrival=min(start + dt.timedelta(minutes=raw.walk_before), leg.departure),
                           mode=Mode.WALK, line="přesun"))
        out.append(leg)
    return out


def rides(journey: Journey) -> list[Leg]:
    """Úseky jízdy (bez pěších přesunů) – k nim se vztahují pozice v rozpisech."""
    return [leg for leg in journey.legs if leg.mode is not Mode.WALK]


def _crosses_border(origin: str, destination: str) -> bool:
    from .. import geo

    a, b = geo.country_of(origin), geo.country_of(destination)
    return a is not None and b is not None and a != b


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
        if seg["to"][0] > k:                 # kus pokračuje dalšími úseky – i ty dokoupit
            out.append({"kind": "buy", "from": [k + 1, 0], "to": seg["to"]})
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
                     f"{s['system']} ({s['name']}) {Decimal(s['price']):.0f} Kč" for s in plan
                     if s["kind"] in ("ids", "offer"))
