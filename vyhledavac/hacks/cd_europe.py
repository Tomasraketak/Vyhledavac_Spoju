"""Hacky s Včasnou jízdenkou Evropa (ČD) a regionálními síťovými jízdenkami.

Včasná jízdenka Evropa ČD je výrazně levnější než běžné jízdné – ale jen pro
cestu z Česka do **jedné** sousední (nebo jiné akční) země. Pravidla z cd.cz:
platí 2 dny, je vázaná na vlak mezi ČR a sousední zemí vytištěný na jízdence
a ten se musí použít v celé jeho trase; slevy pro děti a studenty jen v ČR.

1. ``cd_europe_border`` – cesta do třetí země (Třeboň → Bratislava přes
   Vídeň): jízdenka ČD až na poslední stanici tranzitní země (Marchegg,
   Kittsee), dál jízdenka ÖBB/ZSSK; případně ještě lom na první stanici
   cílové země (ÖBB přes hranici, ZSSK po Slovensku).
2. ``cd_europe_beyond`` – cesta po trati Salzburg – Linz – Wien (Wien →
   Linz): jízdenka ČD až do Horního Dvořiště (přes Linz) nebo Břeclavi
   (přes Vídeň) bývá levnější než jízdenka ÖBB; vystoupíte dřív.
3. ``regional_pass`` – jízdenka ČD na první stanici za hranicí a dál
   Einfach-Raus-Ticket (Rakousko, 2–5 osob) nebo Bayern-Ticket, pokud zbytek
   cesty jede jen regionálními vlaky, ve kterých síťová jízdenka platí.

Šetrnost: každý hack se ptá ČD (prohlížeč) nejvýš jednou nebo dvakrát
a jen tehdy, když trasa podmínky opravdu splňuje (zastávka na trase,
kategorie vlaků). Dílčí dotazy sdílí cache enginu.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Sequence

import httpx

from .. import citytransfer, geo
from ..models import Fare, FareStatus, Journey, SearchQuery
from ..money import get_converter
from ..normalize import matches_station, normalize
from ..registry import HACKS
from .base import SearchFn, TravelHack, combine

log = logging.getLogger(__name__)

CD = "ČD"
MIN_TRANSFER = dt.timedelta(minutes=4)
VCASNA = ("Včasná jízdenka Evropa ČD je vázaná na vlak mezi ČR a sousední zemí "
          "vytištěný na jízdence (musí se použít v celé jeho trase), platí 2 dny "
          "a počet jízdenek za akční cenu je omezený.")


def _numbers(line: str) -> set[str]:
    return set(re.findall(r"\b\d{2,6}\b", line or ""))


def continues(first: Journey, second: Journey) -> bool:
    """Navazuje ``second`` na ``first``? Tentýž vlak (stejné číslo, do 3 min),
    nebo přestup s rezervou ``MIN_TRANSFER`` až 3 h."""
    last, nxt = first.legs[-1], second.legs[0]
    gap = second.departure - first.arrival
    if _numbers(last.line) & _numbers(nxt.line) and abs(gap.total_seconds()) <= 180:
        return True
    needed = MIN_TRANSFER
    # v Praze/Brně známe přejezd mezi nádražími (Na Knížecí -> hl. n. metrem)
    move = citytransfer.transfer(first.destination, second.origin)
    if move is not None:
        needed = max(needed, dt.timedelta(minutes=move.needed if move.minutes else 0))
    elif normalize(first.destination) != normalize(second.origin) and (
            citytransfer.hub_of(first.destination)
            or not (matches_station(first.destination, second.origin)
                    or matches_station(second.origin, first.destination))):
        return False         # jiná stanice, přejezd neznáme
    return needed <= gap <= dt.timedelta(hours=3)


def _stops_at(journey: Journey, station: str) -> bool:
    return any(matches_station(station, name) for leg in journey.legs
               for name in [leg.origin, leg.destination, *(s.name for s in leg.stops)])


#: Kdo prodává jízdenky po cílové zemi (druhý lom) a přes hranici.
INLAND = {"SK": ["ZSSK", "RegioJet"], "HU": ["ÖBB"], "SI": ["ÖBB"], "IT": ["ÖBB"],
          "NL": ["FlixBus"], "DK": ["FlixBus"], "CH": ["ÖBB"], "BE": ["FlixBus"], "FR": ["FlixBus"]}
CROSS = ["ÖBB", "RegioJet", "FlixBus"]


# ---------------------------------------------------------------------------------
@HACKS.register()
class CdEuropeBorderHack(TravelHack):
    name = "cd_europe_border"
    label = "Včasná Evropa ČD do tranzitní země + zbytek u ÖBB/ZSSK (např. přes Vídeň)"
    default_min_saving_pct = 5.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        if geo.country_of(query.origin) != "CZ":
            return []
        target = geo.country_of(query.destination)
        if target in (None, "CZ"):
            return []
        baseline = self.baseline_price(journeys)
        found: list[Journey] = []
        tried = 0
        for (transit, dest), pairs in geo.BORDERS.items():
            if dest != target:
                continue
            for last_in_transit, first_in_dest in pairs:
                # jen přechod, přes který některý nalezený spoj opravdu jede
                if not any(_stops_at(j, last_in_transit) or _stops_at(j, first_in_dest)
                           for j in journeys):
                    continue
                if tried >= 2:
                    break
                tried += 1
                found.extend(await self._via(query, last_in_transit, first_in_dest,
                                             baseline, search))
        return _best_per_departure(found)

    async def _via(self, query: SearchQuery, border: str, first_in_dest: str,
                   baseline: Decimal | None, search: SearchFn) -> list[Journey]:
        heads = [j for j in await search(query.leg_query(query.origin, border),
                                         allow_browser=True, only=[CD]) if j.price is not None]
        if not heads:
            return []
        heads.sort(key=lambda j: j.arrival)
        earliest = heads[0].arrival - dt.timedelta(minutes=5)
        tails = [j for j in await search(query.leg_query(border, query.destination, earliest),
                                         only=CROSS) if j.price is not None]
        # druhý lom: přes hranici u ÖBB, po cílové zemi u domácího dopravce (ZSSK)
        cross = [j for j in await search(query.leg_query(border, first_in_dest, earliest),
                                         only=["ÖBB"]) if j.price is not None]
        chains: list[Journey] = []
        sellers = INLAND.get(geo.country_of(query.destination) or "", [])
        if cross and sellers:
            cross.sort(key=lambda j: j.arrival)
            inland = [j for j in await search(
                query.leg_query(first_in_dest, query.destination, cross[0].arrival - dt.timedelta(minutes=5)),
                allow_browser="ZSSK" in sellers, only=sellers) if j.price is not None]
            for a in cross:
                b = _cheapest_next(a, inland)
                if b is not None:
                    merged = combine(f"{a.provider} + {b.provider}", [a, b])
                    if merged is not None:
                        chains.append(merged)
        options = tails + chains
        out = []
        for head in heads:
            tail = _cheapest_next(head, options)
            if tail is None:
                continue
            parts = [head, *(tail.segments or [tail])]
            total = sum((p.price for p in parts), Decimal(0))
            if not self.is_worth_it(total, baseline):
                continue
            lines = "; ".join(f"{p.origin} → {p.destination}: {p.provider} {p.price:.0f} Kč" for p in parts)
            note = (f"🇪🇺 Včasná Evropa ČD jen do {border} (poslední stanice před další zemí), "
                    f"dál samostatná jízdenka: {lines}. Celkem {total:.0f} Kč"
                    + (f" místo {baseline:.0f} Kč" if baseline is not None else "") + ".")
            journey = combine(" + ".join(dict.fromkeys(p.provider for p in parts)), parts,
                              notes=[note, "⚠️ " + VCASNA])
            if journey is not None:
                out.append(journey)
        return out


# ---------------------------------------------------------------------------------
@HACKS.register()
class CdEuropeBeyondHack(TravelHack):
    name = "cd_europe_beyond"
    label = "Rakousko: jízdenka ČD až do Česka (Wien → Linz přes Horní Dvořiště)"
    default_min_saving_pct = 10.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        targets = geo.corridor_targets(query.origin, query.destination)
        if not targets:
            return []
        baseline = self.baseline_price(journeys)
        if baseline is None:
            return []
        target = targets[0]                     # jeden dotaz na ČD stačí
        candidates = await search(query.leg_query(query.origin, target),
                                  allow_browser=True, only=[CD])
        found = []
        async with httpx.AsyncClient() as client:
            for candidate in candidates:
                if candidate.price is None or not self.is_worth_it(candidate.price, baseline):
                    continue
                trimmed = (_trim_at(candidate, query.destination)
                           or _same_departure(candidate, journeys, query.destination)
                           or await _until_border_train(client, candidate, query.destination))
                if trimmed is None:
                    continue
                trimmed.fares = list(candidate.fares)
                trimmed.fare_status = candidate.fare_status
                trimmed.notes = [
                    f"🎫 Jízdenka ČD až do stanice {target} (Včasná Evropa) za {candidate.price:.0f} Kč "
                    f"místo {baseline:.0f} Kč u ÖBB – vystoupíte už v {trimmed.destination} "
                    f"({trimmed.arrival:%H:%M}).",
                    "⚠️ " + VCASNA + " Vystoupení před cílem a nevyužití vázaného vlaku je na "
                    "vaše riziko – zbytek jízdenky propadá.",
                ]
                trimmed.segments = [candidate]
                found.append(trimmed)
        return _best_per_departure(found)


def _trim_at(journey: Journey, destination: str) -> Journey | None:
    """Zkrátí spoj tak, aby končil v ``destination`` (konec úseku nebo zastávka)."""
    for i, leg in enumerate(journey.legs):
        names = [*(s.name for s in leg.stops), leg.destination]
        for k, name in enumerate(names):
            if not matches_station(destination, name):
                continue
            if i == len(journey.legs) - 1 and k == len(names) - 1:
                return None              # cíl je konec jízdenky – žádná úspora
            trimmed = copy.deepcopy(journey)
            trimmed.legs = trimmed.legs[: i + 1]
            last = trimmed.legs[-1]
            if k < len(leg.stops):
                last.stops = last.stops[: k + 1]
                last.arrival = last.stops[-1].arrival or last.arrival
                last.destination = last.stops[-1].name
            trimmed.provider = f"{journey.provider} (jízdenka dál)"
            return trimmed
    return None


def _same_departure(candidate: Journey, journeys: Sequence[Journey],
                    destination: str) -> Journey | None:
    """cd.cz často ukáže jen přeshraniční vlak – úsek do cíle vezmeme ze spoje
    jiného dopravce (ÖBB) se stejným odjezdem, který do cíle opravdu jede."""
    for other in journeys:
        if other.is_hack or not matches_station(destination, other.destination):
            continue
        if abs((other.departure - candidate.departure).total_seconds()) <= 180:
            trimmed = copy.deepcopy(other)
            trimmed.provider = f"{candidate.provider} (jízdenka dál)"
            trimmed.deep_link = candidate.deep_link
            return trimmed
    return None


async def _until_border_train(client, candidate: Journey, destination: str) -> Journey | None:
    """Cíl je výchozí stanice přeshraničního vlaku (Linz Hbf): příjezd tam
    nejpozději v jeho odjezd. Trasa vlaku z cd.cz."""
    from .. import cdtrain
    from ..models import Leg, Mode

    leg = candidate.legs[-1]
    stops = await cdtrain.train_stops(client, leg.line, leg.departure)
    if not stops or not matches_station(destination, stops[0].name) or stops[0].departure is None:
        return None
    border_dep = stops[0].departure.replace(tzinfo=candidate.departure.tzinfo)
    if border_dep <= candidate.departure:
        return None
    trimmed = copy.deepcopy(candidate)
    trimmed.legs = [Leg(origin=candidate.origin, destination=stops[0].name,
                        departure=candidate.departure, arrival=border_dep - dt.timedelta(minutes=5),
                        mode=Mode.TRAIN, carrier="ÖBB", line="vlak do " + stops[0].name)]
    trimmed.provider = f"{candidate.provider} (jízdenka dál)"
    return trimmed


# ---------------------------------------------------------------------------------
@HACKS.register()
class RegionalPassHack(TravelHack):
    name = "regional_pass"
    label = "ČD za hranici + Einfach-Raus-Ticket / Bayern-Ticket (jen regionální vlaky)"
    default_min_saving_pct = 5.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        if geo.country_of(query.origin) != "CZ":
            return []
        people = len(query.passengers)
        baseline = self.baseline_price(journeys)
        found: list[Journey] = []
        asked = 0
        for pass_ in geo.PASSES:
            price_eur = pass_.price(people)
            if price_eur is None or not pass_.covers(query.destination):
                continue
            pass_czk = get_converter().to_czk(price_eur, "EUR")
            if pass_czk is None:
                continue
            for entry in pass_.entries:
                plans = [p for p in (self._tail(j, entry, pass_) for j in journeys) if p]
                if not plans or asked >= 2:
                    continue
                asked += 1
                heads = [j for j in await search(query.leg_query(query.origin, entry),
                                                 allow_browser=True, only=[CD]) if j.price is not None]
                for base, legs in plans:
                    head = _cheapest_arriving(heads, legs[0].departure)
                    if head is None:
                        continue
                    total = head.price + pass_czk
                    if not self.is_worth_it(total, baseline):
                        continue
                    rest = Journey(provider=pass_.name, legs=legs, fare_status=FareStatus.PRICED,
                                   fares=[Fare(amount=price_eur, currency="EUR", amount_czk=pass_czk,
                                               label=pass_.name, tariff=f"{people} os.")],
                                   deep_link=pass_.url)
                    note = (f"🎟️ Jízdenka ČD do {entry} ({head.price:.0f} Kč) a dál {pass_.name} "
                            f"za {price_eur:.0f} € (≈ {pass_czk:.0f} Kč) – zbytek cesty jede jen "
                            f"regionálními vlaky ({', '.join(sorted({l.line.split()[0] for l in legs}))}).")
                    warn = (f"⚠️ {pass_.name} platí jen v regionálních vlacích "
                            f"({', '.join(pass_.categories)}), ve všední dny od 9:00; ne v RJ/IC/EC/ICE.")
                    journey = combine(f"ČD + {pass_.name}", [head, rest], notes=[note, warn])
                    if journey is not None:
                        found.append(journey)
        return _best_per_departure(found)

    @staticmethod
    def _tail(journey: Journey, entry: str, pass_: geo.RegionalPass):
        """Úseky spoje od vstupní stanice dál – jen když všechny jedou regionálními vlaky."""
        for i, leg in enumerate(journey.legs):
            if matches_station(entry, leg.origin):
                rest = journey.legs[i:]
                if all(pass_.train_ok(l.line) for l in rest if l.mode.value != "walk") \
                        and pass_.valid_at(rest[0].departure):
                    return journey, rest
                return None
        return None


# ---- pomůcky ---------------------------------------------------------------------
def _cheapest_next(first: Journey, options: Sequence[Journey]) -> Journey | None:
    best = None
    for option in options:
        if option.price is None or not continues(first, option):
            continue
        if best is None or (option.price, option.arrival) < (best.price, best.arrival):
            best = option
    return best


def _cheapest_arriving(heads: Sequence[Journey], by: dt.datetime) -> Journey | None:
    ok = [h for h in heads if dt.timedelta(0) <= by - h.arrival <= dt.timedelta(hours=2)
          or abs((by - h.arrival).total_seconds()) <= 180]
    return min(ok, key=lambda h: (h.price, -h.arrival.timestamp()), default=None)


def _best_per_departure(journeys: list[Journey]) -> list[Journey]:
    best: dict[dt.datetime, Journey] = {}
    for j in journeys:
        if j.price is None:
            continue
        if j.departure not in best or j.price < best[j.departure].price:
            best[j.departure] = j
    return sorted(best.values(), key=lambda j: j.departure)
