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

import asyncio
import contextvars
import dataclasses
import datetime as dt
import logging
import re
import time
from decimal import Decimal
from typing import Sequence

from .. import geo, transfer
from ..models import Fare, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import matches_station, normalize, same_place
from ..registry import HACKS, PROVIDERS
from ..trace import nested, set_tag, trace
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
#: Kolik různých míst lomu se ocení, než se vybere nejlevnější.
MAX_SPLIT_OPTIONS = 2
#: Prodejci přes prohlížeč: ČD prodá cestu z Česka (a do Česka) do celé Evropy
#: – často levněji díky Včasné jízdence Evropa; ZSSK po Slovensku a ze Slovenska.
BROWSER_FROM = {"ČD": "CZ", "ZSSK": "SK"}
#: … a ČD i na vlak, který Českem jen projíždí.
TRANSIT_SELLERS = {"ČD"}
#: Rezerva na přestup ve stejné stanici; jinde ve městě skutečný čas (``transfer``).
TRANSFER_SAME = transfer.SAME
#: Navazující varianty zbytku cesty u jiných prodejců (FlixBus × ČD …).
MAX_VARIANTS = 2
VARIANT_WAIT = dt.timedelta(hours=3)
#: Zdroje, které jízdenky neprodávají (jen jízdní řád / tarif IDS).
NOT_SELLERS = {"IDS", "Transitous", "Plánovač"}
#: Zdroj spoje, který se právě oceňuje (souběžně běží víc spojů).
_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar("idos_complete_source", default="IDS")
#: Kdo dodává spoje bez ceny s rozpisem jízdenek (``raw["plans"]``).
ROUTE_SOURCES = {"IDS", "Plánovač"}
#: Nejvýš tolik dotazů na prodejce na jeden spoj (dlouhá cesta přes 5 zemí).
QUERY_BUDGET = 40
#: … z toho přes prohlížeč (ČD, ZSSK – každý dotaz desítky sekund a jdou po jednom).
BROWSER_BUDGET = 6
#: … a za celé hledání (dotazy přes prohlížeč jdou po jednom – 10 × ~25 s).
BROWSER_TOTAL = 10


@HACKS.register()
class IdosCompleteHack(TravelHack):
    name = "idos_complete"
    label = "Spoje z IDOS a plánovače: úseky ocení dopravci"
    default_min_saving_pct = 0.0
    _browser_left = BROWSER_TOTAL

    async def find(self, query: SearchQuery, journeys: Sequence[Journey],
                   search: SearchFn) -> list[Journey]:
        todo = [j for j in journeys if j.provider in ROUTE_SOURCES and (j.raw.get("plans") or j.raw.get("unpriced"))]
        # tentýž spoj z IDOS i z plánovače se oceňuje jen jednou (IDOS má přednost)
        from_idos = {(j.departure, j.arrival) for j in todo if j.provider == "IDS"}
        todo = [j for j in todo if j.provider == "IDS" or (j.departure, j.arrival) not in from_idos]
        # nejdřív spoje bez ceny, pak ty, kde jde jen o možnou úsporu; IDOS před plánovačem
        todo.sort(key=lambda j: (j.price is not None, j.provider != "IDS"))
        todo = todo[:MAX_JOURNEYS]
        # Výsledky dotazů podle (odkud, kam): dotaz z 8:00 Budějovice → Praha
        # vrátí i vlak v 10:10, další spoj se tedy už ptát nemusí.
        self._seen: dict[tuple[str, str, bool], list[Journey]] = {}
        # spoje z hlavního hledání: celá cesta jedním vlakem už může mít cenu
        self._base = [j for j in journeys if j.provider not in NOT_SELLERS and j.price is not None]
        async def one(n: int, journey: Journey) -> list[Journey]:
            set_tag(f"#{n}")             # spoje běží souběžně – v trasování je rozliší číslo
            try:
                return await self._complete(query, journey, search)
            except Exception:
                log.exception("idos_complete: dopočet selhal")
                return []

        # spoje souběžně; dotazy přes prohlížeč (ČD, ZSSK) ale po jednom –
        # souběžné stránky prohlížeče nestíhaly časový limit
        self._browser_lock = asyncio.Lock()
        self._browser_left = BROWSER_TOTAL
        found: list[Journey] = []
        for part in await asyncio.gather(*(one(n, j) for n, j in enumerate(todo, 1))):
            found.extend(part)
        return found

    async def _complete(self, query: SearchQuery, journey: Journey,
                        search: SearchFn) -> list[Journey]:
        rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
        plans = _plans(journey)
        _SOURCE.set(journey.provider)
        trace(f"▶ spoj z {journey.provider} {journey.departure:%H:%M}–{journey.arrival:%H:%M} "
              f"{rides[0].origin} → {rides[-1].destination}: "
              + " ➔ ".join(f"{leg.line} [{leg.carrier or '?'}]" for leg in rides)
              + f"; {len(plans)} rozpis(y) jízdenek")
        budget = [QUERY_BUDGET, BROWSER_BUDGET]

        async def limited(q: SearchQuery, **kw):
            if budget[0] <= 0:
                trace("⚠ vyčerpán limit dotazů na prodejce – dál jen z už známých odpovědí")
                return []
            if kw.get("allow_browser"):
                if budget[1] <= 0 or self._browser_left <= 0:
                    trace("⚠ vyčerpán limit dotazů přes prohlížeč (ČD, ZSSK) – ptám se jen ostatních")
                    return []
                budget[1] -= 1
                self._browser_left -= 1
            budget[0] -= 1
            return await search(q, **kw)

        best = None
        with nested():
            for n, plan in enumerate(plans, 1):
                trace(f"rozpis {n}: " + " | ".join(_plan_text(rides, seg) for seg in plan))
                with nested():
                    segments = await self._price_plan(query, journey, plan, limited)
                if segments is None:
                    trace(f"✗ rozpis {n} nejde ocenit celý")
                    continue
                total = sum((s.price for s in segments), Decimal(0))
                trace(f"✓ rozpis {n}: {total:.0f} Kč = " + " + ".join(
                    f"{s.provider} {s.origin} → {s.destination} {s.price:.0f}" for s in segments))
                if best is None or total < best[0]:
                    best = (total, segments)
        if best is None and journey.provider != "IDS":
            # aspoň známé kusy – ať je vidět, který úsek chybí (z už známých odpovědí, bez dotazů)
            partial = await self._partial(query, rides, plans[0], search)
            if partial is not None:
                partial.deep_link = journey.deep_link
                return [partial]
        if best is None or (journey.price is not None and best[0] >= journey.price):
            trace("✗ spoj zůstane bez ceny" if best is None else "= tarif IDS sám vyjde levněji")
            return []                        # IDS samo vyjde levněji
        out = []
        if not any(o.price <= best[0] for o in self._base if _same(o, rides[0], rides[-1])):
            # (jinak tentýž spoj už má z hledání stejnou nebo nižší cenu)
            merged = _merge(best[1], f"🧩 Spoj z {'IDOS' if journey.provider == 'IDS' else 'plánovače'} s jízdenkami")
            if merged is not None:
                if not any(s.raw.get("other_route") for s in best[1]):
                    merged.legs = list(journey.legs)      # časy, zastávky a pěší přesuny z IDOS
                else:
                    merged.legs = _join_split_trains(merged.legs)
                merged.deep_link = journey.deep_link
                out.append(merged)
        out += await self._variants(query, best[1])
        return out

    async def _partial(self, query: SearchQuery, rides: list[Leg], plan: list[dict],
                       search: SearchFn) -> Journey | None:
        """Spoj, který celý ocenit nejde: kusy s cenou z už známých odpovědí
        (nejdelší možné) a kusy bez ceny – výsledek ukáže, co chybí."""
        async def no_queries(q, **kw):
            return []

        pieces: list[Journey] = []
        for seg in plan:
            part = _slice(rides, seg["from"], seg["to"])
            if seg["kind"] in ("local", "ids", "offer"):
                price = Decimal(seg.get("price") or 0)
                pieces.append(Journey(provider=seg.get("system") or "MHD", legs=part, fare_status=FareStatus.PRICED,
                                      fares=[Fare(amount=price, currency="CZK", amount_czk=price,
                                                  label=seg.get("system") or "MHD")]))
                continue
            i = 0
            while i < len(part):
                for j in range(len(part), i, -1):
                    with nested():
                        found = await self._seller(query, part[i:j], no_queries)
                    if found is not None:
                        pieces.append(found)
                        i = j
                        break
                else:
                    pieces.append(Journey(provider="?", legs=[part[i]], fare_status=FareStatus.UNKNOWN))
                    i += 1
        if not any(p.price is not None and p.provider != "MHD" for p in pieces):
            return None                      # bez jediné skutečné jízdenky nemá smysl ukazovat
        missing = [p for p in pieces if p.price is None]
        trace("◐ jen část ceny – chybí: " + ", ".join(f"{p.origin} → {p.destination} ({p.legs[0].line} "
                                                      f"[{p.legs[0].carrier}])" for p in missing))
        merged = _merge(pieces, "🧩 Spoj – cenu zná jen část jízdenek")
        if merged is not None:
            # neúplná cena nesmí vypadat jako nejlevnější spoj – jen v poznámce
            known = sum((p.price for p in pieces if p.price is not None), Decimal(0))
            merged.fares = []
            merged.fare_status = FareStatus.UNKNOWN
            merged.notes.append(f"Známé jízdenky dohromady {known:.0f} Kč (bez chybějících úseků).")
            merged.notes.append("Chybí cena úseků: " + ", ".join(
                f"{p.origin} → {p.destination} ({p.legs[0].line}, {p.legs[0].carrier or 'dopravce neznámý'})"
                for p in missing) + " – jízdenku kupte u dopravce.")
        return merged

    def _browser_unpriced(self, ends: tuple[str | None, str | None]) -> bool:
        """Vrátil už prohlížeč (ČD) na tuhle kombinaci zemí spoje bez ceny?"""
        known = getattr(self, "_unpriced", set())
        start, end = ends
        return (start, end) in known or start in known or end in known

    def _learn_unpriced(self, ends: tuple[str | None, str | None]) -> None:
        """ČD ukázalo spoje, ale bez ceny (Malmö → Břeclav: ze Švédska ČD neprodává):
        dál se na tu zemi přes prohlížeč neptáme – každý dotaz stojí ~20 s."""
        start, end = ends
        if start == end == "CZ" or None in ends:
            return                           # po Česku ČD ceny dává – výpadek, ne pravidlo
        known = self.__dict__.setdefault("_unpriced", set())
        known.add((start, end))
        if end == "CZ" and start and start != "CZ":
            known.add(start)                 # ani do Česka – z té země ČD neprodává
        if start == "CZ" and end and end != "CZ":
            known.add(end)
        trace(f"ℹ prohlížeč na {start or '?'} → {end or '?'} cenu nedává – příště se neptám")

    async def _variants(self, query: SearchQuery, segments: list[Journey]) -> list[Journey]:
        """Jiné navazující spoje na zbytek cesty za jízdenkami IDS (Trhové Sviny →
        Salzburg: IDS do Budějovic, dál FlixBus podle IDOS – nebo vlak ČD).

        Bere se z odpovědí prodejců, které už přišly (žádné dotazy navíc):
        spoj z téže stanice/města do cíle, který odjíždí s rezervou na přestup
        nejvýš 3 h po příjezdu jízdenky IDS. Nabídne nejlevnější a nejdřív
        přijíždějící, které se liší od hlavní varianty."""
        start = 0
        for k, seg in enumerate(segments):
            if seg.provider in SYSTEMS_IDS:
                start = k + 1
        if start >= len(segments):
            return []
        head, tail = segments[:start], segments[start:]
        ready = head[-1].arrival if head else query.departure
        where = head[-1].destination if head else query.origin
        origin, destination = tail[0].origin, tail[-1].destination
        names = {normalize(_query_name(origin, True)), normalize(_full(origin))}
        pool: list[Journey] = []
        for (o, d, _), options in self._seen.items():
            if o in names and normalize(d) in {normalize(_query_name(destination, True)),
                                               normalize(_full(destination))}:
                pool.extend(options)
        chosen = (tail[0].departure, tail[-1].arrival)
        fits = []
        for option in pool:
            if option.price is None or option.provider in NOT_SELLERS or not option.legs:
                continue
            if not (_same_city(option.legs[0].origin, origin, where)
                    and _same_city(option.legs[-1].destination, destination, query.destination)):
                continue
            if not ready <= option.departure <= ready + VARIANT_WAIT:
                continue
            gap = await transfer.transfer_time(where, option.legs[0].origin, ready)
            if option.departure < ready + gap:
                continue
            if (option.departure, option.arrival) == chosen:
                continue
            fits.append(option)
        picks: list[Journey] = []
        for key in (lambda o: (o.price, o.arrival), lambda o: (o.arrival, o.price)):
            for option in sorted(fits, key=key):
                if all((option.departure, option.arrival, option.provider) !=
                       (p.departure, p.arrival, p.provider) for p in picks):
                    picks.append(option)
                    break
        variants = []
        for option in picks[:MAX_VARIANTS]:
            trace(f"↪ navazující varianta: {option.provider} {option.departure:%H:%M}–{option.arrival:%H:%M} "
                  f"{option.price:.0f} Kč")
            merged = _merge([*head, option], "🔀 Navazující varianta: jízdenky")
            if merged is not None:
                variants.append(merged)
        return variants

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
            if seg["kind"] == "local":            # MHD uprostřed cesty – jízdenka na místě
                segments.append(Journey(provider="MHD", legs=part, fare_status=FareStatus.PRICED,
                                        fares=[Fare(amount=Decimal(0), currency="CZK", amount_czk=Decimal(0),
                                                    label="MHD", tariff="jízdenka MHD na místě – cena "
                                                                        "není započtena")]))
                continue
            if seg["kind"] in ("ids", "offer"):      # tarif IDS / jízdenka z e-shopu IDOS
                price = Decimal(seg["price"])
                segments.append(Journey(
                    provider=seg["system"], legs=part, fare_status=FareStatus.PRICED,
                    fares=[Fare(amount=price, currency="CZK", amount_czk=price, label=seg["system"],
                                tariff=seg["name"])]))
                continue
            bought = await self._price_run(query, part, search, seg.get("splits", MAX_SPLITS))
            if bought is None:
                return None
            segments.extend(bought)
        return segments

    async def _price_run(self, query: SearchQuery, run: list[Leg], search: SearchFn,
                         splits: int, ready: dt.datetime | None = None,
                         ready_at: str = "") -> list[Journey] | None:
        """Jízdenky na souvislý kus cesty: celý u jednoho prodejce, jinak
        co nejdelší začátek + zbytek (rekurzivně) – lom v přestupní stanici
        nebo ve stanici na hranici státu, kde vlak staví."""
        trace(f"kus {run[0].origin} → {run[-1].destination} ({len(run)} úsek/ů, "
              f"{', '.join(sorted(_run_countries(run))) or '?'})")
        with nested():
            return await self._price_run_inner(query, run, search, splits, ready, ready_at)

    async def _price_run_inner(self, query: SearchQuery, run: list[Leg], search: SearchFn,
                               splits: int, ready: dt.datetime | None, ready_at: str) -> list[Journey] | None:
        whole = await self._seller(query, run, search, ready, ready_at=ready_at)
        countries = _run_countries(run)
        abroad = not countries <= {"CZ"}
        # po Česku stačí celá jízdenka; do ciziny se porovná i s rozdělením –
        # ČD co nejdál (Včasná jízdenka Evropa), zbytek místní dopravce
        if whole is not None and (splits <= 0 or not abroad or len(run) == 1 and len(run[0].stops) < 3):
            return [whole]
        if splits <= 0:
            return None
        split = await self._split_run(query, run, search, splits, ready, ready_at)
        options = [o for o in ([whole] if whole else []) + ([split] if split else [])]
        if not options:
            trace("✗ kus nejde ocenit – ani celý, ani s lomem")
            return None
        if len(options) == 1:
            return options[0] if isinstance(options[0], list) else [options[0]]
        split_price = sum((s.price for s in split), Decimal(0))
        trace(f"⚖ celá jízdenka {whole.provider} {whole.price:.0f} Kč × rozdělená {split_price:.0f} Kč "
              f"→ {'rozdělená' if split_price < whole.price else 'celá'}")
        return split if split_price < whole.price else [whole]

    async def _split_run(self, query: SearchQuery, run: list[Leg], search: SearchFn,
                         splits: int, ready: dt.datetime | None = None,
                         ready_at: str = "") -> list[Journey] | None:
        points = _split_points(run)
        if len(run) > MAX_RUN_LEGS:          # dlouhý kus: jen po celých úsecích
            points = [p for p in points if p[1] == 0]
        found: list[list[Journey]] = []
        if points:
            trace("možné lomy: " + ", ".join(_point_name(run, p) for p in reversed(points)))
        ends = {normalize(_query_name(run[0].origin, True)), normalize(_query_name(run[-1].destination, True))}
        for point in reversed(points):        # nejdelší začátek první (ČD co nejdál)
            head, tail = _split(run, point)
            if not head or not tail:
                continue
            if normalize(_query_name(head[-1].destination, True)) in ends:
                continue                      # lom ve stejném městě (Malmö → Malmö Hyllie) nic nepřinese
            trace(f"✂ lom v {_point_name(run, point)}: {head[0].origin} → {head[-1].destination} + "
                  f"{tail[0].origin} → {tail[-1].destination}")
            with nested():
                first = await self._seller(query, head, search, ready, ready_at=ready_at,
                                           deadline=tail[0].departure, next_at=tail[0].origin)
            if first is None:
                continue
            # zbytek navazuje na skutečný příjezd jízdenky začátku (může být jiný spoj)
            rest = await self._price_run(query, tail, search, splits - 1, first.arrival,
                                         first.legs[-1].destination if first.legs else "")
            if rest is not None:
                found.append([first, *rest])
                if len(found) >= MAX_SPLIT_OPTIONS:
                    break
        # nejlevnější z několika lomů: ČD do Villachu + ÖBB bývá levnější než ČD do Jesenic + ÖBB
        return min(found, key=lambda parts: sum((p.price for p in parts), Decimal(0)), default=None)

    async def _seller(self, query: SearchQuery, run: list[Leg], search: SearchFn,
                      ready: dt.datetime | None = None, ready_at: str = "",
                      deadline: dt.datetime | None = None, next_at: str = "") -> Journey | None:
        """Nejlevnější jízdenka na kus ``run``: přesně tentýž spoj, jinak spoj se
        stejným odjezdem, který nedojede později; za lomem (``ready`` = příjezd
        předchozí jízdenky do ``ready_at``) i nejbližší navazující spoj – WESTbahn
        Linz → Salzburg nikdo z prodejců neprodá, ÖBB o pár minut později ano.

        Přestupy mezi jízdenkami počítá se skutečným časem (``transfer``):
        z Berlin Südkreuz na Hauptbahnhof je to přes 20 minut, ne 10.
        ``deadline``/``next_at`` = odjezd a stanice navazujícího kusu."""
        first, last = run[0], run[-1]
        # předchozí jízdenka (jiný spoj) může přijet jinam nebo později – pak
        # tentýž spoj nestihnu a hledá se jen navazující
        catchable = ready is None or await _reachable(ready, ready_at, first.origin, first.departure)
        if not catchable:
            trace(f"⇄ z {ready_at} ({ready:%H:%M}) spoj {first.departure:%H:%M} z {first.origin} nestihnu")
        countries = _run_countries(run)
        abroad = not countries <= {"CZ"}
        # do ciziny: jméno města (prodejci neznají „České Budějovice, aut.nádr.“)
        origin, destination = _query_name(first.origin, abroad), _query_name(last.destination, abroad)
        from_base = _match(self._base, first, last, origin, destination)
        if from_base is not None and not catchable:
            from_base = None
        if from_base is not None:
            trace(f"✓ {origin} → {destination}: už z hledání – {from_base.provider} {from_base.price:.0f} Kč")
            return _on_route(from_base, run)
        crossed = _all_countries(run) or countries
        plain, browser = sellers_for(origin, destination, run, crossed)
        if browser and _SOURCE.get() != "IDS" and not any(leg.mode is Mode.TRAIN for leg in run):
            browser = []                     # autobus z plánovače: ČD přes prohlížeč (pomalé) neptat
        if not plain and not browser:
            trace(f"– {origin} → {destination} ({', '.join(sorted(crossed))}): nikdo neprodává tuhle "
                  "kombinaci zemí, neptám se")
            return None
        trace(f"? {origin} → {destination} {first.departure:%H:%M}–{last.arrival:%H:%M} "
              f"({', '.join(sorted(crossed)) or '?'}): ptám se {', '.join(plain) or '–'}"
              + (f" | prohlížeč: {', '.join(browser)}" if browser else ""))
        asks = [(plain, False)] + ([(browser, True)] if browser else [])
        options: list[Journey] = []
        ends = _end_countries(run)
        for only, allow_browser in asks:
            if not only:
                continue
            key = (normalize(origin), normalize(destination), allow_browser)
            cached = self._seen.get(key, [])
            if allow_browser and self._browser_unpriced(ends):
                trace(f"– prohlížeč ({', '.join(only)}): z/do {' → '.join(c or '?' for c in ends)} "
                      "cenu nedává (zjištěno dřív) – neptám se")
                continue
            # znovu se ptá, dokud tentýž spoj nemá v odpovědích cenu (DSB vrací
            # Öresundståg bez ceny, SJ ho prodá – ale odpověděl jen na dřívější čas)
            if not any(_same(o, first, last) and o.price is not None for o in cached):
                since = first.departure - dt.timedelta(minutes=2)
                if ready is not None:
                    since = min(since, ready + TRANSFER_SAME)
                lock = getattr(self, "_browser_lock", None) if allow_browser else None
                started = time.monotonic()
                if lock is not None:
                    async with lock:
                        waited = time.monotonic() - started
                        fresh = await search(query.leg_query(origin, destination, since),
                                             allow_browser=allow_browser, only=only)
                else:
                    waited = 0.0
                    fresh = await search(query.leg_query(origin, destination, since),
                                         allow_browser=allow_browser, only=only)
                with nested():
                    trace(f"⏱ {', '.join(only)}: {len(fresh)} spojů za {time.monotonic() - started - waited:.0f} s"
                          + (f" (čekání na prohlížeč {waited:.0f} s)" if waited >= 1 else ""))
                cached = cached + list(fresh)
                self._seen[key] = cached
                if allow_browser and fresh and all(o.price is None for o in fresh):
                    self._learn_unpriced(ends)
            options.extend(cached)
        # jen spoje opravdu z místa do místa (prodejce může vrátit i jiný cíl)
        start, end = _end_countries(run)
        def place_ok(o: Journey) -> bool:
            """Odkud i kam sedí – u spoje přesně ve stejný čas stačí jeden konec
            (SJ píše Kodaň švédsky „Köpenhamn H“, DB „Praha hl.n.“ německy)."""
            if not (_marked_ok(o.legs[0].origin, start) and _marked_ok(o.legs[-1].destination, end)):
                return False
            a = _same_city(o.legs[0].origin, first.origin, origin)
            b = _same_city(o.legs[-1].destination, last.destination, destination)
            return (a and b) or ((a or b) and _same(o, first, last))

        options = [o for o in options if o.provider not in NOT_SELLERS and o.legs and place_ok(o)]
        same = [o for o in options if o.price is not None and _same(o, first, last)] if catchable else []
        with nested():
            counts: dict[str, list[int]] = {}
            for o in options:
                c = counts.setdefault(o.provider, [0, 0])
                c[0] += 1
                c[1] += o.price is not None
            trace("odpovědi z místa do místa: " + (", ".join(
                f"{name} {n} (s cenou {p})" for name, (n, p) in sorted(counts.items())) or "žádné"))
            if same:
                best = min(same, key=lambda o: o.price)
                trace(f"✓ tentýž spoj: {best.provider} {best.price:.0f} Kč"
                      + (f" (další: {', '.join(f'{o.provider} {o.price:.0f}' for o in same if o is not best)})"
                         if len(same) > 1 else ""))
                return _on_route(best, run)

        async def connects(o: Journey, gap: dt.timedelta = dt.timedelta(0)) -> bool:
            """Stihnu z předchozí jízdenky odjezd ``o`` a z příjezdu ``o`` navazující kus?"""
            if ready is not None and not await _reachable(ready, ready_at, o.legs[0].origin, o.departure, gap):
                return False
            if deadline is None or not next_at:
                return True
            if o.arrival <= last.arrival + dt.timedelta(minutes=1) and transfer.same_station(
                    o.legs[-1].destination, last.destination):
                return True                  # přijede tam i tehdy jako trasa – přestup podle IDOS
            return await _reachable(o.arrival, o.legs[-1].destination, next_at, deadline, TRANSFER_SAME)

        # jiná trasa se stejným odjezdem, která nedojede později (ÖBB přes Attnang
        # místo Selzthalu) – jízdenka je na ni, úseky se vezmou od prodejce;
        # smí přijet i později než trasa (jinam ve městě), pokud ještě stihne
        # navazující kus (``deadline``) se skutečným časem přestupu
        latest = max(deadline or last.arrival, last.arrival) + dt.timedelta(minutes=1)
        other = [o for o in options if o.price is not None and abs(o.departure - first.departure) <= SAME_DEPARTURE
                 and o.arrival <= latest]
        other = [o for o in other if await connects(o)]
        if not other and ready is None and deadline is not None and next_at:
            # začátek cesty: i dřívější spoj (z odpovědí, které už přišly), když
            # se na něj navazující kus stihne – Öresundståg do Kodaně o 2 h dřív
            early = sorted((o for o in options if o.price is not None
                            and query.departure <= o.departure < first.departure - SAME_DEPARTURE),
                           key=lambda o: (-o.departure.timestamp(), o.price))
            for o in early:
                if await connects(o, TRANSFER_SAME):
                    trace(f"↶ dřívější spoj na začátek: {o.provider} {o.departure:%H:%M}–{o.arrival:%H:%M}")
                    other = [o]
                    break
        if not other and ready is not None:
            late = dt.timedelta(minutes=60)
            near = [o for o in options if o.price is not None and ready <= o.departure <= first.departure + late
                    and o.arrival <= last.arrival + late]
            near.sort(key=lambda o: (o.arrival, o.price))
            other = []
            for o in near:
                if await connects(o, TRANSFER_SAME):
                    other = [o]                  # nejdřív přijíždějící, který stihnu (a z něj dál)
                    break
        if other:
            chosen = min(other, key=lambda o: o.price)
            trace(f"✓ jiný spoj ve stejný čas / navazující: {chosen.provider} {chosen.departure:%H:%M}–"
                  f"{chosen.arrival:%H:%M} {chosen.price:.0f} Kč", )
            return Journey(provider=chosen.provider, legs=list(chosen.legs), fares=list(chosen.fares),
                           fare_status=chosen.fare_status, deep_link=chosen.deep_link,
                           notes=list(chosen.notes), raw={**chosen.raw, "other_route": True})
        trace("✗ tentýž spoj nikdo neprodává")
        return None


async def _reachable(ready: dt.datetime, at: str, where: str, departure: dt.datetime,
                     same_gap: dt.timedelta = dt.timedelta(0)) -> bool:
    """Stihnu z příjezdu ``ready`` do stanice ``at`` odjezd ``departure`` ze stanice ``where``?
    Ve stejné stanici stačí ``same_gap`` (u spoje z IDOS přestup podle jízdního
    řádu, u jiného spoje rezerva), jinde ve městě skutečný čas cesty mezi nádražími."""
    if not at or transfer.same_station(at, where):
        return ready + same_gap <= departure
    return ready + await transfer.transfer_time(at, where, ready) <= departure


def _plan_text(rides: list[Leg], seg: dict) -> str:
    (k0, i0), (k1, i1) = seg["from"], seg["to"]
    a = rides[k0].stops[i0].name if rides[k0].stops else rides[k0].origin
    b = rides[k1].stops[i1].name if rides[k1].stops else rides[k1].destination
    if seg["kind"] in ("ids", "offer"):
        return f"{seg['system']} {a} → {b} {Decimal(seg['price']):.0f} Kč"
    return f"{'MHD' if seg['kind'] == 'local' else 'koupit'} {a} → {b}"


def _point_name(run: list[Leg], point: tuple[int, int]) -> str:
    k, i = point
    return run[k].origin if i == 0 else f"{run[k].stops[i].name} (ve vlaku {run[k].line})"


def sellers_for(origin: str, destination: str, run: list[Leg],
                countries: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Kdo může prodat jízdenku na kus ``origin`` → ``destination``:
    (dopravci bez prohlížeče, dopravci přes prohlížeč)."""
    if countries is None:
        countries = {c for c in (geo.country_of(origin), geo.country_of(destination)) if c}
    trains_cd = all("České dráhy" in leg.carrier or leg.carrier in ("ČD", "CD") for leg in run)
    if countries <= {"CZ"}:
        return list(SELLERS), (list(BROWSER_SELLERS) if trains_cd else [])
    buses = all(leg.mode is Mode.BUS for leg in run)
    ends = {c for c in (_end_countries(run) if run else ()) if c} or countries
    plain = [name for name, cls in PROVIDERS.items()
             if name not in NOT_SELLERS and getattr(cls, "provides_prices", True)
             and not getattr(cls, "needs_browser", False)
             and countries <= set(getattr(cls, "countries", ()))
             and (not getattr(cls, "home", ()) or ends & set(cls.home))      # MÁV jen z/do Maďarska
             and (buses or name not in BUS_SELLERS)]
    # vlakem přes prohlížeč (ČD, ZSSK) – i místo autobusu do ciziny, ať jde porovnat;
    # ČD prodá i vlak, který Českem jen projíždí (EC Krakov → Vídeň)
    trains = any(leg.mode is Mode.TRAIN for leg in run) or len(countries) > 1
    transit = _transit_countries(run)
    browser = [name for name, home in BROWSER_FROM.items()
               if trains and (home in countries or name in TRANSIT_SELLERS and home in transit)]
    return plain, browser


def _end_countries(run: list[Leg]) -> tuple[str | None, str | None]:
    """Země začátku a konce kusu – podle sousedních stanic trasy (Jesenice
    u Lublaně je slovinská, ne česká obec)."""
    def at(leg: Leg, end: int) -> str | None:
        names = [s.name for s in leg.stops] or [leg.origin, leg.destination]
        name = leg.origin if end == 0 else leg.destination
        others = names[1:] if end == 0 else names[:-1]
        return _stop_country(name, {geo.country_of(n) for n in others[:3] if n}, leg.mode is Mode.TRAIN)
    return at(run[0], 0), at(run[-1], -1)


def _run_countries(run: list[Leg]) -> set[str]:
    return {c for c in _end_countries(run) if c}


def _all_countries(run: list[Leg]) -> set[str]:
    """Všechny země, kterými kus vede (konce každého úseku) – prodejce musí
    obsloužit všechny: Stockholm → Kodaň → Hamburk potřebuje SE, DK i DE."""
    found = set(_run_countries(run))
    for leg in run:
        ends = _end_countries([leg])
        found.update(c for c in ends if c)
    return found


def _marked_ok(name: str, expected: str | None) -> bool:
    """Prodejce výslovně napsal jinou zemi („Rybnik(PL)“ místo českého Rybníku)?"""
    marked = geo.marked_country(name)
    return not (marked and expected and marked != expected)


def _same_city(name: str, *wanted: str) -> bool:
    """Leží ``name`` (jak ho píše prodejce) ve stejném místě jako některé z ``wanted``?
    „Bad Ischl Bahnhof (Zugang Bahnsteig 1)“ = „Bad Ischl“, „Salzburg Süd“ = „Salzburg South“."""
    from .. import stations

    if any(same_place(name, w) or matches_station(w, name) for w in wanted if w):
        return True
    here = stations.place(name)
    if here is None:
        return False
    city = here.obec or here.name
    for w in wanted:
        there = stations.place(w) if w else None
        if there is not None and (there.obec or there.name) == city and there.code == here.code:
            return True
    return False


def _czech_station(name: str):
    """Česká železniční stanice přesně tohoto jména (IDOS „Rybník“ ≠ polský Rybnik)."""
    from .. import stations

    db = stations.database()
    hits = [p for p in (db.exact.get(normalize(name), []) if db else []) if p.kind == "t" and not p.code]
    return max(hits, key=lambda p: p.score) if hits else None


def _stop_country(name: str, neighbours: set[str], rail: bool) -> str | None:
    """Země stanice na trase: stejnojmenné místo v sousední zemi trasy má
    přednost (Jesenice u Lublaně), jinak česká železniční stanice (Rybník
    u Dolního Dvořiště, ne polský Rybnik), jinak nejdůležitější místo."""
    found = geo.country_near(name, neighbours)
    if found in neighbours:
        return found
    if rail and _czech_station(name) is not None:
        return "CZ"
    return found


def _city_inside(name: str):
    """Město schované uvnitř jména stanice: „S+U Berlin Hauptbahnhof“ → Berlín,
    „Malmö central train station FlixBus stop“ → Malmö. Delší shoda vyhrává,
    při stejné délce větší město."""
    from .. import stations

    db = stations.database()
    if db is None:
        return None
    words = normalize(name).split()
    best = None
    for i in range(len(words)):
        for j in range(len(words), i, -1):
            hits = [p for p in db.exact.get(" ".join(words[i:j]), []) if p.kind == "c"]
            if hits:
                place = max(hits, key=lambda p: p.score)
                if best is None or (j - i, place.score) > best[0]:
                    best = ((j - i, place.score), place)
                break
    return best[1] if best else None


def _query_name(name: str, abroad: bool) -> str:
    """Jméno pro dotaz u prodejců. Do ciziny se ptáme na město: zastávka
    „České Budějovice, aut.nádr.“ → „České Budějovice“, „Salzburg South“ →
    Salcburk (zahraniční dopravci dostanou místní jméno „Salzburg“)."""
    if not abroad:
        return _full(name)
    from .. import stations

    station = _czech_station(name)
    if station is not None:
        return station.name
    place = stations.place(name) or _city_inside(name)
    if place is None:
        return name.split(",")[0].strip()
    if place.country:
        return place.name
    if place.kind == "b" or "," in name:
        return place.obec or name.split(",")[0].strip()
    return place.name


#: Prodávají jen vlastní autobusy – na vlak se jich ptát nemá smysl.
BUS_SELLERS = {"FlixBus"}

SYSTEMS_IDS = {"PID", "IDESKA", "IDS JMK", "IDOL", "IREDO", "DÚK", "IDPK", "VDV", "IDZK", "IDSOK", "ODIS"}


def _merge(segments: Sequence[Journey], title: str) -> Journey | None:
    lines = "; ".join(f"{s.origin} → {s.destination}: {s.provider} "
                      + (f"{s.price:.0f} Kč" if s.price is not None else "cena neznámá") for s in segments)
    return combine(" + ".join(dict.fromkeys(s.provider for s in segments)), segments,
                   notes=[f"{title}: {lines}."])


_TRAIN_NO = re.compile(r"\b(\d{2,6})\b")


def _join_split_trains(legs: list[Leg]) -> list[Leg]:
    """Vlak rozdělený lomem jízdenky (ČD do Břeclavi + ÖBB z Břeclavi, tentýž
    rj 177) je pro cestujícího jeden úsek, ne přestup."""
    out: list[Leg] = []
    for leg in legs:
        prev = out[-1] if out else None
        if prev is not None and prev.mode is leg.mode is Mode.TRAIN:
            a, b = _TRAIN_NO.search(prev.line or ""), _TRAIN_NO.search(leg.line or "")
            if a and b and a.group(1) == b.group(1) and transfer.same_station(prev.destination, leg.origin) \
                    and dt.timedelta(0) <= leg.departure - prev.arrival <= dt.timedelta(minutes=20):
                out[-1] = dataclasses.replace(prev, destination=leg.destination, arrival=leg.arrival,
                                              stops=list(prev.stops) + list(leg.stops[1:]))
                continue
        out.append(leg)
    return out


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
        if len(stops) < 3 or leg.mode is not Mode.TRAIN:
            continue                         # autobus (FlixBus) se prodává celý, lom jen ve vlaku
        countries = _stop_countries(leg)
        for i in range(1, len(stops) - 1):
            here = countries[i]
            prev = next((c for c in reversed(countries[:i]) if c), None)      # nejbližší známá
            nxt = next((c for c in countries[i + 1:] if c), None)
            if here and ((nxt and nxt != here) or (prev and prev != here)) and stops[i].departure \
                    and stops[i].arrival:
                points.append((k, i))
    return sorted(set(points))


def _stop_countries(leg: Leg) -> list[str | None]:
    """Země každé zastávky úseku (podle sousedních zastávek, bez „ostrůvků“)."""
    stops = leg.stops
    raw = [geo.country_of(s.name) for s in stops]
    countries = [_stop_country(s.name, {c for j, c in enumerate(raw[max(0, i - 2):i + 3], max(0, i - 2))
                                        if c and j != i}, leg.mode is Mode.TRAIN)
                 for i, s in enumerate(stops)]
    return _without_islands(countries)


def _transit_countries(run: list[Leg]) -> set[str]:
    """Země, kterými vlaky kusu projíždějí (EC Krakov → Vídeň vede přes Česko)."""
    found: set[str] = set()
    for leg in run:
        if leg.mode is Mode.TRAIN and len(leg.stops) >= 3:
            found.update(c for c in _stop_countries(leg) if c)
    return found


#: Nejvýš tolik jmenovců za sebou („Blanca“, „Sava“) se považuje za ostrůvek.
MAX_ISLAND = 3


def _without_islands(countries: list[str | None]) -> list[str | None]:
    """Vlak hranici přejede jednou: osamělá „cizí“ zastávka mezi dvěma stejnými
    zeměmi je jmenovkyně odjinud (slovinská „Sava“ není v Itálii, „Blanca“
    ve Španělsku) – její zemi neznáme."""
    out = list(countries)
    known = [i for i, c in enumerate(countries) if c]
    for a in range(len(known)):
        for b in range(a + 1, min(a + 1 + MAX_ISLAND, len(known) - 1)):
            # zastávky known[a+1 … b] mezi dvěma zastávkami téže země
            prev, nxt = countries[known[a]], countries[known[b + 1]] if b + 1 < len(known) else None
            inside = {countries[known[j]] for j in range(a + 1, b + 1)}
            if prev == nxt and prev not in inside:
                for j in range(a + 1, b + 1):
                    out[known[j]] = None
    return out


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
