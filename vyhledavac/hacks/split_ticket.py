"""Lomená jízdenka (split ticket).

Myšlenka: zůstanete sedět v jednom vlaku, ale místo jedné jízdenky A→B
koupíte dvě – A→X a X→B. U přeshraničních cest to často vyjde výrazně
levněji, protože vnitrostátní tarif bývá levnější než mezinárodní, a
u českých slev (ISIC, Junior) platí sleva jen na domácím úseku.

Proti původnímu ``RouteFirstEngine`` se liší v těchto bodech:

* **Body lomu se hledají ve skutečné trase vlaku**, ne jen v natvrdo
  zapsaném seznamu dvojic. Seznam hraničních stanic zůstal, ale slouží
  jen k upřednostnění kandidátů – když vlak jede přes jiný vhodný uzel,
  najde se taky.
* **Navazování úseků je poctivé.** Původní kód považoval dva spoje za
  tentýž vlak i tehdy, když se odjezd a příjezd lišily o méně než 3 minuty
  (``elif diff <= 3: same_train = True``) – to spojovalo i úplně cizí
  vlaky. Tady se porovnává označení spoje a čas.
* **Nic se nevymýšlí.** Když u dílčího úseku chybí cena, varianta se
  označí, místo aby se chybějící cena tiše nahradila nulou.
* Dotazy na dílčí úseky běží paralelně a je jich omezený počet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Sequence

from ..models import Journey, SearchQuery
from ..normalize import matches_station, normalize, same_place
from ..registry import HACKS
from .base import SearchFn, TravelHack, combine, connects

log = logging.getLogger(__name__)

#: Body, na které se nedá koupit jízdenka – státní hranice, odbočky, závorky
#: s upřesněním nástupiště. Jako bod lomu nedávají smysl.
UNTICKETABLE = (
    "statni hranice", "staatsgrenze", "grenze", "hranica", "border",
    "odb", "zastavka na znameni",
)

#: Stanice, kde se vyplatí lomit nejčastěji – typicky hraniční přechody.
#: Slouží k *upřednostnění*, ne k omezení.
PREFERRED_NODES = (
    "Břeclav", "Kúty", "Petržalka", "Marchegg", "Devínska Nová Ves", "Kittsee",
    "Summerau", "Horní Dvořiště", "České Velenice", "Gmünd", "Bad Schandau",
    "Děčín", "Čadca", "Mosty u Jablunkova", "Bohumín", "Petrovice u Karviné",
    "Lanžhot", "Znojmo", "Šatov", "Hevlín", "Český Těšín", "Zebrzydowice",
    "Furth im Wald", "Domažlice", "Cheb", "Schirnding", "Vojtanov",
    "Wien Hbf", "Wien Meidling", "Praha hl.n.", "Brno hl.n.", "Ostrava hl.n.",
    "Bratislava hlavná stanica", "Žilina", "Linz/Donau Hbf", "Salzburg Hbf",
)


@HACKS.register()
class SplitTicketHack(TravelHack):
    name = "split_ticket"
    label = "Lomená jízdenka (rozdělení na dva úseky)"
    default_min_saving_pct = 3.0

    async def find(
        self, query: SearchQuery, journeys: Sequence[Journey], search: SearchFn
    ) -> list[Journey]:
        candidates = self._candidate_nodes(query, journeys)
        if not candidates:
            log.info("split_ticket: na trase nebyl nalezen vhodný bod lomu")
            return []

        max_nodes = int(self.options.get("max_nodes", 4))
        candidates = candidates[:max_nodes]
        log.info("split_ticket: zkouším lomit přes %s", ", ".join(candidates))

        baseline = self.baseline_price(journeys)
        results = await asyncio.gather(
            *(self._try_node(query, node, baseline, search) for node in candidates),
            return_exceptions=True,
        )

        found: list[Journey] = []
        for node, outcome in zip(candidates, results):
            if isinstance(outcome, Exception):
                log.warning("split_ticket: uzel %s selhal (%s)", node, outcome)
                continue
            found.extend(outcome)
        return found

    # ---- výběr bodů lomu -----------------------------------------------------
    def _candidate_nodes(
        self, query: SearchQuery, journeys: Sequence[Journey]
    ) -> list[str]:
        """Posbírá zastávky na skutečných trasách a seřadí je podle vhodnosti.

        Vynechává výchozí a cílovou stanici – lomit na koncích nedává smysl.
        """
        seen: dict[str, int] = {}
        for journey in journeys:
            if journey.is_hack:
                continue
            for leg in journey.legs:
                for stop in leg.stops:
                    name = clean_station_name(stop.name or "")
                    if not name:
                        continue
                    if same_place(name, query.origin) or same_place(name, query.destination):
                        continue
                    if not is_ticketable(name):
                        continue
                    seen[name] = seen.get(name, 0) + 1

        if not seen:
            return []

        def rank(name: str) -> tuple[int, int]:
            preferred = any(matches_station(node, name) for node in PREFERRED_NODES)
            # Nejdřív preferované uzly, pak ty na nejvíce trasách.
            return (0 if preferred else 1, -seen[name])

        return sorted(seen, key=rank)

    # ---- vyhodnocení jednoho uzlu -------------------------------------------
    async def _try_node(
        self, query: SearchQuery, node: str, baseline, search: SearchFn
    ) -> list[Journey]:
        first_leg = await search(query.leg_query(query.origin, node))
        if not first_leg:
            return []

        min_transfer = dt.timedelta(minutes=float(self.options.get("min_transfer_minutes", 4)))

        # Druhý úsek hledáme od nejbližšího reálného příjezdu do uzlu.
        usable = [j for j in first_leg if j.price is not None] or list(first_leg)
        usable.sort(key=lambda j: j.arrival)
        earliest = usable[0].arrival - dt.timedelta(minutes=5)

        second_leg = await search(query.leg_query(node, query.destination, earliest))
        if not second_leg:
            return []

        combos: list[Journey] = []
        for first in usable[: int(self.options.get("max_first_legs", 6))]:
            best = self._best_continuation(first, second_leg, min_transfer)
            if best is None:
                continue
            second, same_train = best
            total = _total(first, second)
            if total is None or not self.is_worth_it(total, baseline):
                continue

            saving = f"{baseline - total:.0f} Kč" if baseline is not None else "?"
            note = (
                f"✂️ Lomeno v {node}: zůstáváte ve stejném vlaku, "
                f"jen máte dvě jízdenky (úspora {saving})"
                if same_train
                else f"✂️ Lomeno v {node} s přestupem (úspora {saving})"
            )
            merged = combine(
                f"{first.provider} + {second.provider}",
                [first, second],
                notes=[note],
            )
            if merged is not None:
                combos.append(merged)

        return _best_per_departure(combos)

    def _best_continuation(
        self, first: Journey, options: Sequence[Journey], min_transfer: dt.timedelta
    ) -> tuple[Journey, bool] | None:
        """Najde nejlevnější platné pokračování; tentýž vlak má přednost."""
        best: tuple[Journey, bool] | None = None
        best_key: tuple | None = None
        for option in options:
            ok, same_train = connects(first, option, min_transfer)
            if not ok:
                continue
            price = option.price
            # Klíč: nejdřív stejný vlak, pak nejnižší cena, pak dřívější příjezd.
            key = (0 if same_train else 1,
                   price if price is not None else 10 ** 9,
                   option.arrival)
            if best_key is None or key < best_key:
                best, best_key = (option, same_train), key
        return best


def clean_station_name(name: str) -> str:
    """Očistí název stanice z jízdního řádu na podobu, kterou zná prodejce.

    HAFAS připojuje upřesnění v závorce ("Wien Hbf (Bahnsteige 3-12)"),
    které našeptávače dopravců neznají.
    """
    cleaned = re.sub(r"\s*\([^)]*\)", "", name)
    return cleaned.strip(" -–,") or name.strip()


def is_ticketable(name: str) -> bool:
    """Dá se do téhle stanice koupit jízdenka?

    HAFAS vrací i body, které stanice nejsou – hlavně státní hranice
    ("Břeclav státní hranice/Staatsgrenze"). Lomit na nich nelze a dotazy
    na ně jen zbytečně zatěžují dopravce.
    """
    low = normalize(name)
    return bool(low) and not any(marker in low for marker in UNTICKETABLE)


def _total(first: Journey, second: Journey):
    if first.price is None or second.price is None:
        return None
    return first.price + second.price


def _best_per_departure(journeys: list[Journey]) -> list[Journey]:
    """Na jeden odjezd nabídneme jen nejlevnější variantu."""
    best: dict[dt.datetime, Journey] = {}
    for journey in journeys:
        current = best.get(journey.departure)
        if current is None:
            best[journey.departure] = journey
            continue
        if journey.price is not None and (
            current.price is None or journey.price < current.price
        ):
            best[journey.departure] = journey
    return sorted(best.values(), key=lambda j: j.departure)
