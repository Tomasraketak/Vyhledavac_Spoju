"""Dopočet ceny spojů z IDOS, kde jízdenka IDS nepokryje celou cestu.

Domanín → Praha: autobus IDESKA do Českých Budějovic (cenu spočítá tarif
IDESKA) a dál rychlík do Prahy – ten IDS nepokrývá. Dopravce „IDS“ takový
spoj vrátí bez ceny a nepokryté úseky uloží do ``raw["unpriced"]``. Tenhle
hack se na každý nepokrytý úsek zeptá ostatních dopravců (Arriva/SJT,
Bileto, RegioJet, FlixBus …) a vezme jízdenku na **tentýž spoj** (odjezd
a příjezd do pár minut). Když se podaří ocenit všechny úseky, nabídne
spoj s celkovou cenou: jízdenky IDS + jízdenky dopravců.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from ..models import Fare, FareStatus, Journey, SearchQuery
from ..registry import HACKS
from .base import SearchFn, TravelHack, combine

log = logging.getLogger(__name__)

MAX_JOURNEYS = 6
#: Kdo prodává jízdenky na úseky mimo IDS (bez prohlížeče).
SELLERS = ["Arriva", "Bileto", "RegioJet", "FlixBus", "LeoExpress"]
SAME_DEPARTURE = dt.timedelta(minutes=3)
SAME_ARRIVAL = dt.timedelta(minutes=6)


@HACKS.register()
class IdosCompleteHack(TravelHack):
    name = "idos_complete"
    label = "Spoje z IDOS: úseky mimo IDS ocení dopravci"
    default_min_saving_pct = 0.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        todo = [j for j in journeys if j.provider == "IDS" and j.raw.get("unpriced")][:MAX_JOURNEYS]
        found = []
        for journey in todo:
            try:
                done = await self._complete(query, journey, search)
            except Exception:
                log.exception("idos_complete: dopočet selhal")
                continue
            if done is not None:
                found.append(done)
        return found

    async def _complete(self, query: SearchQuery, journey: Journey,
                        search: SearchFn) -> Journey | None:
        legs = journey.legs
        pieces = {a: (a, b, Decimal(price), system, name)
                  for a, b, price, system, name in journey.raw.get("ids_pieces", [])}
        unpriced = set(journey.raw["unpriced"])
        segments: list[Journey] = []
        i = 0
        while i < len(legs):
            if i in pieces:
                a, b, price, system, name = pieces[i]
                segments.append(Journey(
                    provider=system, legs=legs[a:b + 1], fare_status=FareStatus.PRICED,
                    fares=[Fare(amount=price, currency="CZK", amount_czk=price, label=system,
                                tariff=name)]))
                i = b + 1
                continue
            # souvislý nepokrytý úsek
            j = i
            while j + 1 < len(legs) and j + 1 in unpriced:
                j += 1
            seller = await self._seller(query, legs[i:j + 1], search)
            if seller is None and j > i:           # zkusit po jednotlivých úsecích
                parts = [await self._seller(query, [leg], search) for leg in legs[i:j + 1]]
                if any(p is None for p in parts):
                    return None
                segments.extend(parts)
            elif seller is None:
                return None
            else:
                segments.append(seller)
            i = j + 1
        lines = "; ".join(f"{s.origin} → {s.destination}: {s.provider} {s.price:.0f} Kč" for s in segments)
        merged = combine(" + ".join(dict.fromkeys(s.provider for s in segments)), segments,
                         notes=[f"🧩 Spoj z IDOS s jízdenkami: {lines}."])
        if merged is not None:
            merged.legs = list(legs)                 # časy a zastávky z IDOS
            merged.deep_link = journey.deep_link
        return merged

    @staticmethod
    async def _seller(query: SearchQuery, run, search: SearchFn) -> Journey | None:
        first, last = run[0], run[-1]
        options = await search(query.leg_query(_full(first.origin), _full(last.destination),
                                               first.departure - dt.timedelta(minutes=2)),
                               only=SELLERS)
        same = [o for o in options if o.price is not None and o.provider != "IDS"
                and abs(o.departure - first.departure) <= SAME_DEPARTURE
                and abs(o.arrival - last.arrival) <= SAME_ARRIVAL]
        return min(same, key=lambda o: o.price, default=None)


def _full(name: str) -> str:
    """Zkratky IDOS na celé jméno pro dopravce („Veselí n.Lužnicí“ -> „Veselí nad Lužnicí“)."""
    from .. import stations

    place = stations.place(name)
    if place is not None and place.kind != "b" and "," not in name and not place.country:
        return place.name
    return name
