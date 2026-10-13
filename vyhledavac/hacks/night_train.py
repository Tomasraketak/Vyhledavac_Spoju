"""Noční vlak European Sleeper s návazností: Amsterdam → Praha nočním vlakem
a dál do Bratislavy, Vídně nebo Brna.

European Sleeper prodává jen stanice na své trase (Brusel – Amsterdam –
Berlín – Drážďany – Praha, Brusel – Kolín – Curych – Milán, Paříž – Berlín),
přímé hledání ho tedy na cestu Amsterdam → Bratislava vůbec nezná. A IDOS
ani plánovač ho v nejlepších spojích často nemají, protože jede jednou
denně a ne každý den. Při nákupu s předstihem bývá přitom nejlevnější
(sedadlo Amsterdam → Praha od 50 €).

Postup:

1. Polohy odkud/kam (databáze měst, česká místa z geokodéru Transitous).
2. Na každé trase vybere stanici nejblíž začátku a nejblíž cíli. Má-li
   smysl (noční vlak veze aspoň polovinu cesty a objížďka je nejvýš o 35 %
   delší), zeptá se European Sleeper na úsek mezi nimi.
3. Dokoupí dojezd k vlaku (odkud → nástupní stanice) a z výstupní stanice do
   cíle u ostatních dopravců – se skutečným časem přestupu (``transfer``).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from decimal import Decimal
from typing import Sequence

from .. import transfer
from ..models import Journey, SearchQuery
from ..registry import HACKS, PROVIDERS
from ..trace import nested, trace
from .base import SearchFn, TravelHack, combine

log = logging.getLogger(__name__)

SELLER = "EuropeanSleeper"
#: Objížďka přes noční vlak nejvýš o tolik delší než přímá vzdálenost.
MAX_DETOUR = 1.35
#: Noční vlak musí vézt aspoň takový díl přímé vzdálenosti.
MIN_SHARE = 0.5
#: Na místě (začátek/cíl je stanice nočního vlaku).
ON_ROUTE_KM = 20.0
#: Kolik nočních vlaků (dnů) ocenit s návazností.
MAX_TRAINS = 2
#: Nejdelší čekání na navazující spoj po příjezdu nočního vlaku.
MAX_WAIT = dt.timedelta(hours=4)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def candidates(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[str, str]]:
    """Dvojice (nástupní, výstupní) stanic European Sleeper, přes které má
    smysl jet – seřazené podle délky celé cesty."""
    from ..providers import european_sleeper as es

    direct = _km(start, end)
    found: dict[tuple[str, str], float] = {}
    for route in es.ROUTES:
        stations = [s for s in route if s in es.COORDS]
        board = min(stations, key=lambda s: _km(start, es.COORDS[s]))
        alight = min(stations, key=lambda s: _km(end, es.COORDS[s]))
        if board == alight or not es.same_route(board, alight):
            continue
        ride = _km(es.COORDS[board], es.COORDS[alight])
        total = _km(start, es.COORDS[board]) + ride + _km(es.COORDS[alight], end)
        if ride < MIN_SHARE * direct or total > MAX_DETOUR * direct + 50:
            continue
        found[(board, alight)] = total
    return sorted(found, key=found.get)


@HACKS.register()
class NightTrainHack(TravelHack):
    name = "night_train"
    label = "Noční vlak European Sleeper s návazností (Amsterdam → Praha → Bratislava)"
    default_min_saving_pct = 0.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        if SELLER not in PROVIDERS or len(query.passengers) > 5:
            return []
        from ..providers import european_sleeper as es

        start, end = await transfer.locate(query.origin), await transfer.locate(query.destination)
        if start is None or end is None:
            return []
        pairs = candidates(start, end)
        direct = (es.pick_station(query.origin), es.pick_station(query.destination))
        pairs = [p for p in pairs if p != direct][:1]      # přímý spoj už hledal dopravce sám
        found: list[Journey] = []
        for board, alight in pairs:
            names = es._BY_ID[board][1], es._BY_ID[alight][1]
            trace(f"🌙 noční vlak European Sleeper {names[0]} → {names[1]} pro {query.origin} → "
                  f"{query.destination}")
            with nested():
                found += await self._via(query, search, board, alight, start, end)
        return found

    async def _via(self, query: SearchQuery, search: SearchFn, board: str, alight: str,
                   start: tuple[float, float], end: tuple[float, float]) -> list[Journey]:
        from ..providers import european_sleeper as es

        board_name, alight_name = es._BY_ID[board][1], es._BY_ID[alight][1]
        head_needed = _km(start, es.COORDS[board]) > ON_ROUTE_KM
        tail_needed = _km(es.COORDS[alight], end) > ON_ROUTE_KM
        trains = await search(query.leg_query(query.origin if not head_needed else board_name,
                                              query.destination if not tail_needed else alight_name),
                              only=[SELLER])
        trains = sorted((t for t in trains if t.provider == SELLER and t.price is not None),
                        key=lambda t: t.departure)[:MAX_TRAINS]
        if not trains:
            trace("✗ noční vlak v tomhle okně nejede nebo nemá cenu")
            return []
        out = []
        for train in trains:
            trace(f"vlak {train.legs[0].line} {train.departure:%d.%m. %H:%M}–{train.arrival:%H:%M}: "
                  f"{train.price:.0f} Kč ({train.fares[0].tariff if train.fares else ''})")
            heads: list[Journey | None] = [None]
            if head_needed:
                head = await self._head(query, search, board_name, train)
                if head is None:
                    continue
                heads = [head]
            tails: list[Journey | None] = [None]
            if tail_needed:
                tails = list(await self._tails(query, search, alight_name, train))
                if not tails:
                    continue
            for head in heads:
                for tail in tails:
                    journey = self._joined(train, head, tail, board_name, alight_name)
                    if journey is not None:
                        out.append(journey)
        return out

    @staticmethod
    def _joined(train: Journey, head: Journey | None, tail: Journey | None,
                board_name: str, alight_name: str) -> Journey | None:
        parts = [p for p in (head, train, tail) if p is not None]
        journey = combine(" + ".join(dict.fromkeys(p.provider for p in parts)), parts, notes=[
            f"🌙 Noční vlak European Sleeper {board_name} → {alight_name} "
            f"({train.fares[0].tariff if train.fares else 'cena za místo'}). Jede jen některé dny; "
            "s předstihem bývá mnohem levnější."])
        if journey is not None and journey.price is not None:
            trace(f"✓ {journey.price:.0f} Kč = " + " + ".join(f"{p.provider} {p.price:.0f}" for p in parts))
            return journey
        return None

    async def _head(self, query: SearchQuery, search: SearchFn, board_name: str,
                    train: Journey) -> Journey | None:
        """Dojezd k nočnímu vlaku – nejlevnější, který ho stihne."""
        options = await search(query.leg_query(query.origin, board_name, query.departure))
        fitting = []
        for o in options:
            if o.price is None or not o.legs or o.departure < query.departure:
                continue
            gap = await transfer.transfer_time(o.legs[-1].destination, train.legs[0].origin, o.arrival)
            if o.arrival + gap <= train.departure:
                fitting.append(o)
        if not fitting:
            trace(f"✗ k vlaku do {board_name} nic s cenou nestihne odjezd {train.departure:%H:%M}")
            return None
        best = min(fitting, key=lambda o: (o.price, -o.arrival.timestamp()))
        trace(f"→ dojezd {best.provider} {best.departure:%H:%M}–{best.arrival:%H:%M} {best.price:.0f} Kč")
        return best

    async def _tails(self, query: SearchQuery, search: SearchFn, alight_name: str,
                     train: Journey) -> list[Journey]:
        """Navazující spoje z výstupní stanice do cíle (do 4 h): nejlevnější
        a nejdřív v cíli (když je to jiný)."""
        since = train.arrival + transfer.SAME
        options = await search(query.leg_query(alight_name, query.destination, since))
        fitting = []
        for o in options:
            if o.price is None or not o.legs or not since <= o.departure <= train.arrival + MAX_WAIT:
                continue
            gap =await transfer.transfer_time(train.legs[-1].destination, o.legs[0].origin, train.arrival)
            if train.arrival + gap <= o.departure:
                fitting.append(o)
        if not fitting:
            trace(f"✗ z {alight_name} dál nic s cenou do {MAX_WAIT.seconds // 3600} h")
            return []
        cheapest = min(fitting, key=lambda o: (o.price or Decimal(0), o.arrival))
        fastest = min(fitting, key=lambda o: (o.arrival, o.price or Decimal(0)))
        chosen = [cheapest] + ([fastest] if fastest is not cheapest else [])
        for best in chosen:
            trace(f"→ dál {best.provider} {best.departure:%H:%M}–{best.arrival:%H:%M} {best.price:.0f} Kč")
        return chosen
