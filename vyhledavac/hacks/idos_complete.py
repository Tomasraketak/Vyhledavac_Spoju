"""Dopočet ceny spojů z IDOS, kde jízdenka IDS nepokryje celou cestu.

Domanín → Praha: autobus IDESKA do Českých Budějovic (cenu spočítá tarif
IDESKA) a dál rychlík do Prahy – ten IDS celý nepokrývá. Dopravce „IDS“
takový spoj vrátí bez ceny a s několika rozpisy jízdenek v ``raw["plans"]``:

* IDS na celé úseky (autobus), vlak celý u dopravce;
* IDS co nejdál – i vlakem, do poslední zastávky v systému, kde vlak
  staví (Domanín → Tábor IDESKA), zbytek u dopravce (Tábor → Praha), případně
  zase IDS dalšího systému (PID);
* IDS co nejdál a zbytek vlaku jednou jízdenkou.

Tenhle hack se na každý dokupovaný kus zeptá dopravců a vezme jízdenku na
**tentýž spoj** (odjezd a příjezd do pár minut). Nabídne nejlevnější rozpis,
který jde ocenit celý.

Kdo prodává: po Česku Arriva/SJT, Bileto, RegioJet, FlixBus, Leo Express
a – jsou-li zapnuté – ČD a OneTicket přes prohlížeč. Úsek do ciziny nebo
v cizině ocení dopravci, kteří obsluhují obě země (ÖBB, DB, PKP, MÁV,
RegioJet…; ČD a ZSSK přes prohlížeč).

Když jízdenku na celý kus nikdo neprodá, kus se rozdělí – nejdřív co
nejdelší začátek, zbytek znovu (rekurzivně): v přestupních stanicích
a ve stanicích na státní hranici, kde vlak staví. Domanín → Bad Ischl
(IDOS: autobus a vlak do Českých Budějovic, EC do Linze, dál ÖBB):

1. IDESKA Domanín → České Budějovice (tarif IDS);
2. ČD / ÖBB České Budějovice → Bad Ischl jednou jízdenkou;
3. když ji nikdo neprodá: České Budějovice → Linz (ČD/ÖBB) a Linz → Bad
   Ischl (ÖBB), případně lom na hranici (Horní Dvořiště / Summerau).

Brno → Malacky s přestupem v Kútech: celá cesta u ČD/ZSSK, jinak Brno →
Kúty (ČD) a Kúty → Malacky (ZSSK).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from .. import geo
from ..models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import normalize, same_place
from ..registry import HACKS, PROVIDERS
from .base import SearchFn, TravelHack, combine

log = logging.getLogger(__name__)

MAX_JOURNEYS = 6
#: Kdo prodává jízdenky na úseky mimo IDS (bez prohlížeče).
SELLERS = ["Arriva", "Bileto", "RegioJet", "FlixBus", "LeoExpress"]
#: Prodejci jízdenek na vlaky ČD přes prohlížeč – ptáme se jich, jen když je
#: uživatel zapnul (engine je jinak vynechá), a jen na vlaky ČD.
BROWSER_SELLERS = ["ČD", "OneTicket"]
SAME_DEPARTURE = dt.timedelta(minutes=3)
SAME_ARRIVAL = dt.timedelta(minutes=6)
#: Kus delší než tolik úseků se dělí jen po jednotlivých úsecích (šetří dotazy).
MAX_RUN_LEGS = 4
#: Hloubka rekurzivního dělení kusu (= nejvýš tolik jízdenek navíc).
MAX_SPLITS = 3
#: Prodejci přes prohlížeč podle země: ČD prodá cestu z Česka do okolních zemí,
#: ZSSK po Slovensku a ze Slovenska.
BROWSER_COUNTRIES = {"ČD": {"CZ", "AT", "DE", "SK", "PL", "HU"}, "ZSSK": {"SK", "CZ", "AT", "HU"}}
#: Zdroje, které jízdenky neprodávají (jen jízdní řád / tarif IDS).
NOT_SELLERS = {"IDS", "Transitous"}


@HACKS.register()
class IdosCompleteHack(TravelHack):
    name = "idos_complete"
    label = "Spoje z IDOS: úseky mimo IDS ocení dopravci"
    default_min_saving_pct = 0.0

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        todo = [j for j in journeys if j.provider == "IDS" and (j.raw.get("plans") or j.raw.get("unpriced"))]
        # nejdřív spoje bez ceny, pak ty, kde jde jen o možnou úsporu
        todo.sort(key=lambda j: j.price is not None)
        todo = todo[:MAX_JOURNEYS]
        # Výsledky dotazů podle (odkud, kam): dotaz z 8:00 Budějovice → Praha
        # vrátí i vlak v 10:10, další spoj se tedy už ptát nemusí.
        self._seen: dict[tuple[str, str, bool], list[Journey]] = {}
        # spoje z hlavního hledání: celá cesta jedním vlakem už může mít cenu
        self._base = [j for j in journeys if j.provider not in NOT_SELLERS and j.price is not None]
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
        best = None
        for plan in _plans(journey):
            segments = await self._price_plan(query, journey, plan, search)
            if segments is None:
                continue
            total = sum((s.price for s in segments), Decimal(0))
            if best is None or total < best[0]:
                best = (total, segments)
        if best is None or (journey.price is not None and best[0] >= journey.price):
            return None                      # IDS samo vyjde levněji
        rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
        if any(o.price <= best[0] for o in self._base if _same(o, rides[0], rides[-1])):
            return None                      # tentýž spoj už má z hledání stejnou nebo nižší cenu
        segments = best[1]
        lines = "; ".join(f"{s.origin} → {s.destination}: {s.provider} {s.price:.0f} Kč" for s in segments)
        merged = combine(" + ".join(dict.fromkeys(s.provider for s in segments)), segments,
                         notes=[f"🧩 Spoj z IDOS s jízdenkami: {lines}."])
        if merged is not None:
            if not any(s.raw.get("other_route") for s in segments):
                merged.legs = list(journey.legs)      # časy, zastávky a pěší přesuny z IDOS
            merged.deep_link = journey.deep_link
        return merged

    async def _price_plan(self, query: SearchQuery, journey: Journey, plan: list[dict],
                          search: SearchFn) -> list[Journey] | None:
        legs = [leg for leg in journey.legs if leg.mode is not Mode.WALK]   # pozice v rozpisu = jízdy
        if not plan or plan[0]["from"] != [0, 0] \
                or plan[-1]["to"] != [len(legs) - 1, max(len(legs[-1].stops), 2) - 1]:
            return None                      # rozpis nepokrývá celou cestu
        segments: list[Journey] = []
        for seg in plan:
            part = _slice(legs, seg["from"], seg["to"])
            if not part:
                return None
            if seg["kind"] == "ids":
                price = Decimal(seg["price"])
                segments.append(Journey(
                    provider=seg["system"], legs=part, fare_status=FareStatus.PRICED,
                    fares=[Fare(amount=price, currency="CZK", amount_czk=price, label=seg["system"],
                                tariff=seg["name"])]))
                continue
            bought = await self._price_run(query, part, search, MAX_SPLITS)
            if bought is None:
                return None
            segments.extend(bought)
        return segments

    async def _price_run(self, query: SearchQuery, run: list[Leg], search: SearchFn,
                         splits: int) -> list[Journey] | None:
        """Jízdenky na souvislý kus cesty: celý u jednoho prodejce, jinak
        co nejdelší začátek + zbytek (rekurzivně) – lom v přestupní stanici
        nebo ve stanici na hranici státu, kde vlak staví."""
        whole = await self._seller(query, run, search)
        if whole is not None:
            return [whole]
        if splits <= 0:
            return None
        points = _split_points(run)
        if len(run) > MAX_RUN_LEGS:          # dlouhý kus: jen po celých úsecích
            points = [p for p in points if p[1] == 0]
        for point in reversed(points):        # nejdelší začátek první
            head, tail = _split(run, point)
            if not head or not tail:
                continue
            first = await self._seller(query, head, search)
            if first is None:
                continue
            rest = await self._price_run(query, tail, search, splits - 1)
            if rest is not None:
                return [first, *rest]
        return None

    async def _seller(self, query: SearchQuery, run: list[Leg], search: SearchFn) -> Journey | None:
        first, last = run[0], run[-1]
        origin, destination = _full(first.origin), _full(last.destination)
        from_base = _match(self._base, first, last, origin, destination)
        if from_base is not None:
            return _on_route(from_base, run)
        plain, browser = sellers_for(origin, destination, run)
        asks = [(plain, False)] + ([(browser, True)] if browser else [])
        options: list[Journey] = []
        for only, allow_browser in asks:
            if not only:
                continue
            key = (normalize(origin), normalize(destination), allow_browser)
            cached = self._seen.get(key, [])
            if not any(_same(o, first, last) for o in cached):
                fresh = await search(query.leg_query(origin, destination, first.departure - dt.timedelta(minutes=2)),
                                     allow_browser=allow_browser, only=only)
                cached = cached + list(fresh)
                self._seen[key] = cached
            options.extend(cached)
        options = [o for o in options if o.provider not in NOT_SELLERS]
        same = [o for o in options if o.price is not None and _same(o, first, last)]
        if same:
            return _on_route(min(same, key=lambda o: o.price), run)
        # jiná trasa se stejným odjezdem, která nedojede později (ÖBB přes Attnang
        # místo Selzthalu) – jízdenka je na ni, úseky se vezmou od prodejce
        other = [o for o in options if o.price is not None and abs(o.departure - first.departure) <= SAME_DEPARTURE
                 and o.arrival <= last.arrival + dt.timedelta(minutes=1)]
        if other:
            chosen = min(other, key=lambda o: o.price)
            return Journey(provider=chosen.provider, legs=list(chosen.legs), fares=list(chosen.fares),
                           fare_status=chosen.fare_status, deep_link=chosen.deep_link,
                           notes=list(chosen.notes), raw={**chosen.raw, "other_route": True})
        return None


def sellers_for(origin: str, destination: str, run: list[Leg]) -> tuple[list[str], list[str]]:
    """Kdo může prodat jízdenku na kus ``origin`` → ``destination``:
    (dopravci bez prohlížeče, dopravci přes prohlížeč)."""
    countries = {c for c in (geo.country_of(origin), geo.country_of(destination)) if c}
    trains_cd = all("České dráhy" in leg.carrier or leg.carrier in ("ČD", "CD") for leg in run)
    if countries <= {"CZ"}:
        return list(SELLERS), (list(BROWSER_SELLERS) if trains_cd else [])
    plain = [name for name, cls in PROVIDERS.items()
             if name not in NOT_SELLERS and getattr(cls, "provides_prices", True)
             and not getattr(cls, "needs_browser", False)
             and countries <= set(getattr(cls, "countries", ()))]
    browser = [name for name, allowed in BROWSER_COUNTRIES.items() if countries <= allowed
               and (name != "ČD" or "CZ" in countries) and (name != "ZSSK" or "SK" in countries)]
    return plain, browser


def _match(options: Sequence[Journey], first: Leg, last: Leg, origin: str, destination: str) -> Journey | None:
    """Nejlevnější spoj z hlavního hledání, který jede přesně tento kus."""
    same = [o for o in options if _same(o, first, last)
            and same_place(o.legs[0].origin, origin) and same_place(o.legs[-1].destination, destination)]
    return min(same, key=lambda o: o.price, default=None)


def _on_route(option: Journey, run: list[Leg]) -> Journey:
    """Jízdenka prodejce na kus ``run`` – s úseky z IDOS (zastávky, dopravci úseků)."""
    copy = Journey(provider=option.provider, legs=list(run), fares=list(option.fares),
                   fare_status=option.fare_status, deep_link=option.deep_link, notes=list(option.notes),
                   raw=dict(option.raw))
    copy.raw.pop("other_route", None)
    return copy


def _split_points(run: list[Leg]) -> list[tuple[int, int]]:
    """Kde se smí kus rozdělit: ``(úsek, zastávka)`` – přestupy ``(k, 0)``
    a stanice na hranici státu uvnitř úseku (poslední před hranicí a první
    za ní), kde vlak staví. Seřazeno podle polohy na trase."""
    points: list[tuple[int, int]] = [(k, 0) for k in range(1, len(run))]
    for k, leg in enumerate(run):
        stops = leg.stops
        if len(stops) < 3:
            continue
        countries = [geo.country_of(s.name) for s in stops]
        for i in range(1, len(stops) - 1):
            here, nxt, prev = countries[i], countries[i + 1], countries[i - 1]
            if here and ((nxt and nxt != here) or (prev and prev != here)) and stops[i].departure \
                    and stops[i].arrival:
                points.append((k, i))
    return sorted(set(points))


def _split(run: list[Leg], point: tuple[int, int]) -> tuple[list[Leg], list[Leg]]:
    k, i = point
    if i == 0:
        return run[:k], run[k:]
    leg = run[k]
    head = _slice([leg], [0, 0], [0, i])
    tail = _slice([leg], [0, i], [0, len(leg.stops) - 1])
    if not head or not tail:
        return [], []
    return run[:k] + head, tail + run[k + 1:]


def _same(option: Journey, first: Leg, last: Leg) -> bool:
    return (abs(option.departure - first.departure) <= SAME_DEPARTURE
            and abs(option.arrival - last.arrival) <= SAME_ARRIVAL)


def _plans(journey: Journey) -> list[list[dict]]:
    plans = journey.raw.get("plans")
    if plans:
        return plans
    # starší tvar: celé úseky
    legs = journey.legs
    out: list[dict] = [{"kind": "ids", "from": [a, 0], "to": [b, max(len(legs[b].stops), 2) - 1],
                        "price": price, "system": system, "name": name}
                       for a, b, price, system, name in journey.raw.get("ids_pieces", [])]
    out += [{"kind": "buy", "from": [i, 0], "to": [i, max(len(legs[i].stops), 2) - 1]}
            for i in journey.raw.get("unpriced", [])]
    return [sorted(out, key=lambda seg: seg["from"])]


def _slice(legs: list[Leg], start: list[int], end: list[int]) -> list[Leg]:
    """Úseky spoje od zastávky ``start`` do ``end`` (``[úsek, zastávka]``), krajní oříznuté."""
    (k0, i0), (k1, i1) = start, end
    out = []
    for k in range(k0, k1 + 1):
        leg = legs[k]
        stops = leg.stops or [Stop(leg.origin, departure=leg.departure), Stop(leg.destination, arrival=leg.arrival)]
        a = i0 if k == k0 else 0
        b = i1 if k == k1 else len(stops) - 1
        if not 0 <= a < b < len(stops):
            if a == 0 and b == len(stops) - 1:
                out.append(leg)
                continue
            return []
        if a == 0 and b == len(stops) - 1:
            out.append(leg)
            continue
        dep = stops[a].departure or stops[a].arrival
        arr = stops[b].arrival or stops[b].departure
        if dep is None or arr is None:
            return []
        out.append(Leg(origin=stops[a].name, destination=stops[b].name, departure=dep, arrival=arr,
                       mode=leg.mode, carrier=leg.carrier, line=leg.line, stops=stops[a:b + 1],
                       reservation_required=leg.reservation_required))
    return out


def _full(name: str) -> str:
    """Zkratky IDOS na celé jméno pro dopravce („Veselí n.Lužnicí“ -> „Veselí nad Lužnicí“)."""
    from .. import stations

    place = stations.place(name)
    if place is not None and place.kind != "b" and "," not in name and not place.country:
        return place.name
    return name
