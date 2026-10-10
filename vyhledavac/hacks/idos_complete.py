"""Dopočet ceny spojů z IDOS, kde jízdenka IDS nepokryje celou cestu.

Domanín → Praha: autobus IDESKA do Českých Budějovic (cenu spočítá tarif
IDESKA) a dál rychlík do Prahy – ten IDS celý nepokrývá. Dopravce „IDS“
takový spoj vrátí bez ceny a s několika rozpisy jízdenek v ``raw["plans"]``:

* IDS na celé úseky (autobus), vlak celý u dopravce;
* IDS co nejdál – i vlakem, do poslední zastávky v systému, kde vlak
  staví (Domanín → Tábor IDESKA), zbytek u dopravce (Tábor → Praha), případně
  zase IDS dalšího systému (PID);
* IDS co nejdál a zbytek vlaku jednou jízdenkou.

Tenhle hack se na každý dokupovaný kus zeptá dopravců (Arriva/SJT, Bileto,
RegioJet, FlixBus, Leo Express a – jsou-li zapnuté – ČD a OneTicket přes
prohlížeč) a vezme jízdenku na **tentýž spoj** (odjezd a příjezd do pár
minut). Nabídne nejlevnější rozpis, který jde ocenit celý.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Sequence

from ..models import Fare, FareStatus, Journey, Leg, SearchQuery, Stop
from ..normalize import normalize
from ..registry import HACKS
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
        segments = best[1]
        lines = "; ".join(f"{s.origin} → {s.destination}: {s.provider} {s.price:.0f} Kč" for s in segments)
        merged = combine(" + ".join(dict.fromkeys(s.provider for s in segments)), segments,
                         notes=[f"🧩 Spoj z IDOS s jízdenkami: {lines}."])
        if merged is not None:
            merged.legs = list(journey.legs)          # časy a zastávky z IDOS
            merged.deep_link = journey.deep_link
        return merged

    async def _price_plan(self, query: SearchQuery, journey: Journey, plan: list[dict],
                          search: SearchFn) -> list[Journey] | None:
        legs = journey.legs
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
            seller = await self._seller(query, part, search)
            if seller is None and len(part) > 1:          # zkusit po jednotlivých úsecích
                parts = [await self._seller(query, [leg], search) for leg in part]
                if any(p is None for p in parts):
                    return None
                segments.extend(parts)
            elif seller is None:
                return None
            else:
                segments.append(seller)
        return segments

    async def _seller(self, query: SearchQuery, run: list[Leg], search: SearchFn) -> Journey | None:
        first, last = run[0], run[-1]
        origin, destination = _full(first.origin), _full(last.destination)
        trains_cd = all("České dráhy" in leg.carrier or leg.carrier in ("ČD", "CD") for leg in run)
        asks = [(SELLERS, False)] + ([(BROWSER_SELLERS, True)] if trains_cd else [])
        options: list[Journey] = []
        for only, browser in asks:
            key = (normalize(origin), normalize(destination), browser)
            cached = self._seen.get(key, [])
            if not any(_same(o, first, last) for o in cached):
                fresh = await search(query.leg_query(origin, destination, first.departure - dt.timedelta(minutes=2)),
                                     allow_browser=browser, only=only)
                cached = cached + list(fresh)
                self._seen[key] = cached
            options.extend(cached)
        same = [o for o in options if o.price is not None and o.provider != "IDS" and _same(o, first, last)]
        return min(same, key=lambda o: o.price, default=None)


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
