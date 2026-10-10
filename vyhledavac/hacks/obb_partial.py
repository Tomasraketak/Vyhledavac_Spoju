"""ÖBB „Ticket nur für Teilstrecke“ – dokoupení zbytku trasy jinde.

Shop ÖBB u některých spojů prodá jízdenku jen na část trasy (např. Třeboň →
Marchegg za 14,90 € Sparschiene, ale ne dál do Bratislavy). Dopravce ÖBB
takovou cenu u celého spoje nezobrazí (byla by nesmyslně nízká) a uloží ji
do ``journey.raw["partial"]``. Tenhle hack dohledá chybějící začátek / konec
trasy u dopravců (i samostatnou jízdenkou ÖBB) a nabídne kombinaci: jízdenka ÖBB na
úsek, kde platí, + jízdenka jiného dopravce na zbytek – na tomtéž spoji
nebo s přestupem.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from ..models import Fare, FareStatus, Journey, SearchQuery
from ..normalize import matches_station
from ..registry import HACKS
from .base import SearchFn, TravelHack, combine
from .cd_europe import continues

log = logging.getLogger(__name__)

MAX_JOURNEYS = 4


@HACKS.register()
class ObbPartialHack(TravelHack):
    name = "obb_partial"
    label = "ÖBB jen na část trasy + zbytek u jiného dopravce"
    default_min_saving_pct = 3.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        partial = [j for j in journeys if j.raw.get("partial") and j.raw["partial"].get("czk")]
        if not partial:
            return []
        baseline = self.baseline_price(journeys)
        found = []
        for journey in partial[:MAX_JOURNEYS]:
            try:
                combo = await self._complete(query, journey, search)
            except Exception:
                log.exception("obb_partial: dopočet selhal")
                continue
            if combo is not None and self.is_worth_it(combo.price, baseline):
                found.append(combo)
        return found

    async def _complete(self, query: SearchQuery, journey: Journey,
                        search: SearchFn) -> Journey | None:
        info = journey.raw["partial"]
        start, end = _leg_index(journey, info["from"], "origin"), _leg_index(journey, info["to"], "destination")
        if start is None or end is None or end < start:
            return None
        price = Decimal(info["czk"])
        obb = copy.deepcopy(journey)
        obb.legs = obb.legs[start:end + 1]
        obb.raw = {}
        obb.notes = []
        obb.fares = [Fare(amount=Decimal(info["eur"]), currency="EUR", amount_czk=price,
                          label="ÖBB", tariff="jízdenka na úsek")]
        obb.fare_status = FareStatus.PRICED
        parts = [obb]
        # zbytek smí prodat kdokoli, i ÖBB samostatnou jízdenkou (ta už je celá)
        others = lambda js: [j for j in js if j.price is not None and not j.raw.get("partial")]
        if start > 0:              # chybí začátek: odkud -> začátek úseku ÖBB
            heads = others(await search(query.leg_query(query.origin, obb.origin)))
            head = _latest_before(heads, obb)
            if head is None:
                return None
            parts.insert(0, head)
        if end < len(journey.legs) - 1:     # chybí konec: konec úseku ÖBB -> kam
            tails = others(await search(query.leg_query(obb.destination, query.destination,
                                                        obb.arrival - dt.timedelta(minutes=5))))
            tail = min((t for t in tails if continues(obb, t)), key=lambda t: (t.price, t.arrival),
                       default=None)
            if tail is None:
                return None
            parts.append(tail)
        total = sum((p.price for p in parts), Decimal(0))
        lines = "; ".join(f"{p.origin} → {p.destination}: {p.provider} {p.price:.0f} Kč" for p in parts)
        note = (f"🧩 ÖBB prodává jízdenku jen na část trasy ({info['from']} → {info['to']}, "
                f"{Decimal(info['eur']):.2f} €) – zbytek samostatnou jízdenkou: {lines}. "
                f"Celkem {total:.0f} Kč.")
        return combine(" + ".join(dict.fromkeys(p.provider for p in parts)), parts, notes=[note])


def _leg_index(journey: Journey, name: str, end: str) -> int | None:
    """Úsek spoje, který začíná (``origin``) / končí (``destination``) ve stanici ``name``."""
    for i, leg in enumerate(journey.legs):
        if matches_station(name, getattr(leg, end)) or matches_station(getattr(leg, end), name):
            return i
    return None


def _latest_before(heads: Sequence[Journey], nxt: Journey) -> Journey | None:
    ok = [h for h in heads if continues(h, nxt)]
    return min(ok, key=lambda h: (h.price, -h.departure.timestamp()), default=None)
