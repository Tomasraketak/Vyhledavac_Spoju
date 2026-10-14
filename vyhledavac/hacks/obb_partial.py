"""ÖBB „Ticket nur für Teilstrecke“ – dokoupení zbytku trasy jinde.

Shop ÖBB u některých spojů prodá jízdenku jen na část trasy (např. Třeboň →
Marchegg za 14,90 € Sparschiene, ale ne dál do Bratislavy; Trnava → Köln Hbf,
ale ne dál do Rotterdamu). Dopravce ÖBB takovou cenu u celého spoje nezobrazí
(byla by nesmyslně nízká) a uloží ji do ``journey.raw["partial"]``. Tenhle
hack dohledá chybějící začátek / konec trasy u dopravců (i samostatnou
jízdenkou ÖBB) a nabídne kombinaci: jízdenka ÖBB na úsek, kde platí, +
jízdenka jiného dopravce na zbytek – na tomtéž spoji nebo s přestupem
(Köln → Rotterdam třeba nočním FlixBusem).

Zbytek může být zase jen částečná jízdenka ÖBB (Köln → Arnhem) – pak se
dohledá i její zbytek (Arnhem → Rotterdam u NS), nejvýš ``MAX_DEPTH`` krát.

Cena se nabídne vždy, i když je dražší než nejlevnější autobus: spoj by
jinak zůstal úplně bez ceny.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from .. import transfer
from ..models import Fare, FareStatus, Journey, SearchQuery
from ..normalize import matches_station
from ..registry import HACKS
from ..trace import set_tag, trace
from .base import SearchFn, TravelHack, combine
from .cd_europe import continues

log = logging.getLogger(__name__)

MAX_JOURNEYS = 8
#: Kolikrát smí být i zbytek trasy jen částečná jízdenka ÖBB.
MAX_DEPTH = 2
#: Nejdelší čekání na navazující jízdenku (noční příjezd do Kolína → ranní vlak).
MAX_WAIT = dt.timedelta(hours=3)


@HACKS.register()
class ObbPartialHack(TravelHack):
    name = "obb_partial"
    label = "ÖBB jen na část trasy + zbytek u jiného dopravce"
    default_min_saving_pct = 0.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        partial = [j for j in journeys if j.raw.get("partial") and j.raw["partial"].get("czk")]
        if not partial:
            return []
        # tentýž spoj (časy + úsek jízdenky) od ÖBB přijde i víckrát
        unique: dict[tuple, Journey] = {}
        for journey in sorted(partial, key=lambda j: j.departure):
            info = journey.raw["partial"]
            unique.setdefault((journey.departure, journey.arrival, info["from"], info["to"]), journey)
        todo = list(unique.values())[:MAX_JOURNEYS]

        async def one(n: int, journey: Journey) -> Journey | None:
            set_tag(f"ÖBB#{n}")
            try:
                return await self._complete(query, journey, search)
            except Exception:
                log.exception("obb_partial: dopočet selhal")
                return None

        found = [combo for combo in await asyncio.gather(*(one(n, j) for n, j in enumerate(todo, 1)))
                 if combo is not None]
        # stejná kombinace (tytéž časy) z několika spojů ÖBB – stačí nejlevnější
        best: dict[tuple, Journey] = {}
        for combo in found:
            key = (combo.departure, combo.arrival)
            if key not in best or combo.price < best[key].price:
                best[key] = combo
        return list(best.values())

    async def _complete(self, query: SearchQuery, journey: Journey,
                        search: SearchFn) -> Journey | None:
        info = journey.raw["partial"]
        trace(f"▶ ÖBB {journey.departure:%H:%M}–{journey.arrival:%H:%M}: jízdenka jen "
              f"{info['from']} → {info['to']} ({Decimal(info['eur']):.2f} €)")
        obb = _piece(journey)
        if obb is None:
            trace("✗ úsek jízdenky ve spoji nenajdu")
            return None
        missing_head = obb.legs[0] is not journey.legs[0]
        missing_tail = obb.legs[-1] is not journey.legs[-1]
        parts = [obb]
        if missing_head:
            heads = _priced(await search(query.leg_query(query.origin, obb.origin)))
            head = _latest_before(heads, obb)
            if head is None:
                trace(f"✗ začátek {query.origin} → {obb.origin} nikdo neprodává")
                return None
            trace(f"✓ začátek: {head.provider} {head.departure:%H:%M}–{head.arrival:%H:%M} {head.price:.0f} Kč")
            parts.insert(0, head)
        if missing_tail:
            rest = await self._cover(query, obb, search, MAX_DEPTH)
            if rest is None:
                rest = await self._regional_rest(query, journey, obb, search)
            if rest is None:
                trace(f"✗ zbytek {obb.destination} → {query.destination} nikdo neprodává")
                return None
            parts += rest
        # úsek ÖBB levněji jinde: tentýž vlak, nebo jiný, který se vejde mezi
        # sousední jízdenky (ÖBB Bratislava → Praha 89 €, RegioJet o půl hodiny později 399 Kč)
        i = parts.index(obb)
        found = await search(query.leg_query(obb.origin, obb.destination, obb.departure - dt.timedelta(minutes=5)))
        cheaper = await _cheaper_fit(obb, found, parts[i - 1] if i > 0 else None,
                                     parts[i + 1] if i + 1 < len(parts) else None)
        if cheaper is not None:
            trace(f"↓ úsek {obb.origin} → {obb.destination} levněji: {cheaper.provider} "
                  f"{cheaper.departure:%H:%M}–{cheaper.arrival:%H:%M} {cheaper.price:.0f} Kč místo ÖBB {obb.price:.0f} Kč")
            parts[i] = cheaper
        total = sum((p.price for p in parts), Decimal(0))
        lines = "; ".join(f"{p.origin} → {p.destination}: {p.provider} {p.price:.0f} Kč" for p in parts)
        trace(f"✓ celkem {total:.0f} Kč = {lines}")
        if cheaper is None:
            note = (f"🧩 ÖBB prodává jízdenku jen na část trasy ({info['from']} → {info['to']}, "
                    f"{Decimal(info['eur']):.2f} €) – zbytek samostatnou jízdenkou: {lines}. "
                    f"Celkem {total:.0f} Kč.")
        else:
            note = (f"🧩 Jízdenky po částech: {lines}. Celkem {total:.0f} Kč (ÖBB by úsek "
                    f"{info['from']} → {info['to']} prodalo za {Decimal(info['eur']):.2f} €).")
        combo = combine(" + ".join(dict.fromkeys(p.provider for p in parts)), parts, notes=[note])
        if combo is not None and not combo.deep_link:
            combo.deep_link = journey.deep_link
        return combo

    async def _regional_rest(self, query: SearchQuery, journey: Journey, obb: Journey,
                             search: SearchFn) -> list[Journey] | None:
        """Zbytek tímtéž spojem, když za jízdenkou ÖBB jedou německé regionální
        vlaky (Duisburg → Venlo): ty podle ceníku (odhad), dál zase prodejci (NS)."""
        from ..de_regional import is_regional, regional_journey
        from ..money import get_converter

        rest = [leg for leg in _span(journey.legs, obb.destination, journey.legs[-1].destination)
                if leg.mode.value != "walk"]
        k = 0
        while k < len(rest) and is_regional(rest[k]):
            k += 1
        if k == 0:
            return None
        regional = await regional_journey(rest[:k], query.passengers, get_converter())
        if regional is None:
            return None
        trace(f"≈ regionální vlaky {regional.origin} → {regional.destination} podle ceníku: "
              f"{regional.price:.0f} Kč ({regional.fares[0].tariff})")
        if k == len(rest):
            return [regional]
        tail = await self._cover(query, regional, search, MAX_DEPTH - 1)
        return None if tail is None else [regional, *tail]

    async def _cover(self, query: SearchQuery, prev: Journey, search: SearchFn,
                     depth: int) -> list[Journey] | None:
        """Jízdenky z konce ``prev`` do cíle: nejlevnější navazující spoj s cenou,
        jinak další částečná jízdenka ÖBB a zbytek za ní (rekurzivně)."""
        since = prev.arrival - dt.timedelta(minutes=5)
        found = await search(query.leg_query(prev.destination, query.destination, since))
        options = [t for t in _priced(found) if await _follows(prev, t)]
        if options:
            tail = min(options, key=lambda t: (t.price, t.arrival))
            trace(f"✓ zbytek {prev.destination} → {query.destination}: {tail.provider} "
                  f"{tail.departure:%H:%M}–{tail.arrival:%H:%M} {tail.price:.0f} Kč")
            return [tail]
        if depth <= 0:
            return None
        # zbytek zase jen částečně (ÖBB Köln → Arnhem) – dohledat i jeho zbytek
        for candidate in sorted((t for t in found if t.raw.get("partial") and t.raw["partial"].get("czk")),
                                key=lambda t: Decimal(t.raw["partial"]["czk"])):
            piece = _piece(candidate)
            if piece is None or not await _follows(prev, piece):
                continue
            if piece.legs[-1] is candidate.legs[-1]:
                return [piece]
            trace(f"… zbytek jen částečně: ÖBB {piece.origin} → {piece.destination} {piece.price:.0f} Kč")
            rest = await self._cover(query, piece, search, depth - 1)
            if rest is not None:
                return [piece, *rest]
        return None


def _piece(journey: Journey) -> Journey | None:
    """Část spoje, na kterou platí částečná jízdenka ÖBB, s její cenou – i když
    jízdenka končí uprostřed vlaku (ICE 920 jede dál, jízdenka jen do Kölnu)."""
    info = journey.raw["partial"]
    legs = _span(journey.legs, info["from"], info["to"])
    if not legs:
        return None
    obb = copy.copy(journey)
    obb.legs = legs
    obb.raw = {}
    obb.notes = []
    obb.segments = []
    price = Decimal(info["czk"])
    obb.fares = [Fare(amount=Decimal(info["eur"]), currency="EUR", amount_czk=price,
                      label="ÖBB", tariff="jízdenka na úsek")]
    obb.fare_status = FareStatus.PRICED
    return obb


def _span(legs: list, start: str, end: str) -> list:
    """Úseky od stanice ``start`` do ``end`` (i zastávky uprostřed vlaku; krajní
    úseky oříznuté). Celé úseky zůstanou tytéž objekty."""
    from .idos_complete import _slice

    def names(leg) -> list[str]:
        return [s.name for s in leg.stops] if len(leg.stops) >= 2 else [leg.origin, leg.destination]

    def hit(a: str, b: str) -> bool:
        return matches_station(a, b) or matches_station(b, a)

    first = None
    for k, leg in enumerate(legs):
        if first is None:
            stops = names(leg)
            for i, name in enumerate(stops[:-1]):
                if hit(start, name):
                    first = [k, i]
                    break
        if first is not None:
            stops = names(leg)
            for i, name in enumerate(stops):
                if (k, i) > tuple(first) and hit(end, name):
                    return _slice(legs, first, [k, i])
    return []


def _priced(journeys: Sequence[Journey]) -> list[Journey]:
    """Zbytek smí prodat kdokoli, i ÖBB samostatnou jízdenkou (ta už je celá)."""
    return [j for j in journeys if j.price is not None and not j.raw.get("partial") and j.legs]


async def _follows(prev: Journey, nxt: Journey) -> bool:
    """Navazuje ``nxt`` na ``prev``? Tentýž vlak, nebo přestup se skutečným časem
    na přesun (Köln Hbf → autobusové nádraží) a čekáním nejvýš ``MAX_WAIT``."""
    if continues(prev, nxt):
        return True
    gap = nxt.departure - prev.arrival
    if gap < dt.timedelta(0) or gap > MAX_WAIT:
        return False
    from .idos_complete import _same_city

    if not _same_city(nxt.legs[0].origin, prev.legs[-1].destination):
        return False
    return gap >= await transfer.transfer_time(prev.legs[-1].destination, nxt.legs[0].origin, prev.arrival)


async def _cheaper_fit(obb: Journey, found: Sequence[Journey], before: Journey | None,
                      after: Journey | None) -> Journey | None:
    """Levnější jízdenka místo úseku ÖBB: tentýž spoj (odjezd i příjezd do 5 min)
    u jiného prodejce, nebo jiný spoj z/do stejného místa, který navazuje na
    předchozí jízdenku a stihne tu další."""
    tolerance = dt.timedelta(minutes=5)
    from .idos_complete import _same_city

    best = None
    for option in _priced(found):
        if option.price >= obb.price or (best is not None and option.price >= best.price):
            continue
        same = (abs(option.departure - obb.departure) <= tolerance
                and abs(option.arrival - obb.arrival) <= tolerance)
        if not same:
            if not (_same_city(option.legs[0].origin, obb.origin)
                    and _same_city(option.legs[-1].destination, obb.destination)):
                continue
            if before is not None and not await _follows(before, option):
                continue
            if before is None and option.departure < obb.departure:
                continue
            if after is not None and not await _follows(option, after):
                continue
        best = option
    return best


def _latest_before(heads: Sequence[Journey], nxt: Journey) -> Journey | None:
    ok = [h for h in heads if continues(h, nxt)]
    return min(ok, key=lambda h: (h.price, -h.departure.timestamp()), default=None)
