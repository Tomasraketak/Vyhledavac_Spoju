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

from .. import geo
from ..models import Journey, SearchQuery
from ..normalize import matches_station, normalize, same_place
from ..registry import HACKS
from ..trace import trace
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

        abroad = len({c for c in (geo.country_of(query.origin), geo.country_of(query.destination))}) > 1
        max_nodes = int(self.options.get("max_nodes", MAX_NODES_ABROAD if abroad else 4))
        candidates = candidates[:max_nodes]
        log.info("split_ticket: zkouším lomit přes %s", ", ".join(candidates))

        baseline = self.baseline_price(journeys)
        results = await asyncio.gather(
            *(self._try_node(query, node, baseline, search, journeys) for node in candidates),
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
        Z každého města jen jednu stanici (Děčín hl.n., Děčín-Přípeř a Děčín-
        Prostřední Žleb jsou pro jízdenku totéž). Nejdřív stanice na hranici
        (známé uzly i místa, kde vlak přejede do jiné země), pak velká města
        na trase – na dlouhé cestě (Praha → Amsterdam) se vyplatí lomit
        i v Drážďanech, Frankfurtu nebo Kolíně, kde začíná levnější dopravce.
        """
        seen: dict[str, int] = {}
        border: dict[str, frozenset[str]] = {}      # stanice → dvojice zemí, mezi kterými leží
        ends = {_city_key(query.origin), _city_key(query.destination)}
        for journey in journeys:
            if journey.is_hack:
                continue
            for leg in journey.legs:
                names = [clean_station_name(stop.name or "") for stop in leg.stops]
                countries = [geo.country_of(name) if name else None for name in names]
                times = [stop.time for stop in leg.stops]
                for i, name in enumerate(names):
                    if not name:
                        continue
                    if same_place(name, query.origin) or same_place(name, query.destination) \
                            or _city_key(name) in ends:
                        continue                  # lom ve výchozím či cílovém městě nic nepřinese
                    if not is_ticketable(name):
                        continue
                    seen[name] = seen.get(name, 0) + 1
                    # poslední stanice před hranicí / první za ní: soused v jiné zemi
                    # kousek vedle (trasa jen s pár zastávkami – Praha, Drážďany –
                    # by jinak udělala z Drážďan hraniční stanici)
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(names) and countries[i] and countries[j] \
                                and countries[j] != countries[i] and _close(times[i], times[j]):
                            border[name] = frozenset((countries[i], countries[j]))

        if not seen:
            return []

        groups: dict[str, list[str]] = {}
        for name in seen:
            groups.setdefault(_city_key(name), []).append(name)

        def count(city: str) -> int:
            return sum(seen[name] for name in groups[city])

        def station(city: str) -> str:
            # stanice z jízdního řádu, kterou trasy používají nejčastěji
            return max(groups[city], key=lambda n: (seen[n], -len(n)))

        def pair(city: str) -> frozenset[str] | None:
            pairs = [border[n] for n in groups[city] if n in border]
            if pairs:
                return pairs[0]
            if any(matches_station(node, n) for node in PREFERRED_NODES for n in groups[city]):
                return frozenset({"?", city})        # známý uzel lomu (Břeclav, Linz …)
            return None

        # z každého přechodu hranice (CZ|DE, DE|NL …) jen nejpoužívanější stanice
        by_pair: dict[frozenset[str], list[str]] = {}
        for city in groups:
            key = pair(city)
            if key is not None:
                by_pair.setdefault(key, []).append(city)
        edge = sorted((max(cities, key=count) for cities in by_pair.values()), key=lambda c: -count(c))
        rest = [c for c in groups if c not in edge]
        # velká města, kudy vede víc tras, dřív než vesnice na hlavní trati
        hubs = sorted(rest, key=lambda c: (_city_score(c) < HUB_SCORE, -count(c), -_city_score(c)))
        return [station(c) for c in [*edge[:MAX_BORDER_NODES], *hubs, *edge[MAX_BORDER_NODES:]]]

    # ---- vyhodnocení jednoho uzlu -------------------------------------------
    async def _try_node(
        self, query: SearchQuery, node: str, baseline, search: SearchFn,
        journeys: Sequence[Journey] = (),
    ) -> list[Journey]:
        first_leg = await search(query.leg_query(query.origin, node))
        if not first_leg:
            trace(f"✂ lom v {node}: do uzlu nic nejede (nebo to nikdo neprodá)")
            return []

        min_transfer = dt.timedelta(minutes=float(self.options.get("min_transfer_minutes", 4)))

        # Druhý úsek hledáme od nejbližšího reálného příjezdu do uzlu.
        usable = [j for j in first_leg if j.price is not None] or list(first_leg)
        usable.sort(key=lambda j: j.arrival)
        earliest = usable[0].arrival - dt.timedelta(minutes=5)

        second_leg = await search(query.leg_query(node, query.destination, earliest))
        if not second_leg:
            trace(f"✂ lom v {node}: z uzlu do cíle nic nejede (nebo to nikdo neprodá)")
            return []

        combos: list[Journey] = []
        rejected: list[str] = []
        for first in usable[: int(self.options.get("max_first_legs", 6))]:
            best = self._best_continuation(first, second_leg, min_transfer)
            if best is None:
                continue
            second, same_train = best
            total = _total(first, second)
            if total is None:
                continue
            # levnější než všechno – nebo aspoň nikdo nejede dřív i rychleji za stejně
            # (vlak po kusech levněji než tentýž vlak celý, i když autobus vyjde levněji)
            if not self.is_worth_it(total, baseline):
                better = _dominated(first.departure, second.arrival, total, journeys)
                if better is not None:
                    rejected.append(f"{first.departure:%H:%M}–{second.arrival:%H:%M} {total:.0f} Kč "
                                    f"(lepší {better.provider} {better.departure:%H:%M}–{better.arrival:%H:%M} "
                                    f"{better.price:.0f} Kč)")
                    continue

            similar = _similar_price(first.departure, second.arrival, journeys)
            if baseline is not None and total < baseline:
                saving = f" (úspora {baseline - total:.0f} Kč proti nejlevnějšímu spoji)"
            elif similar is not None and total < similar:
                saving = f" (úspora {similar - total:.0f} Kč proti spoji ve stejný čas)"
            else:
                saving = ""
            note = (
                f"✂️ Lomeno v {node}: zůstáváte ve stejném vlaku, jen máte dvě jízdenky{saving}"
                if same_train
                else f"✂️ Lomeno v {node} s přestupem{saving}"
            )
            merged = combine(
                f"{first.provider} + {second.provider}",
                [first, second],
                notes=[note],
            )
            if merged is not None:
                combos.append(merged)

        found = _best_per_departure(combos)
        trace(f"✂ lom v {node}: {len(first_leg)} spojů do uzlu, {len(second_leg)} z uzlu → "
              + (", ".join(f"{j.provider} {j.departure:%H:%M}–{j.arrival:%H:%M} {j.price:.0f} Kč" for j in found)
                 or "nic výhodného")
              + (f"; nenabízím {', '.join(rejected[:3])}" if rejected else ""))
        return found

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


#: Kolik uzlů lomu zkusit na cestě do ciziny (dvě otázky na uzel, souběžně).
MAX_NODES_ABROAD = 6
#: … z toho nejvýš tolik hraničních – zbytek velká města na trase.
MAX_BORDER_NODES = 3


def _city_key(name: str) -> str:
    """Město stanice („Děčín-Přípeř“ → Děčín, „Dresden Hbf“ → Drážďany,
    „Wien Hbf“ i „Vídeň, hlavní nádraží“ → Vídeň)."""
    from .. import city, stations

    found = city.city_of(name)
    if found is not None:
        return found[0]
    place = stations.place(name)
    if place is None:
        return normalize(name)
    return place.name if place.kind == "c" else (place.obec or place.name)


def _city_score(city: str) -> int:
    """Velikost (důležitost) města – větší uzel = víc dopravců a spojů."""
    from .. import stations

    place = stations.place(city)
    return place.score if place is not None else 0


#: Velké město (skóre v databázi míst) – uzel, kde se vyplatí lomit.
HUB_SCORE = 10000
#: Sousední zastávky do tolika minut od sebe – pak je změna země opravdu hranice.
BORDER_GAP = dt.timedelta(minutes=45)


def _close(a: dt.datetime | None, b: dt.datetime | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= BORDER_GAP


def _dominated(departure: dt.datetime, arrival: dt.datetime, total,
               journeys: Sequence[Journey]) -> Journey | None:
    """Spoj, který jede stejně pozdě nebo později, dorazí stejně brzy nebo dřív
    a stojí nejvýš tolik – pak kombinace nemá smysl (``None`` = žádný takový)."""
    slack = dt.timedelta(minutes=2)
    return next((j for j in journeys if j.price is not None and not j.is_hack and j.price <= total
                 and j.departure >= departure - slack and j.arrival <= arrival + slack), None)


def _similar_price(departure: dt.datetime, arrival: dt.datetime, journeys: Sequence[Journey]):
    """Nejnižší cena spoje zhruba ve stejný čas (odjezd i příjezd do hodiny)."""
    hour = dt.timedelta(hours=1)
    prices = [j.price for j in journeys if j.price is not None and not j.is_hack
              and abs(j.departure - departure) <= hour and abs(j.arrival - arrival) <= hour]
    return min(prices, default=None)


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
    if re.match(r"^[A-Z]{2}\s*/\s*[A-Z]{2}\b", name.strip()):
        return False                     # „CZ/PL - Petrovice u Karviné / Zebrzydowice“ – hraniční bod
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
