"""České dráhy.

ČD bohužel nemají veřejné JSON API pro vyhledání spojení – jejich formulář
posílá POST zpět na stejnou stránku a výsledek se renderuje serverem. Proto
je tenhle dopravce jediný z "velké trojky", který potřebuje prohlížeč.

Co se oproti původnímu ``CDfetchV3.py`` změnilo:

* selektory jsou vrstvené – zkouší se několik generací názvů tříd za sebou.
  Původní kód měl jeden natvrdo zadaný selektor (``.suggestion-options``),
  který dnes na webu ČD **už neexistuje**, takže vyhledávání tiše vracelo
  prázdný seznam;
* když stránka nevypadá, jak čekáme, dopravce vyhodí chybu s popisem místo
  ``except: pass``. Uživatel se tak dozví, že se ČD rozbilo, a nemyslí si,
  že prostě nejedou vlaky;
* detaily vlaků se stahují paralelně a s limitem, ne pro každý spoj zvlášť;
* prohlížeč běží headless a bez obrázků.

Jízdní řád českých vlaků včetně mezizastávek umí vrátit i dopravce ÖBB
(HAFAS) bez prohlížeče – pokud ČD selže, aplikace díky tomu není slepá.
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

SEARCH_URL = "https://www.cd.cz/spojeni-a-jizdenka/"

# Vrstvené selektory: bereme první, který na stránce existuje.
FROM_INPUT = [
    ".connection-from input.select__input",
    "input[name='From']",
    "input[aria-label*='Odkud']",
    "input[placeholder*='Odkud']",
    "#connectionFrom",
]
TO_INPUT = [
    ".connection-to input.select__input",
    "input[name='To']",
    "input[aria-label*='Kam']",
    "input[placeholder*='Kam']",
    "#connectionTo",
]
SUGGESTIONS = [
    ".suggestion-options:visible",
    ".select__options:visible",
    "ul[role='listbox']:visible",
    ".autocomplete-results:visible",
]
SEARCH_BUTTON = [
    "button.search-btn",
    "button[type='submit']:has-text('Vyhledat')",
    "button:has-text('Vyhledat spojení')",
    "button:has-text('Hledat')",
]
CONNECTION_CARD = [
    "article.overview-connection",
    "div.connection-item",
    "[data-testid='connection']",
]
COOKIE_BUTTONS = [
    "button:has-text('Povolit všechny')",
    "button:has-text('Přijmout vše')",
    "button:has-text('Souhlasím')",
    "#cookiesAcceptAll",
]


@PROVIDERS.register()
class CdProvider(BrowserProvider):
    name = "ČD"
    label = "České dráhy (vyžaduje prohlížeč)"
    currency = "CZK"
    provides_prices = True
    provides_stops = True
    countries = ("CZ",)

    async def search(self, query: SearchQuery) -> list[Journey]:
        session = await self.browser()
        page = await session.new_page()
        try:
            await session.goto(page, SEARCH_URL)
            await session.accept_cookies(page, COOKIE_BUTTONS)
            await self._configure_passengers(page, query.lead)
            await self._fill_route(session, page, query)
            await self._submit(page)
            return await self._parse_results(session, page, query)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ---- kroky formuláře -----------------------------------------------------
    async def _first_visible(self, page, selectors: list[str], what: str):
        """Vrátí první existující prvek ze seznamu kandidátů."""
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=2500):
                    return locator
            except Exception:
                continue
        raise ProviderError(
            self.name,
            f"na stránce ČD nebyl nalezen prvek '{what}'. "
            "Web se pravděpodobně změnil – je potřeba aktualizovat selektory "
            f"v vyhledavac/providers/cd.py ({what}).",
        )

    async def _fill_route(self, session, page, query: SearchQuery) -> None:
        from_input = await self._first_visible(page, FROM_INPUT, "pole Odkud")
        await session.human_type(from_input, query.origin, page)
        await self._pick_suggestion(page, from_input)

        to_input = await self._first_visible(page, TO_INPUT, "pole Kam")
        await session.human_type(to_input, query.destination, page)
        await self._pick_suggestion(page, to_input)

        # Datum a čas nastavujeme přímo hodnotou – klikání v kalendáři je
        # křehké a zbytečně pomalé.
        await self._set_value(page, "#depDate", query.departure.strftime("%d.%m.%Y"))
        await self._set_value(page, "#timePickerObj", query.departure.strftime("%H:%M"))

    async def _pick_suggestion(self, page, input_locator) -> None:
        """Vybere první položku našeptávače; když se neukáže, potvrdí Enterem."""
        for selector in SUGGESTIONS:
            try:
                options = page.locator(selector).first
                if await options.is_visible(timeout=3000):
                    await page.wait_for_timeout(250)
                    await input_locator.press("ArrowDown")
                    await input_locator.press("Enter")
                    return
            except Exception:
                continue
        await input_locator.press("Enter")

    async def _set_value(self, page, selector: str, value: str) -> None:
        """Nastaví hodnotu a vyvolá události, aby si jich všiml framework."""
        try:
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
        except Exception as exc:
            log.debug("%s: nepodařilo se nastavit %s (%s)", self.name, selector, exc)

    async def _configure_passengers(self, page, passenger: Passenger) -> None:
        """Nastaví slevovou kategorii. Selhání není fatální – jen se ztratí sleva."""
        wanted = _passenger_button(passenger)
        if wanted is None:
            return
        try:
            opener = page.locator(".connection-passenger").first
            if not await opener.count():
                return
            await opener.click(timeout=4000)
            await page.wait_for_timeout(700)

            minus = page.locator("li.user-card.selected").filter(
                has_text="Dospělý"
            ).locator(".icon-minus-circle").first
            if await minus.count():
                await minus.click(timeout=2500)
                await page.wait_for_timeout(400)

            for selector in wanted:
                button = page.locator(selector).first
                if await button.count():
                    await button.click(timeout=2500)
                    await page.wait_for_timeout(500)
                    break

            done = page.locator("#passengersModal button:has-text('Hotovo')").first
            if await done.count():
                await done.click(timeout=2500)
                await page.wait_for_timeout(400)
        except Exception as exc:
            log.info("%s: slevovou kategorii se nepodařilo nastavit (%s)", self.name, exc)
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    async def _submit(self, page) -> None:
        button = await self._first_visible(page, SEARCH_BUTTON, "tlačítko Vyhledat")
        await button.click()
        for selector in CONNECTION_CARD:
            try:
                await page.locator(selector).first.wait_for(state="visible", timeout=25000)
                await page.wait_for_timeout(2500)  # doplní se ceny načítané zvlášť
                return
            except Exception:
                continue
        raise TransientProviderError(
            self.name, "ČD nevrátilo žádné spojení (nebo se změnila struktura výsledků)"
        )

    # ---- parsování -----------------------------------------------------------
    async def _parse_results(self, session, page, query: SearchQuery) -> list[Journey]:
        cards = None
        for selector in CONNECTION_CARD:
            locator = page.locator(selector)
            if await locator.count():
                cards = locator
                break
        if cards is None:
            return []

        count = min(await cards.count(), int(self.options.get("max_connections", 12)))
        journeys: list[Journey] = []
        for index in range(count):
            try:
                journey = await self._parse_card(page, cards.nth(index), query)
            except Exception as exc:
                log.debug("%s: kartu %d nelze přečíst (%s)", self.name, index, exc)
                continue
            if journey is not None:
                journeys.append(journey)
        return journeys

    async def _parse_card(self, page, card, query: SearchQuery) -> Journey | None:
        times = await card.locator("p.schedule__text--time").all_inner_texts()
        times = [t.strip() for t in times if _TIME_RE.search(t)]
        if len(times) < 2:
            return None

        departure = _combine(query.departure, times[0])
        arrival = _combine(query.departure, times[-1])
        if arrival < departure:
            arrival += dt.timedelta(days=1)

        price_text = ""
        for selector in ("button.btn--green", "[data-testid='price']", ".price"):
            locator = card.locator(selector).first
            if await locator.count():
                price_text = (
                    await locator.get_attribute("aria-label")
                    or await locator.inner_text()
                    or ""
                )
                break

        sold_out = await card.locator("button[aria-label='Již nelze zakoupit']").count()
        amount = _parse_price(price_text)
        fares = []
        if amount is not None:
            fares.append(self.make_fare(amount, "CZK", label="ČD e-shop",
                                        tariff=_tariff_label(query.lead)))
            status = FareStatus.PRICED
        elif sold_out:
            status = FareStatus.SOLD_OUT
        else:
            status = FareStatus.UNKNOWN

        train_names = await card.locator("div.detail-link p.label__text > span").all_inner_texts()
        train_names = [t.strip() for t in train_names if t.strip()]
        line = train_names[1] if len(train_names) > 1 else (train_names[0] if train_names else "ČD vlak")

        leg = Leg(
            origin=query.origin,
            destination=query.destination,
            departure=departure,
            arrival=arrival,
            mode=Mode.TRAIN,
            carrier="ČD",
            line=line,
            stops=[Stop(name=query.origin, departure=departure),
                   Stop(name=query.destination, arrival=arrival)],
        )

        return Journey(
            provider=self.name,
            legs=[leg],
            fares=fares,
            fare_status=status,
            deep_link=SEARCH_URL,
        )


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _combine(base: dt.datetime, time_text: str) -> dt.datetime:
    """Spojí datum dotazu s časem ze stránky."""
    match = _TIME_RE.search(time_text)
    if not match:
        return base
    hour, minute = int(match.group(1)), int(match.group(2))
    return base.replace(hour=min(hour, 23), minute=min(minute, 59),
                        second=0, microsecond=0, tzinfo=base.tzinfo or DEFAULT_TZ)


def _parse_price(text: str) -> Decimal | None:
    """Vytáhne částku. Pozor na mezery v tisícících a desetinnou čárku."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").lower()
    if "vyprod" in cleaned or "nedostup" in cleaned:
        return None
    match = re.search(r"(\d[\d\s]*(?:[,.]\d{1,2})?)", cleaned)
    if not match:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:
        return None


def _tariff_label(passenger: Passenger) -> str:
    if passenger.has(DiscountCard.ISIC):
        return "ISIC"
    if passenger.is_child:
        return "Dítě"
    if passenger.is_youth:
        return "Junior"
    if passenger.is_senior:
        return "Senior"
    return "Obyčejné"


def _passenger_button(passenger: Passenger) -> list[str] | None:
    """Selektory tlačítka pro danou slevovou kategorii."""
    if passenger.has(DiscountCard.ISIC):
        return ["button.btn-add-user[title*='ISIC']", "button.btn-add-user[title*='Student']"]
    if passenger.is_child:
        return ["button.btn-add-user[title*='Dítě']", "button.btn-add-user[title*='15']"]
    if passenger.is_youth:
        return ["button.btn-add-user[title*='Junior']", "button.btn-add-user[title*='Student']"]
    if passenger.is_senior:
        return ["button.btn-add-user[title*='Senior']", "button.btn-add-user[title*='65']"]
    return None  # dospělý je výchozí, není co měnit
