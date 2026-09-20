"""Vestavěné filtry.

Každý je samostatný a bezstavový, takže se dají libovolně kombinovat.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from ..models import FareStatus, Journey, Mode, SearchQuery
from ..normalize import same_place
from ..registry import FILTERS
from .base import Filter


@FILTERS.register()
class DepartureWindow(Filter):
    """Nechá jen spoje odjíždějící v zadaném okně od požadovaného času.

    Původní kód tohle dělal přes ``(dep - req) % 1440 <= 720``, což u spojů
    přes půlnoc propouštělo i spoje z předchozího dne. Se skutečnými
    ``datetime`` je porovnání triviální a správné.
    """

    name = "departure_window"
    label = "Časové okno odjezdu"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        hours = self.options.get("hours")
        window = dt.timedelta(hours=float(hours)) if hours is not None else query.window
        end = query.departure + window
        return [j for j in journeys if query.departure <= j.departure <= end]


@FILTERS.register()
class MaxPrice(Filter):
    """Strop ceny. Spoje bez ceny se dají volitelně ponechat."""

    name = "max_price"
    label = "Maximální cena"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        limit = self.options.get("limit")
        if limit is None:
            return journeys
        limit = Decimal(str(limit))
        keep_unknown = bool(self.options.get("keep_unknown", True))
        out = []
        for journey in journeys:
            price = journey.price
            if price is None:
                if keep_unknown:
                    out.append(journey)
                continue
            if price <= limit:
                out.append(journey)
        return out


@FILTERS.register()
class MaxDuration(Filter):
    """Strop doby jízdy v hodinách."""

    name = "max_duration"
    label = "Maximální doba jízdy"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        hours = self.options.get("hours")
        if hours is None:
            return journeys
        limit = dt.timedelta(hours=float(hours))
        return [j for j in journeys if j.duration <= limit]


@FILTERS.register()
class MaxTransfers(Filter):
    """Nejvýše N přestupů. ``0`` znamená jen přímé spoje."""

    name = "max_transfers"
    label = "Maximální počet přestupů"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        limit = self.options.get("limit")
        if limit is None:
            return journeys
        limit = int(limit)
        return [j for j in journeys if _real_transfers(j) <= limit]


@FILTERS.register()
class MinTransferTime(Filter):
    """Zahodí spoje s nereálně krátkým přestupem.

    Jízdní řády občas nabídnou přestup na dvě minuty přes celé nádraží.
    """

    name = "min_transfer_time"
    label = "Minimální čas na přestup"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        minutes = float(self.options.get("minutes", 5))
        limit = dt.timedelta(minutes=minutes)
        out = []
        for journey in journeys:
            gaps = [
                journey.legs[i + 1].departure - journey.legs[i].arrival
                for i in range(len(journey.legs) - 1)
                if journey.legs[i + 1].mode is not Mode.WALK
            ]
            if all(gap >= limit for gap in gaps):
                out.append(journey)
        return out


@FILTERS.register()
class OnlyModes(Filter):
    """Jen vybrané druhy dopravy, např. ``modes=['train']``."""

    name = "only_modes"
    label = "Druh dopravy"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        wanted = self.options.get("modes")
        if not wanted:
            return journeys
        allowed = {Mode(m) if not isinstance(m, Mode) else m for m in wanted}
        allowed.add(Mode.WALK)  # pěší přesun mezi nádražími nevadí
        return [j for j in journeys if j.modes <= allowed]


@FILTERS.register()
class RequirePrice(Filter):
    """Jen spoje se známou cenou (pro řazení podle ceny)."""

    name = "require_price"
    label = "Pouze s cenou"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        return [j for j in journeys if j.price is not None]


@FILTERS.register()
class DropSoldOut(Filter):
    """Zahodí vyprodané spoje."""

    name = "drop_sold_out"
    label = "Skrýt vyprodané"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        return [j for j in journeys if j.fare_status is not FareStatus.SOLD_OUT]


@FILTERS.register()
class Deduplicate(Filter):
    """Sloučí tentýž spoj nalezený více dopravci.

    Za shodný považujeme spoj se stejným odjezdem i příjezdem. Ponecháme ten
    s nižší cenou, ale převezmeme podrobnosti trasy od toho, kdo je má –
    typicky ČD/ÖBB znají zastávky, ale prodejce je levnější.
    """

    name = "deduplicate"
    label = "Sloučit duplicity"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        best: dict[tuple, Journey] = {}
        for journey in journeys:
            key = (journey.departure, journey.arrival, journey.is_hack)
            current = best.get(key)
            if current is None:
                best[key] = journey
                continue
            winner, loser = _cheaper(current, journey)
            # Podrobnější trasa vyhrává nad chudší, i když přišla od dražšího.
            if _stop_count(loser) > _stop_count(winner):
                winner.legs = loser.legs
                if loser.provider != winner.provider:
                    winner.notes.append(f"trasa podle {loser.provider}")
            best[key] = winner
        return list(best.values())


@FILTERS.register()
class ParetoOptimal(Filter):
    """Nechá jen nedominované spoje.

    Spoj je dominovaný, když existuje jiný, který je zároveň stejně nebo
    více pohodlný v čase (odjíždí nejdřív stejně a přijíždí nejpozději
    stejně) a zároveň je levnější. Takový spoj nemá smysl nabízet.
    """

    name = "pareto"
    label = "Jen rozumné kompromisy"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        priced = [j for j in journeys if j.price is not None]
        others = [j for j in journeys if j.price is None]
        keep: list[Journey] = []
        for candidate in priced:
            dominated = False
            for other in priced:
                if other is candidate:
                    continue
                if _dominates(other, candidate):
                    dominated = True
                    break
            if not dominated:
                keep.append(candidate)
        return keep + others


@FILTERS.register()
class Limit(Filter):
    """Ořízne počet výsledků."""

    name = "limit"
    label = "Počet výsledků"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        count = int(self.options.get("count", query.max_results))
        return journeys[:count]


# ---- pomocné funkce ---------------------------------------------------------
def _real_transfers(journey: Journey) -> int:
    """Přestupy bez pěších přesunů (ty nejsou přestup na jiný spoj)."""
    return max(0, len([leg for leg in journey.legs if leg.mode is not Mode.WALK]) - 1)


def _stop_count(journey: Journey) -> int:
    return sum(len(leg.stops) for leg in journey.legs)


def _cheaper(a: Journey, b: Journey) -> tuple[Journey, Journey]:
    """Vrátí (levnější, dražší). Spoj bez ceny prohrává."""
    pa, pb = a.price, b.price
    if pa is None:
        return (b, a) if pb is not None else (a, b)
    if pb is None:
        return a, b
    return (a, b) if pa <= pb else (b, a)


def _dominates(better: Journey, worse: Journey) -> bool:
    price_better = better.price is not None and worse.price is not None and better.price < worse.price
    if not price_better:
        return False
    return better.departure >= worse.departure and better.arrival <= worse.arrival
