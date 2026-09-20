"""Skryté město (hidden city / throwaway ticket).

Někdy je jízdenka do vzdálenější stanice levnější než do té, kam
skutečně jedete – typicky když je cílová stanice ve vyšší tarifní zóně
nebo když dopravce zlevňuje koncové stanice linky. Koupíte tedy jízdenku
A→C a vystoupíte v B.

Podmínky, které hlídáme:

* vlak musí v B **skutečně zastavovat** (ověřujeme v seznamu zastávek);
* B musí ležet na trase **před** C;
* spoj nesmí vyžadovat místenku vázanou na konkrétní úsek – u takových
  spojů (RegioJet, FlixBus) to nefunguje a variantu nenabízíme.

Varování pro uživatele je součástí výsledku: zpáteční část jízdenky
propadá a někteří dopravci to v podmínkách zakazují.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..models import Journey, SearchQuery
from ..normalize import matches_station, same_place
from ..registry import HACKS
from .base import SearchFn, TravelHack

log = logging.getLogger(__name__)

#: Kam dál za cíl se zkouší jízdenka. Rozšiřitelné volbou ``beyond``.
DEFAULT_BEYOND = {
    "praha": ["Kolín", "Pardubice hl.n.", "Ústí nad Labem hl.n."],
    "brno": ["Břeclav", "Vyškov na Moravě", "Blansko"],
    "vieden": ["Wien Meidling", "Wiener Neustadt Hbf", "St.Pölten Hbf"],
    "bratislava": ["Trnava", "Kúty", "Nové Zámky"],
    "ostrava": ["Bohumín", "Český Těšín"],
}


@HACKS.register()
class HiddenCityHack(TravelHack):
    name = "hidden_city"
    label = "Skryté město (vystoupíte dřív, než končí jízdenka)"
    default_min_saving_pct = 8.0   # vyšší práh: má to nevýhody, musí se vyplatit

    async def find(
        self, query: SearchQuery, journeys: Sequence[Journey], search: SearchFn
    ) -> list[Journey]:
        targets = self._beyond_targets(query)
        if not targets:
            return []

        baseline = self.baseline_price(journeys)
        if baseline is None:
            # Bez ceny přímého spoje nemáme s čím porovnávat.
            return []

        found: list[Journey] = []
        for target in targets[: int(self.options.get("max_targets", 3))]:
            try:
                candidates = await search(query.leg_query(query.origin, target))
            except Exception as exc:
                log.warning("hidden_city: %s selhalo (%s)", target, exc)
                continue

            for candidate in candidates:
                journey = self._evaluate(candidate, query, target, baseline)
                if journey is not None:
                    found.append(journey)

        return _cheapest_per_departure(found)

    def _beyond_targets(self, query: SearchQuery) -> list[str]:
        explicit = self.options.get("beyond")
        if explicit:
            return list(explicit)
        from ..normalize import normalize

        key = normalize(query.destination).replace(" ", "")
        for name, targets in DEFAULT_BEYOND.items():
            if key.startswith(name) or name in key:
                return targets
        return []

    def _evaluate(
        self, candidate: Journey, query: SearchQuery, target: str, baseline
    ) -> Journey | None:
        price = candidate.price
        if price is None or not self.is_worth_it(price, baseline):
            return None

        # Musí zastavit v našem skutečném cíli...
        alight = _find_alight(candidate, query.destination)
        if alight is None:
            return None
        leg_index, stop_index = alight

        # ...a nesmí to být spoj s povinnou místenkou na celý úsek.
        if any(leg.reservation_required for leg in candidate.legs):
            return None

        real_arrival = candidate.legs[leg_index].stops[stop_index].arrival
        if real_arrival is None or real_arrival <= candidate.departure:
            return None

        saving = baseline - price
        trimmed = _trim_to(candidate, leg_index, stop_index, query.destination)
        trimmed.notes.append(
            f"🎭 Jízdenka až do {target}, ale vystoupíte v {query.destination} "
            f"({real_arrival:%H:%M}). Úspora {saving:.0f} Kč."
        )
        trimmed.notes.append(
            "⚠️ Zbytek jízdenky propadá a někteří dopravci tento postup "
            "v přepravních podmínkách zakazují."
        )
        trimmed.segments = [candidate]
        return trimmed


def _find_alight(journey: Journey, destination: str) -> tuple[int, int] | None:
    """Najde poslední zastávku odpovídající cíli – ale ne úplný konec trasy."""
    for leg_index, leg in enumerate(journey.legs):
        for stop_index, stop in enumerate(leg.stops):
            if not matches_station(destination, stop.name):
                continue
            is_final = leg_index == len(journey.legs) - 1 and stop_index == len(leg.stops) - 1
            if is_final:
                return None      # cíl je koncová – žádné skryté město
            return leg_index, stop_index
    return None


def _trim_to(journey: Journey, leg_index: int, stop_index: int, destination: str) -> Journey:
    """Zkrátí spoj tak, aby končil ve stanici, kde skutečně vystoupíme."""
    import copy

    trimmed = copy.deepcopy(journey)
    trimmed.legs = trimmed.legs[: leg_index + 1]
    last = trimmed.legs[-1]
    last.stops = last.stops[: stop_index + 1]
    arrival = last.stops[-1].arrival or last.arrival
    last.arrival = arrival
    last.destination = last.stops[-1].name or destination
    trimmed.provider = f"{journey.provider} (skryté město)"
    return trimmed


def _cheapest_per_departure(journeys: list[Journey]) -> list[Journey]:
    best: dict = {}
    for journey in journeys:
        key = journey.departure
        current = best.get(key)
        if current is None or (
            journey.price is not None
            and (current.price is None or journey.price < current.price)
        ):
            best[key] = journey
    return sorted(best.values(), key=lambda j: j.departure)
