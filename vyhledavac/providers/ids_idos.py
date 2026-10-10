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
        bez ceny a v ``raw["unpriced"]`` – dopočítá je hack ``idos_complete``
        u ostatních dopravců.
        """
        from ..hacks.ids_ticket import NOT_IDS, SYSTEMS, RouteStop

        infos = []          # pro každý úsek: (stops, povolené systémy | None = podle zón, IDS platí?)
        route_systems: list[dict] = []
        legs: list[Leg] = []
        for leg in conn.legs:
            names = [leg.origin, leg.destination]
            ok = True
            if leg.is_bus or leg.is_city:
                systems = set()
                if leg.detail_url:
                    page = await self._detail(leg.detail_url)
                    licence, tagged = idos.parse_line_info(page)
                    found = line_systems(licence or leg.licence, tagged)
                    systems = found if found is not None else None
                    names = idos.parse_route(page) or names
                ok = systems is None or bool(systems)
                rail = False
            else:
                ok = not NOT_IDS.match(leg.train_label) and "České dráhy" in leg.carrier
                systems = None
                rail = True
            stops = []
            near: set[str] = set(route_systems[-1]) if route_systems else set()
            for i, name in enumerate(names):
                place = _place(name, rail, near)
                if place is not None and place.systems:
                    near = set(place.systems)
                zones = {k: v for k, v in (place.systems if place else {}).items() if k in SYSTEMS}
                time = leg.departure if i == 0 else (leg.arrival if i == len(names) - 1 else None)
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
                                        departure=s.time if not k else None)
                                   for k, s in enumerate(stops)]))

        pieces, unpriced = self._pieces(query, infos, legs)
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=conn.share_url or idos.results_url())
        total = sum((f.price for _, _, f in pieces), Decimal(0))
        tickets = "; ".join(f"{legs[a].origin} → {legs[b].destination}: {f.system} ({f.name}) "
                            f"{f.price:.0f} Kč" for a, b, f in pieces)
        if pieces and not unpriced:
            journey.fares = [self.make_fare(total, "CZK", label=" + ".join(dict.fromkeys(f.system for _, _, f in pieces)),
                                            tariff=tickets)]
            journey.fare_status = FareStatus.PRICED
            journey.notes.append(f"🎫 Jízdenky IDS: {tickets} – cena podle tarifu systému; koupíte je "
                                 "v aplikaci systému, u řidiče nebo v automatu. Spoj z IDOS.")
        else:
            missing = ", ".join(f"{legs[i].origin} → {legs[i].destination} ({legs[i].line})" for i in unpriced)
            journey.notes.append("Spoj z IDOS. " + (f"Jízdenky IDS: {tickets}. " if pieces else "")
                                 + f"Cenu úseku {missing} IDS nezná – je potřeba jízdenka dopravce.")
            journey.raw = {"ids_pieces": [(a, b, str(f.price), f.system, f.name) for a, b, f in pieces],
                           "unpriced": unpriced}
        return journey

    @staticmethod
    def _pieces(query: SearchQuery, infos, legs):
        """Úseky spoje po jízdenkách IDS: nejdelší souvislé úseky v jednom systému.

        Vrací ``([(od úseku, do úseku, IdsFare)], [nepokryté úseky])``.
        """
        from ..hacks.ids_ticket import IdsTicketHack, SYSTEMS

        hack = IdsTicketHack()
        pieces, unpriced = [], []
        a = 0
        while a < len(infos):
            found = None
            for b in range(len(infos) - 1, a - 1, -1):          # nejdelší úsek první
                if not all(infos[k][2] for k in range(a, b + 1)):
                    continue
                route = [s for k in range(a, b + 1) for s in infos[k][0]]
                allowed = set(SYSTEMS)
                for k in range(a, b + 1):
                    if infos[k][1] is not None:
                        allowed &= set(infos[k][1])
                allowed &= set(route[0].systems) & set(route[-1].systems)
                minutes = (legs[b].arrival - legs[a].departure).total_seconds() / 60
                fares = [f for f in (hack._ids_fare(query, route, sys_, 0, len(route) - 1, minutes)
                                     for sys_ in sorted(allowed)) if f is not None]
                if fares:
                    found = (b, min(fares, key=lambda f: f.price))
                    break
            if found is None:
                unpriced.append(a)
                a += 1
                continue
            b, fare = found
            pieces.append((a, b, fare))
            a = b + 1
        return pieces, unpriced
