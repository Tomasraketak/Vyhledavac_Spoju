"""ZSSK – Železničná spoločnosť Slovensko.

Prodejní web ``predaj.zssk.sk`` je JSF aplikace bez veřejného API, takže
tady prohlížeč zůstává. Selektory z původního kódu (`#fromInput`,
`#toInput`, `#departDate`, `#actionSearchConnectionButton`) jsou ověřeně
**stále platné** – na rozdíl od ČD se web nezměnil.

ZSSK v seznamu spojů ceny neukazuje a JSON API nemá. Cenu zná až nákupní
formulář, takže se – stejně jako v původním ``ZSSK_Fetch.py`` – pro každý
spoj prokliká *Kúpiť lístok → Voľba cestujúcich → Voľba cestovného lístka*
a přečte se konečná cena. Oproti původnímu skriptu:

* trychtýř běží jen pro prvních ``price_limit`` spojů (výchozí 6) a má
  časový rozpočet ``price_budget`` (výchozí 90 s),
* selhání u jednoho spoje ostatní neshodí; po dvou chybách po sobě se
  skončí a uživatel se dozví, u kolika spojů cena chybí,
* návrat na seznam nečeká na událost "load" (kterou ZSSK nikdy nevyšle)
  a když selže, hledání se zopakuje,
* stanice se vybírají až po načtení našeptávače – rozbalený seznam jinak
  překrývá další pole a klik do něj vyprší.

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

from ..errors import ProviderError, TransientProviderError
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
from .base import BrowserProvider

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
    #: Hledání ~25 s + ceny přes trychtýř (rozpočet ``price_budget``).
    min_timeout = 150.0

    async def search(self, query: SearchQuery) -> list[Journey]:
        session = await self.browser()
        page = await session.new_page()
        try:
            await session.goto(page, SEARCH_URL)
            await session.accept_cookies(page, COOKIE_BUTTONS)
            await self._fill_form(session, page, query)
            await self._submit(page)
            journeys = await self._parse_list(page, query)

            # Ceny jsou hlavní smysl celého vyhledávače, takže trychtýř je
            # zapnutý ve výchozím stavu. Vypnout: options={"deep_prices": False}.
            if self.options.get("deep_prices", True):
                await self._fill_missing_prices(session, page, journeys, query)
            return journeys
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ---- formulář ------------------------------------------------------------
    async def _fill_form(self, session, page, query: SearchQuery) -> None:
        origin = page.locator("#fromInput").first
        if not await origin.count():
            raise ProviderError(
                self.name,
                "na predaj.zssk.sk nebylo nalezeno pole '#fromInput' – "
                "web se změnil, aktualizujte selektory v providers/zssk.py",
            )
        await self._pick_station(session, page, origin, query.origin)
        destination = page.locator("#toInput").first
        await self._pick_station(session, page, destination, query.destination)

        # ZSSK čeká slovenský formát data s mezerami: "1. 10. 2026".
        await self._set_value(page, "#departDate", _sk_date(query.departure))
        await self._set_value(page, "#departTime", query.departure.strftime("%H:%M"))

    async def _pick_station(self, session, page, field, name: str) -> None:
        """Napíše stanici a vybere první návrh z našeptávače.

        Našeptávač se plní až z XHR ``/autocompleteStations/…``. Enter
        stisknutý dřív nic nevybere, a dokud je seznam rozbalený, překrývá
        pole "Kam" – klik do něj pak Playwright opakuje do vypršení limitu.
        Proto: počkat na návrhy → Enter → počkat, až seznam zmizí.
        """
        suggestions = page.locator("a.dropdown-item[role='option']")
        await field.click(force=True)
        await field.fill("")
        for char in name:
            await field.press_sequentially(char, delay=60)
        try:
            await suggestions.first.wait_for(state="visible", timeout=6000)
        except Exception:
            log.debug("%s: našeptávač pro %r se neukázal", self.name, name)
        await page.wait_for_timeout(300)
        await field.press("Enter")
        try:
            await suggestions.first.wait_for(state="hidden", timeout=3000)
        except Exception:
            await field.press("Tab")
        await page.wait_for_timeout(400)

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
        await page.wait_for_timeout(1500)

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

            times = _TIME_RE.findall(text)
            if len(times) < 2:
                continue
            departure = _combine(query.departure, times[0])
            arrival = _combine(query.departure, times[-1])
            if arrival < departure:
                arrival += dt.timedelta(days=1)

            train = await _first_text(group, [".trainNumber span", ".trainNumber", ".train-name"])
            fares = []
            status = FareStatus.UNKNOWN
            amount = _parse_eur(text)
            if amount is not None:
                fares.append(self.make_fare(amount, "EUR", label="ZSSK",
                                            tariff=_tariff_label(query.lead)))
                status = FareStatus.PRICED

            leg = Leg(
                origin=query.origin,
                destination=query.destination,
                departure=departure,
                arrival=arrival,
                mode=Mode.TRAIN,
                carrier="ZSSK",
                line=train or "ZSSK vlak",
                stops=[Stop(name=query.origin, departure=departure),
                       Stop(name=query.destination, arrival=arrival)],
            )
            journeys.append(
                Journey(
                    provider=self.name,
                    legs=[leg],
                    fares=fares,
                    fare_status=status,
                    journey_id=str(index),
                    deep_link=SEARCH_URL,
                )
            )
        return journeys

    # ---- ceny přes nákupní trychtýř -------------------------------------------
    async def _fill_missing_prices(self, session, page, journeys, query) -> None:
        """Doplní ceny přes nákupní trychtýř – stejně jako původní skript.

        ZSSK v seznamu spojů ceny neukazuje a JSON API nemá; cenu zná až
        stránka "Voľba cestovného lístka". Proto se pro každý spoj projde
        Kúpiť lístok → volba cestujících → cena → zpět. Je to ~6–10 s na
        spoj, takže se to dělá jen pro prvních ``price_limit`` spojů.

        Selhání jednoho spoje ostatní neshodí. Když web selže dvakrát po
        sobě, další pokusy by jen pálily čas – skončí se a nahlásí se to.
        """
        budget = int(self.options.get("price_limit", 6))
        pending = [j for j in journeys if j.price is None][:budget]
        if not pending:
            return

        log.info("%s: zjišťuji ceny pro %d spojů přes nákupní trychtýř",
                 self.name, len(pending))
        failures_in_row = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self.options.get("price_budget", 90))
        for journey in pending:
            if loop.time() > deadline:
                break
            try:
                await self._ensure_list(session, page, query)
                amount = await self._price_via_funnel(page, int(journey.journey_id), query)
            except Exception as exc:
                failures_in_row += 1
                log.info("%s: cenu spoje %s nezjišťuji (%s)", self.name,
                         journey.journey_id, str(exc).splitlines()[0][:160])
                if failures_in_row >= 2:
                    break
                continue
            failures_in_row = 0
            if amount is not None:
                journey.fares = [self.make_fare(amount, "EUR", label="ZSSK",
                                                tariff=_tariff_label(query.lead))]
                journey.fare_status = FareStatus.PRICED

        priced = sum(1 for j in pending if j.price is not None)
        if priced < len(pending):
            self.note(f"ceny ZSSK se podařilo zjistit u {priced} z {len(pending)} spojů")
        if len(journeys) > budget:
            self.note(f"ceny ZSSK zjišťuji jen u prvních {budget} spojů – každá "
                      "cena znamená projít nákupní formulář (~8 s)")

    async def _ensure_list(self, session, page, query: SearchQuery) -> None:
        """Vrátí stránku na seznam spojů – v nejhorším novým hledáním.

        ``page.go_back()`` čeká na událost "load", kterou ZSSK kvůli
        reklamním skriptům nikdy nevyšle. Proto ``wait_until="commit"``
        a pak čekání na samotný seznam.
        """
        groups = page.locator(CONNECTION_GROUP).first
        for _ in range(2):
            if await _visible(groups, 1500):
                return
            try:
                await page.go_back(wait_until="commit", timeout=15000)
            except Exception:
                pass
            if await _visible(groups, 8000):
                await page.wait_for_timeout(500)
                return
        log.info("%s: návrat na seznam selhal, hledám znovu", self.name)
        await session.goto(page, SEARCH_URL)
        await self._fill_form(session, page, query)
        await self._submit(page)

    async def _price_via_funnel(self, page, index: int, query: SearchQuery) -> Decimal | None:
        """Projde nákupní trychtýř jednoho spoje a přečte koncovou cenu."""
        group = page.locator(CONNECTION_GROUP).nth(index)
        header = group.locator(".connection").first
        if await header.count():
            await header.click(force=True)
            await page.wait_for_timeout(800)

        buy = group.locator(
            "a.btn-primary:has(.fa-shopping-cart), a:has-text('Kúpiť lístok')"
        ).first
        if not await buy.count():
            raise ProviderError(self.name, "u spoje chybí tlačítko Kúpiť lístok")
        await buy.evaluate("node => node.click()")

        # U zpožděných vlaků vyskočí "Kupujete lístok do meškajúceho vlaku".
        modal_button = page.locator("#modalBuy .btn-primary").first
        if await _visible(modal_button, 2500):
            await modal_button.click(force=True)
            await page.wait_for_timeout(800)

        await page.locator("h1:has-text('Voľba cestujúcich')").wait_for(
            state="visible", timeout=20000)
        await page.wait_for_timeout(1200)

        await self._choose_passenger(page, query.lead)

        await page.locator("[data-cy='continue']").first.click(force=True)
        await page.locator("h1:has-text('Voľba cestovného lístka')").wait_for(
            state="visible", timeout=20000)
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
            await page.wait_for_timeout(500)
        return None

    async def _choose_passenger(self, page, passenger: Passenger) -> None:
        """Nastaví věkovou kategorii, slevovou kartu a případně 100% slevu."""
        age_pattern = _age_pattern(passenger)
        dropdown = page.locator("[data-cy='ageCategory-0']").first
        if await dropdown.count():
            await dropdown.click()
            await page.wait_for_timeout(400)
            option = page.locator("[data-cy='ageCategoryList-0'] li").filter(
                has_text=re.compile(age_pattern, re.IGNORECASE)).first
            if await option.count():
                await option.click()
            await page.wait_for_timeout(1200)

        card_pattern = "ISIC" if passenger.is_student else r"Bez|žiadny"
        card = page.locator("[data-cy='discountType-0']").first
        if await card.count():
            await card.click()
            await page.wait_for_timeout(400)
            option = page.locator("[data-cy='discountTypeList-0'] li").filter(
                has_text=re.compile(card_pattern, re.IGNORECASE)).first
            if await option.count():
                await option.click()
            await page.wait_for_timeout(1200)

        # 100% sleva – checkbox se přepíná jen když neodpovídá požadavku.
        wants_free = passenger.has(DiscountCard.SK_FREE)
        label = page.locator("label[data-cy='freeTicket-0']").first
        try:
            if await label.is_visible(timeout=2500):
                checked = await label.evaluate(
                    """label => {
                        let input = label.control
                            || label.parentElement.querySelector('input[type="checkbox"]');
                        return input ? input.checked : false;
                    }"""
                )
                if checked != wants_free:
                    await label.click()
                    await page.wait_for_timeout(1200)
        except Exception:
            pass


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
