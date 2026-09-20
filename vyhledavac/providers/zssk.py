"""ZSSK – Železničná spoločnosť Slovensko.

Prodejní web ``predaj.zssk.sk`` je JSF aplikace bez veřejného API, takže
tady prohlížeč zůstává. Selektory z původního kódu (`#fromInput`,
`#toInput`, `#departDate`, `#actionSearchConnectionButton`) jsou ověřeně
**stále platné** – na rozdíl od ČD se web nezměnil.

Hlavní změna oproti původnímu ``ZSSK_Fetch.py`` je v získávání cen.
Původní kód u **každého** spoje proklikal celý nákupní trychtýř (otevřít
spoj → Kúpiť lístok → volba cestujúcich → volba lístka → přečíst cenu →
dvakrát zpět). To je 5–6 načtení stránky na jeden spoj; u deseti spojů
i několik minut a jediná chyba uprostřed shodila zbytek.

Tady je to dvoustupňově:

1. **Rychlý průchod** – ceny, které jsou vidět rovnou v seznamu spojů.
2. **Hloubkový režim** (volba ``deep_prices``) – trychtýř, ale jen pro
   prvních N spojů, každý ve vlastní záložce, a selhání jednoho spoje
   ostatní neovlivní.

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

    async def search(self, query: SearchQuery) -> list[Journey]:
        session = await self.browser()
        page = await session.new_page()
        try:
            await session.goto(page, SEARCH_URL)
            await session.accept_cookies(page, COOKIE_BUTTONS)
            await self._fill_form(session, page, query)
            await self._submit(page)
            journeys = await self._parse_list(page, query)

            if self.options.get("deep_prices", False):
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
        await session.human_type(origin, query.origin, page)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(600)

        destination = page.locator("#toInput").first
        await session.human_type(destination, query.destination, page)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(600)

        # ZSSK čeká slovenský formát data s mezerami: "1. 10. 2026".
        await self._set_value(page, "#departDate", _sk_date(query.departure))
        await self._set_value(page, "#departTime", query.departure.strftime("%H:%M"))

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

    # ---- hloubkové ceny ------------------------------------------------------
    async def _fill_missing_prices(self, session, page, journeys, query) -> None:
        """Doplní ceny přes nákupní trychtýř – pomalé, proto jen pro pár spojů.

        Každý spoj se řeší samostatně a jeho selhání se jen zaloguje.
        """
        budget = int(self.options.get("price_limit", 5))
        pending = [j for j in journeys if j.price is None][:budget]
        if not pending:
            return

        log.info("%s: dopočítávám ceny pro %d spojů (hloubkový režim)",
                 self.name, len(pending))
        for journey in pending:
            try:
                amount = await self._price_via_funnel(page, int(journey.journey_id), query)
            except Exception as exc:
                log.debug("%s: cenu spoje %s nezjišťuji (%s)",
                          self.name, journey.journey_id, exc)
                continue
            if amount is not None:
                journey.fares = [self.make_fare(amount, "EUR", label="ZSSK",
                                                tariff=_tariff_label(query.lead))]
                journey.fare_status = FareStatus.PRICED

    async def _price_via_funnel(self, page, index: int, query: SearchQuery) -> Decimal | None:
        """Projde nákupní trychtýř a přečte koncovou cenu."""
        group = page.locator(CONNECTION_GROUP).nth(index)
        header = group.locator(".connection").first
        if await header.count():
            await header.click()
            await page.wait_for_timeout(800)

        buy = group.locator(
            "a.btn-primary:has(.fa-shopping-cart), a:has-text('Kúpiť lístok')"
        ).first
        if not await buy.count():
            return None
        await buy.evaluate("node => node.click()")

        # Někdy vyskočí modál s upozorněním na zpoždění.
        modal = page.locator("#modalBuy").first
        try:
            if await modal.is_visible(timeout=2000):
                await page.locator("#modalBuy .btn-primary").first.click()
                await page.wait_for_timeout(800)
        except Exception:
            pass

        await page.locator("h1:has-text('Voľba cestujúcich')").wait_for(
            state="visible", timeout=15000)
        await page.wait_for_timeout(1000)

        await self._choose_passenger(page, query.lead)

        await page.locator("[data-cy='continue']").first.click()
        await page.locator("h1:has-text('Voľba cestovného lístka')").wait_for(
            state="visible", timeout=15000)
        await page.wait_for_timeout(2000)

        text = await _first_text(page, ["#finalPrice", "#final_price",
                                        "h2:has-text('Celková suma')"])
        amount = _parse_eur(text or "")

        # Zpět na seznam, aby šel zpracovat další spoj.
        await page.go_back()
        await page.wait_for_timeout(900)
        if not await page.locator(CONNECTION_GROUP).first.is_visible():
            await page.go_back()
            await page.wait_for_timeout(900)
        return amount

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

        card_pattern = "ISIC" if passenger.has(DiscountCard.ISIC) else r"Bez|žiadny"
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
    if passenger.has(DiscountCard.ISIC):
        return "ISIC"
    return "Obyčajné"
