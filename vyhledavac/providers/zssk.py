"""ZSSK – Železničná spoločnosť Slovensko.

Prodejní web ``predaj.zssk.sk`` je JSF aplikace bez veřejného API, takže
tady prohlížeč zůstává. Selektory z původního kódu (`#fromInput`,
`#toInput`, `#departDate`, `#actionSearchConnectionButton`) jsou ověřeně
**stále platné** – na rozdíl od ČD se web nezměnil.

ZSSK v seznamu spojů ceny neukazuje a JSON API nemá. Cenu zná až nákupní
formulář, takže se pro každý spoj prokliká *Kúpiť lístok → Voľba
cestujúcich → Voľba cestovného lístka* a přečte se konečná cena. Aby se
na to nečekalo:

* jízdní řád se pošle hned (``Provider.publish``) a ceny se doplňují
  průběžně, jak se je daří zjistit – na poslední spoj se nečeká,
* trychtýř běží souběžně v ``price_workers`` (výchozí 3) oddělených
  kontextech prohlížeče; ceny se mezi spoji nesdílí ani necachují
  (dynamické ceny, zahraniční lístky),
* žádné pevné prodlevy: čeká se na odpověď našeptávače či JSF požadavku,
  výchozí cestující (dospělý bez slevy) se nepřeklikává a na seznam se
  vrací otevřením ``/search`` (ZSSK si hledání pamatuje, ~1 s),
* trychtýř pokrývá prvních ``price_limit`` spojů (výchozí 3 od zadaného odjezdu) v časovém
  rozpočtu ``price_budget`` (výchozí 110 s); selhání u jednoho spoje
  ostatní neshodí.

Vypnout trychtýř (jen jízdní řád, rychle): ``options={"deep_prices": False}``.

Slovenská 100% sleva (bezplatná přeprava studentů a seniorů) se zapíná
kartou ``DiscountCard.SK_FREE``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Any

from ..errors import ProviderError, StationNotFoundError, TransientProviderError
from ..models import (
    DEFAULT_TZ,
    DiscountCard,
    FareStatus,
    Journey,
    Leg,
    Mode,
    Passenger,
    SearchQuery,
    Stop,
)
from ..registry import PROVIDERS
from .base import BrowserProvider, PRICE_SCOPE_COUNT, price_scope

log = logging.getLogger(__name__)

SEARCH_URL = "https://predaj.zssk.sk/search"

COOKIE_BUTTONS = [
    "button:has-text('Súhlasím')",
    "button:has-text('Prijať')",
    "button:has-text('Rozumiem')",
    "#cookieAccept",
]

CONNECTION_GROUP = ".connection-group"

# Ceny přímo v seznamu – web je občas ukazuje, občas ne.
LIST_PRICE_SELECTORS = [
    ".connection-price",
    ".price",
    "[data-cy='price']",
    "span.amount",
]

_PRICE_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)\s*(?:€|EUR)", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


@PROVIDERS.register()
class ZsskProvider(BrowserProvider):
    name = "ZSSK"
    label = "ZSSK – slovenské vlaky (vyžaduje prohlížeč)"
    currency = "EUR"
    provides_prices = True
    provides_stops = False
    countries = ("SK",)
    international_only = True
    #: Hledání ~10 s + ceny přes trychtýř (rozpočet ``price_budget``).
    #: Co do limitu nestihne, zůstane – engine drží poslední průběžný stav.
    min_timeout = 150.0

    async def search(self, query: SearchQuery) -> list[Journey]:
        session = await self.browser()
        page = await session.new_page()
        try:
            await self._open_list(session, page, query)
            # cena se zjišťuje průchodem nákupu spoj po spoji – jen první spoje
            journeys = price_scope(await self._parse_list(page, query), query,
                                   int(self.options.get("price_limit", PRICE_SCOPE_COUNT)))

            # Ceny jsou hlavní smysl celého vyhledávače, takže trychtýř je
            # zapnutý ve výchozím stavu. Vypnout: options={"deep_prices": False}.
            if self.options.get("deep_prices", True) and journeys:
                # Jízdní řád hned – ceny se doplní, jak budou přicházet.
                self.publish(journeys)
                await self._fill_missing_prices(session, page, journeys, query)
            return journeys
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _open_list(self, session, page, query: SearchQuery) -> None:
        """Otevře web, vyplní formulář a počká na seznam spojů."""
        await session.goto(page, SEARCH_URL)
        await session.accept_cookies(page, COOKIE_BUTTONS)
        await self._fill_form(session, page, query)
        await self._submit(page)

    # ---- formulář ------------------------------------------------------------
    async def _fill_form(self, session, page, query: SearchQuery) -> None:
        origin = page.locator("#fromInput").first
        if not await origin.count():
            raise ProviderError(
                self.name,
                "na predaj.zssk.sk nebylo nalezeno pole '#fromInput' – "
                "web se změnil, aktualizujte selektory v providers/zssk.py",
            )
        await self._pick_station(page, origin, query.origin)
        destination = page.locator("#toInput").first
        await self._pick_station(page, destination, query.destination)

        # ZSSK čeká slovenský formát data s mezerami: "1. 10. 2026".
        await self._set_value(page, "#departDate", _sk_date(query.departure))
        await self._set_value(page, "#departTime", query.departure.strftime("%H:%M"))

    async def _pick_station(self, page, field, name: str) -> None:
        """Napíše stanici a vybere z našeptávače návrh, který jí opravdu odpovídá.

        Našeptávač se plní z XHR ``/autocompleteStations/<text>``. Dokud je
        seznam rozbalený, překrývá pole "Kam". Proto: napsat → počkat na
        odpověď XHR pro *tenhle* text (ne na starý seznam z předchozího
        pole) → kliknout na návrh → seznam zmizí. Psaní po znacích (60 ms
        na znak) nahradilo ``fill`` + poslední znak.

        První návrh se dřív bral naslepo – na "Vídeň" ale ZSSK jako první
        nabídne slovenský **Vígľaš** a hledalo se do něj. Návrh se proto
        porovná se zadaným jménem; když nesedí, zkusí se slovenský či
        německý tvar (Viedeň, Wien). Když nesedí nic, ZSSK tam nejezdí.
        """
        from ..normalize import aliases, best_match

        # Každé pole má vlastní (skrytý) seznam – pracujeme jen s viditelným.
        visible = page.locator("a.dropdown-item[role='option']:visible")
        for term in [name, *aliases(name)]:
            await field.click(force=True)
            await field.fill(term[:-1])
            try:
                async with page.expect_response(
                    lambda r: "autocompleteStations" in r.url, timeout=6000
                ) as info:
                    await field.press_sequentially(term[-1:], delay=30)
                texts = [str(t).strip() for t in (await (await info.value).json() or [])]
            except Exception:
                log.debug("%s: našeptávač pro %r neodpověděl", self.name, term)
                continue
            index = best_match(name, texts)
            if index is None:
                log.debug("%s: na %r nabízí %s – nic nesedí", self.name, term, texts[:5])
                continue
            option = visible.filter(has_text=re.compile(rf"^\s*{re.escape(texts[index])}\s*$"))
            try:
                await option.first.click(timeout=3000)
            except Exception:
                # Položka mimo zobrazenou část seznamu – napíšeme ji celou.
                await field.fill(texts[index])
                await field.press("Enter")
            try:
                await visible.first.wait_for(state="hidden", timeout=2000)
            except Exception:
                await field.press("Tab")
            return
        raise StationNotFoundError(self.name, f"ZSSK nezná stanici {name!r}")

    async def _set_value(self, page, selector: str, value: str) -> None:
        locator = page.locator(selector).first
        if not await locator.count():
            return
        await locator.evaluate(
            """(node, value) => {
                node.value = value;
                node.dispatchEvent(new Event('input', { bubbles: true }));
                node.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )

    async def _submit(self, page) -> None:
        button = page.locator("#actionSearchConnectionButton").first
        if not await button.count():
            raise ProviderError(self.name, "tlačítko vyhledávání nenalezeno")
        await button.click()
        try:
            await page.locator(CONNECTION_GROUP).first.wait_for(state="visible", timeout=30000)
        except Exception as exc:
            raise TransientProviderError(
                self.name, f"ZSSK nevrátilo žádné spojení ({exc})"
            ) from exc
        await page.wait_for_timeout(300)

    # ---- seznam spojů --------------------------------------------------------
    async def _parse_list(self, page, query: SearchQuery) -> list[Journey]:
        groups = page.locator(CONNECTION_GROUP)
        count = min(await groups.count(), int(self.options.get("max_connections", 12)))
        journeys: list[Journey] = []

        for index in range(count):
            group = groups.nth(index)
            try:
                text = await group.inner_text()
            except Exception:
                continue

            legs = _legs_from_text(text, query)
            if not legs:
                times = _TIME_RE.findall(text)
                if len(times) < 2:
                    continue
                departure = _day_after_query(_combine(query.departure, times[0]), query)
                arrival = _combine(departure, times[-1])
                if arrival < departure:
                    arrival += dt.timedelta(days=1)
                train = await _first_text(group, [".trainNumber span", ".trainNumber", ".train-name"])
                legs = [Leg(
                    origin=query.origin, destination=query.destination,
                    departure=departure, arrival=arrival, mode=Mode.TRAIN, carrier="ZSSK",
                    line=train or "ZSSK vlak",
                    stops=[Stop(name=query.origin, departure=departure),
                           Stop(name=query.destination, arrival=arrival)],
                )]

            fares = []
            status = FareStatus.UNKNOWN
            amount = _parse_eur(text)
            if amount is not None:
                fares.append(self.make_fare(amount, "EUR", label="ZSSK",
                                            tariff=_tariff_label(query.lead)))
                status = FareStatus.PRICED

            journeys.append(
                Journey(
                    provider=self.name,
                    legs=legs,
                    fares=fares,
                    fare_status=status,
                    journey_id=str(index),
                    deep_link=SEARCH_URL,
                )
            )
        return journeys

    # ---- ceny přes nákupní trychtýř -------------------------------------------
    async def _fill_missing_prices(self, session, page, journeys, query) -> None:
        """Doplní ceny přes nákupní trychtýř – průběžně a souběžně.

        ZSSK v seznamu spojů ceny neukazuje a JSON API nemá; cenu zná až
        stránka "Voľba cestovného lístka". Pro každý spoj se proto projde
        Kúpiť lístok → volba cestujících → cena → zpět na seznam.

        * Každá zjištěná cena se hned pošle dál (``publish``) – UI ji ukáže,
          aniž by čekalo na poslední spoj.
        * Trychtýř běží souběžně v ``price_workers`` (výchozí 3) oddělených
          kontextech prohlížeče. Každý kontext je pro ZSSK jiný návštěvník
          (vlastní JSF relace), takže si rozpracované nákupy nepřepisují.
        * Ceny se mezi spoji **nesdílí** (dynamické ceny, zahraniční
          lístky) – každý spoj má cenu ze svého vlastního průchodu.
        * Selhání jednoho spoje ostatní neshodí; spoj se zkusí ještě jednou
          (klidně v jiném kontextu). Kontext, který selže dvakrát po sobě,
          končí – další pokusy by jen pálily čas.
        """
        limit = int(self.options.get("price_limit", PRICE_SCOPE_COUNT))
        pending = [j for j in journeys if j.price is None][:limit]
        if not pending:
            return

        workers = max(1, min(int(self.options.get("price_workers", 3)), len(pending)))
        log.info("%s: zjišťuji ceny pro %d spojů (%d souběžně)",
                 self.name, len(pending), workers)
        queue: asyncio.Queue = asyncio.Queue()
        for journey in pending:
            queue.put_nowait(journey)
        attempts: dict[int, int] = {}
        unsellable: list[Journey] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self.options.get("price_budget", 110))
        tariff = _tariff_label(query.lead)

        async def worker(number: int) -> None:
            if number == 0:
                wpage = page
            else:
                try:
                    wpage = await session.new_isolated_page()
                    await self._open_list(session, wpage, query)
                except Exception as exc:
                    log.info("%s: souběžný kontext %d se nespustil (%s)", self.name, number,
                             str(exc).splitlines()[0][:160])
                    return
            failures_in_row = 0
            while not queue.empty() and loop.time() < deadline:
                journey = queue.get_nowait()
                try:
                    await self._ensure_list(session, wpage, query)
                    amount = await self._price_via_funnel(wpage, journey, query)
                except _NotSellable:
                    # Vlastnost spoje, ne porucha webu – bez opakování.
                    journey.fare_status = FareStatus.NOT_BOOKABLE
                    unsellable.append(journey)
                    # Když neprojde ani jeden z prvních spojů a žádná cena
                    # není, ZSSK tuhle trasu online neprodává vůbec (Praha →
                    # Vídeň) – další průchody by jen pálily čas.
                    if len(unsellable) >= workers and not any(j.price for j in pending):
                        while not queue.empty():
                            queue.get_nowait()
                        self.note("ZSSK tuto trasu přes e-shop neprodává – ceny hledejte "
                                  "u jiného dopravce")
                    self.publish(journeys)
                    continue
                except Exception as exc:
                    failures_in_row += 1
                    tries = attempts[id(journey)] = attempts.get(id(journey), 0) + 1
                    log.info("%s: cenu spoje %s nezjišťuji (%s)", self.name,
                             journey.departure.strftime("%H:%M"),
                             str(exc).splitlines()[0][:160])
                    if tries < 2:
                        queue.put_nowait(journey)
                    if failures_in_row >= 2:
                        return
                    continue
                failures_in_row = 0
                if amount is not None:
                    journey.fares = [self.make_fare(amount, "EUR", label="ZSSK", tariff=tariff)]
                    journey.fare_status = FareStatus.PRICED
                    self.publish(journeys)

        await asyncio.gather(*(worker(n) for n in range(workers)))

        priced = sum(1 for j in pending if j.price is not None)
        offline = sum(1 for j in pending if j.fare_status is FareStatus.NOT_BOOKABLE)
        if offline >= workers and not priced:
            return      # celá trasa je mimo e-shop – poznámka už padla výš
        if offline:
            self.note(f"{offline} {'spoj' if offline == 1 else 'spoje' if offline < 5 else 'spojů'} "
                      "ZSSK online neprodává (lůžkový vůz či místenka) – cenu ověřte u pokladny")
        if priced + offline < len(pending):
            self.note(f"ceny ZSSK se podařilo zjistit u {priced} z {len(pending) - offline} spojů")

    async def _ensure_list(self, session, page, query: SearchQuery) -> None:
        """Vrátí stránku na seznam spojů – v nejhorším novým hledáním.

        ZSSK si poslední hledání pamatuje v relaci, takže stačí znovu
        otevřít ``/search`` (~1 s). ``page.go_back()`` naopak čeká na
        událost "load", kterou ZSSK kvůli reklamním skriptům nevyšle.
        """
        groups = page.locator(CONNECTION_GROUP).first
        if "/search" in page.url and await _visible(groups, 300):
            return
        try:
            await page.goto(SEARCH_URL, wait_until="commit", timeout=15000)
        except Exception:
            pass
        if await _visible(groups, 8000):
            return
        log.info("%s: seznam se neobnovil, hledám znovu", self.name)
        await self._open_list(session, page, query)

    async def _find_group(self, page, journey: Journey):
        """Najde řádek spoje podle časů – pořadí se mezi relacemi může lišit."""
        groups = page.locator(CONNECTION_GROUP)
        wanted = ((journey.departure.hour, journey.departure.minute),
                  (journey.arrival.hour, journey.arrival.minute))
        count = await groups.count()
        guess = int(journey.journey_id or 0)
        order = ([guess] if guess < count else []) + [i for i in range(count) if i != guess]
        for index in order:
            times = _TIME_RE.findall(await groups.nth(index).inner_text())
            if len(times) >= 2:
                found = ((int(times[0][0]), int(times[0][1])),
                         (int(times[-1][0]), int(times[-1][1])))
                if found == wanted:
                    return groups.nth(index)
        raise ProviderError(self.name, "spoj už v seznamu není")

    async def _price_via_funnel(self, page, journey: Journey, query: SearchQuery) -> Decimal | None:
        """Projde nákupní trychtýř jednoho spoje a přečte koncovou cenu."""
        group = await self._find_group(page, journey)
        buy = group.locator(
            "a.btn-primary:has(.fa-shopping-cart), a:has-text('Kúpiť lístok')"
        ).first
        if not await buy.count():
            raise ProviderError(self.name, "u spoje chybí tlačítko Kúpiť lístok")
        await buy.evaluate("node => node.click()")

        # Buď rovnou stránka cestujících, nebo u zpožděného vlaku modální
        # okno "Kupujete lístok do meškajúceho vlaku". Čekáme na to, co
        # přijde dřív – dřív se na modál pokaždé čekalo 2,5 s naprázdno.
        heading = page.locator("h1:has-text('Voľba cestujúcich')")
        modal_button = page.locator("#modalBuy .btn-primary")
        await page.locator("h1:has-text('Voľba cestujúcich'), #modalBuy .btn-primary:visible") \
            .first.wait_for(state="visible", timeout=20000)
        if await modal_button.first.is_visible():
            await modal_button.first.click(force=True)
            await heading.wait_for(state="visible", timeout=20000)

        await self._choose_passenger(page, query.lead)

        offer = page.locator("h1:has-text('Voľba cestovného lístka')")
        async with _jsf_round_trip(page, timeout=20000):
            await page.locator("[data-cy='continue']").first.click(force=True)
        try:
            await offer.wait_for(state="visible", timeout=4000)
        except Exception as exc:
            # Server odpověděl, ale formulář zůstal na volbě cestujících –
            # bez hlášky. Typicky lůžkový vůz nebo trasa, kterou ZSSK online
            # neprodá (např. Praha → Vídeň). Zkoušet znovu nemá smysl.
            if "/passenger" in page.url:
                raise _NotSellable(self.name, "spoj nejde koupit přes e-shop ZSSK") from exc
            await offer.wait_for(state="visible", timeout=10000)
        return await self._read_final_price(page)

    async def _read_final_price(self, page, timeout: float = 12.0) -> Decimal | None:
        """Cena se dopočítává AJAXem – čteme ji, až v ní je částka."""
        selectors = ["#finalPrice", "#final_price", "h2:has-text('Celková suma')",
                     "[data-cy='finalPrice']"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            amount = _parse_eur(await _first_text(page, selectors) or "")
            if amount is not None:
                return amount
            await page.wait_for_timeout(250)
        return None

    async def _choose_passenger(self, page, passenger: Passenger) -> None:
        """Nastaví věkovou kategorii, slevovou kartu a případně 100% slevu.

        Výchozí je "Dospelý 26 - 61 r." a "Bez dodatočnej zľavy" – když
        už odpovídá, nic se nepřeklikává (každá změna je JSF požadavek
        na server). Po změně se čeká na odpověď serveru, ne pevnou dobu.
        """
        await self._select(page, "ageCategory-0", "ageCategoryList-0", _age_pattern(passenger))
        card_pattern = "ISIC" if passenger.is_student else r"Bez|žiadny"
        await self._select(page, "discountType-0", "discountTypeList-0", card_pattern)

        # 100% sleva – checkbox se přepíná jen když neodpovídá požadavku.
        wants_free = passenger.has(DiscountCard.SK_FREE)
        label = page.locator("label[data-cy='freeTicket-0']").first
        try:
            if await label.count() and await label.is_visible():
                checked = await label.evaluate(
                    """label => {
                        let input = label.control
                            || label.parentElement.querySelector('input[type="checkbox"]');
                        return input ? input.checked : false;
                    }"""
                )
                if checked != wants_free:
                    async with _jsf_round_trip(page):
                        await label.click()
        except Exception:
            pass

    async def _select(self, page, field: str, listing: str, pattern: str) -> None:
        dropdown = page.locator(f"[data-cy='{field}']").first
        if not await dropdown.count():
            return
        regex = re.compile(pattern, re.IGNORECASE)
        try:
            if regex.search((await dropdown.inner_text()).strip()):
                return
        except Exception:
            pass
        await dropdown.click()
        option = page.locator(f"[data-cy='{listing}'] li").filter(has_text=regex).first
        try:
            await option.wait_for(state="visible", timeout=3000)
        except Exception:
            return
        async with _jsf_round_trip(page):
            await option.click()


class _jsf_round_trip:
    """Počká na odpověď JSF AJAXu po akci (místo pevného ``wait_for_timeout``)."""

    def __init__(self, page, timeout: float = 8000) -> None:
        self._cm = page.expect_response(
            lambda r: r.request.method == "POST" and "predaj.zssk.sk" in r.url,
            timeout=timeout,
        )

    async def __aenter__(self):
        await self._cm.__aenter__()
        return self

    async def __aexit__(self, *exc):
        try:
            await self._cm.__aexit__(*exc)
        except Exception:
            pass   # odpověď nepřišla včas – pokračujeme, cenu ověří další krok
        return False


# ---- pomocné ----------------------------------------------------------------
async def _visible(locator, timeout_ms: int) -> bool:
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _first_text(scope, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            locator = scope.locator(selector).first
            if await locator.count():
                text = (await locator.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


class _NotSellable(ProviderError):
    """Formulář u spoje nepokračuje (lůžkový vůz, místenka…) – opakovat nemá smysl."""


_EVENT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s+(.+?)\s*$")
_TRAIN_RE = re.compile(r"^/?\s*([A-Za-z]{1,5}\s+\d{1,6})\b")


def _day_after_query(when: dt.datetime, query: SearchQuery) -> dt.datetime:
    """ZSSK u nočních spojů vypisuje čas bez data – 00:36 je už další den."""
    if when < query.departure - dt.timedelta(hours=1):
        return when + dt.timedelta(days=1)
    return when


def _legs_from_text(text: str, query: SearchQuery) -> list[Leg]:
    """Rozloží řádek spoje ZSSK na jednotlivé vlaky.

    Text vypadá takto (odjezd, vlak, příjezd, odjezd, vlak, příjezd…)::

        22:16  Praha hl.n.
        EN 443
        04:48  Vrútky
        06:05  Vrútky
        R 931
        / Os 7307          ← průjezdný vůz, stejný úsek
        08:20  Zvolen os.st.

    Dřív se bral jen první a poslední čas a první vlak, takže spoj se třemi
    přestupy vypadal jako přímý.
    """
    events: list[tuple[int, int, str]] = []
    trains: list[list[str]] = []
    for line in text.splitlines():
        event = _EVENT_RE.match(line)
        if event and "\t" in line:
            events.append((int(event.group(1)), int(event.group(2)), event.group(3).strip()))
            trains.append([])
            continue
        train = _TRAIN_RE.match(line.strip())
        if train and events:
            trains[-1].append(train.group(1))
    if len(events) < 2 or len(events) % 2:
        return []

    stamps: list[dt.datetime] = []
    for hour, minute, _ in events:
        stamp = _combine(query.departure, (str(hour), str(minute)))
        if not stamps:
            stamp = _day_after_query(stamp, query)
        else:
            stamp = stamp.replace(year=stamps[-1].year, month=stamps[-1].month, day=stamps[-1].day)
            if stamp < stamps[-1]:
                stamp += dt.timedelta(days=1)
        stamps.append(stamp)

    legs = []
    for i in range(0, len(events), 2):
        origin, destination = events[i][2], events[i + 1][2]
        departure, arrival = stamps[i], stamps[i + 1]
        legs.append(Leg(
            origin=origin, destination=destination, departure=departure, arrival=arrival,
            mode=Mode.TRAIN, carrier="ZSSK", line=" / ".join(trains[i]) or "ZSSK vlak",
            stops=[Stop(name=origin, departure=departure), Stop(name=destination, arrival=arrival)],
        ))
    return legs


def _sk_date(when: dt.datetime) -> str:
    """ZSSK chce '1. 10. 2026' – bez vedoucích nul a s mezerami."""
    return f"{when.day}. {when.month}. {when.year}"


def _combine(base: dt.datetime, hhmm: tuple[str, str]) -> dt.datetime:
    hour, minute = int(hhmm[0]), int(hhmm[1])
    return base.replace(hour=min(hour, 23), minute=min(minute, 59),
                        second=0, microsecond=0,
                        tzinfo=base.tzinfo or DEFAULT_TZ)


def _parse_eur(text: str) -> Decimal | None:
    """Vytáhne částku v eurech. 0 € je platná cena (100% sleva), ne chyba."""
    if not text:
        return None
    match = _PRICE_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", "."))
    except Exception:
        return None


def _age_pattern(passenger: Passenger) -> str:
    """Věková kategorie ZSSK: dieťa do 15, mladý 16–25, dospelý 26–61, senior 62+.

    Student s ISIC nad 25 let spadá u ZSSK do dospělých – slevu mu dá
    až karta ISIC vybraná zvlášť v "zľava".
    """
    if passenger.age < 16:
        return r"15"
    if passenger.age <= 25:
        return r"25"
    if passenger.age >= 62:
        return r"62"
    return r"26"


def _tariff_label(passenger: Passenger) -> str:
    if passenger.has(DiscountCard.SK_FREE):
        return "Bezplatná preprava"
    if passenger.is_student:
        return "ISIC"
    return "Obyčajné"
