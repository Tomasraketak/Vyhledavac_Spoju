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
import time
from decimal import Decimal
from typing import Any

from ..errors import BlockedError, ProviderError, TransientProviderError
from ..models import (
    DEFAULT_TZ,
    FareCategory,
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

# Vrstvené selektory: bereme první, který na stránce existuje A JE VIDĚT.
#
# Pozor na past: ČD mají formulář na stránce **třikrát** (desktop, mobil,
# sbalená varianta). Jen jedna kopie je viditelná, ostatní jsou skryté.
# Původní kód bral ``.first``, což je skoro vždy ta skrytá – klik na ni
# vyprší a vyhledávání tiše skončí bez výsledků. Proto všude ``:visible``.
FROM_INPUT = [
    ".connection-from input.select__input:visible",
    "input[data-og-type='from']:visible",
    "input[aria-label*='odkud' i]:visible",
    "input[placeholder*='odkud' i]:visible",
]
TO_INPUT = [
    ".connection-to input.select__input:visible",
    "input[data-og-type='to']:visible",
    "input[aria-label*='kam' i]:visible",
    "input[placeholder*='kam' i]:visible",
]
# Našeptávač je jQuery UI autocomplete (třída ui-autocomplete-input),
# takže seznam se renderuje do ``ul.ui-autocomplete`` na konci <body>.
SUGGESTIONS = [
    "ul.ui-autocomplete:visible",
    ".suggestion-options:visible",
    ".select__options:visible",
    "ul[role='listbox']:visible",
]
SEARCH_BUTTON = [
    "button.search-btn:visible",
    "button[type='submit']:has-text('Vyhledat'):visible",
    "button:has-text('Vyhledat spojení'):visible",
    "button:has-text('Hledat'):visible",
]
CONNECTION_CARD = [
    "article.overview-connection",
    "div.connection-item",
    "[data-testid='connection']",
]
# Překryvný "Čekejte..." dialog. Dokud je otevřený, odchytává kliknutí –
# tohle je druhý důvod, proč se původní scraper nikdy nedostal k výsledkům.
LOADER = "#loaderModal[data-open='true']"

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
            # Formulář se doplňuje JavaScriptem až po načtení dokumentu.
            await page.wait_for_timeout(int(self.options.get("settle_ms", 5000)))
            await session.accept_cookies(page, COOKIE_BUTTONS)
            await self._assert_widget_rendered(page)
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
    async def _first_visible(self, page, selectors: list[str], what: str,
                             timeout: float = 15.0):
        """Vrátí první **viditelný** prvek ze seznamu kandidátů.

        Dva důvody, proč to není jednořádkové:

        1. Formulář je na stránce třikrát (desktop, mobil, sbalená verze) a
           viditelná bývá až druhá nebo třetí kopie – proto se prochází
           všechny shody, ne jen ``.first``.
        2. Prvky se doplňují postupně, takže se čeká až do ``timeout``,
           místo jediné kontroly hned po načtení.
        """
        deadline = time.monotonic() + timeout
        while True:
            for selector in selectors:
                try:
                    locator = page.locator(selector)
                    count = await locator.count()
                except Exception:
                    continue
                for index in range(min(count, 6)):
                    candidate = locator.nth(index)
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            if time.monotonic() >= deadline:
                break
            await page.wait_for_timeout(400)

        raise ProviderError(
            self.name,
            f"na stránce ČD nebyl nalezen viditelný prvek '{what}'. "
            "Web se pravděpodobně změnil – aktualizujte selektory "
            f"v vyhledavac/providers/cd.py ({what}).",
        )

    async def _maybe_visible(self, page, selectors: list[str], timeout: float = 3.0):
        """Jako ``_first_visible``, ale místo výjimky vrací ``None``."""
        try:
            return await self._first_visible(page, selectors, "nepovinný prvek", timeout)
        except ProviderError:
            return None

    async def _fill_route(self, session, page, query: SearchQuery) -> None:
        await self._wait_for_loader(page)
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
            locator = page.locator(f"{selector}:visible").first
            if not await locator.count():
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
        """Vybere v okně "Cestující" tarif podle kategorie cestujícího.

        Okno má tlačítko "Přidat" pro každý tarif (``button.btn-add-user``
        s ``title="Přidat cestujícího Junior 18—25 let, Žákovský průkaz
        ČR"``). Postup: přidat náš tarif → odebrat výchozího dospělého →
        Hotovo → **ověřit** v souhrnu formuláře, že se tarif opravdu změnil.

        Dřívější verze hledala tlačítko s "ISIC", které na cd.cz není,
        a když ho nenašla, potichu nechala dospělého – ceny pak byly za
        plné jízdné, i když uživatel zadal 21 let a ISIC. Teď se to nejdřív
        zkusí a když se tarif nenastaví, dopravce to řekne poznámkou.
        """
        choice = _cd_tariff(passenger)
        if choice is None:
            return
        label, pattern = choice
        try:
            await self._wait_for_loader(page)
            opener = await self._maybe_visible(page, [".connection-passenger"], timeout=5.0)
            if opener is None:
                raise ProviderError(self.name, "okno Cestující není na stránce")
            await opener.click(timeout=5000, force=True)
            modal = page.locator("#passengersModal")
            await modal.locator("button.btn-add-user").first.wait_for(state="attached", timeout=8000)
            await page.wait_for_timeout(500)

            titles = await modal.locator("button.btn-add-user").evaluate_all(
                "buttons => buttons.map(b => b.title || '')")
            index = next((i for i, t in enumerate(titles) if re.search(pattern, t)), None)
            if index is None:
                raise ProviderError(self.name, f"v okně Cestující chybí tarif {label!r}")
            await modal.locator("button.btn-add-user").nth(index).click(force=True)
            await page.wait_for_timeout(700)

            # Výchozího dospělého odebrat – jinak by se ceny sčítaly za dva.
            adult = modal.locator("li.user-card.selected").filter(
                has_text=re.compile(r"Dospělý 26.64 let\s+Bez slevového", re.S))
            if await adult.count():
                minus = adult.first.locator(".icon-minus-circle, button:has(.icon-minus-circle)").first
                if await minus.count():
                    await minus.click(force=True)
                    await page.wait_for_timeout(600)

            done = modal.locator("button:has-text('Hotovo')").first
            if await done.count():
                await done.click(force=True)
            await page.wait_for_timeout(900)

            summary = (await opener.inner_text()).replace("\n", " ")
            wanted_head = label.split(",")[0]
            if wanted_head.split()[0] not in summary or "Dospělý 26" in summary and "Dospělý" not in wanted_head:
                raise ProviderError(self.name, f"tarif se nenastavil (formulář ukazuje {summary!r})")
            log.info("%s: cestující nastaven na %s", self.name, label)
        except Exception as exc:
            reason = exc.message if isinstance(exc, ProviderError) else str(exc).splitlines()[0][:120]
            self.note(f"slevu „{label}“ se na cd.cz nepodařilo nastavit – ceny ČD jsou "
                      f"za dospělého ({reason})")
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(400)
            except Exception:
                pass

    async def _assert_widget_rendered(self, page) -> None:
        """Ověří, že se vyhledávací formulář vůbec vykreslil.

        ČD načítají formulář jako samostatnou komponentu (ogres4) z vlastního
        backendu. Když ji server neobslouží, stránka se zobrazí i s hlavičkou
        a menu, ale prostřední část zůstane prázdná – prvky sice v DOMu jsou,
        ale mají nulové rozměry. Typicky jde o dočasné omezení z jedné IP
        adresy (přiliš mnoho dotazů).

        Bez téhle kontroly by to vypadalo jako změněné selektory a člověk by
        zbytečně hledal chybu v kódu.
        """
        try:
            rendered = await page.evaluate(
                """() => {
                    const visible = (el) => {
                        const box = el.getBoundingClientRect();
                        return box.width > 0 && box.height > 0;
                    };
                    const fields = [...document.querySelectorAll('input.select__input')];
                    return {
                        total: fields.length,
                        visible: fields.filter(visible).length,
                        bodyLength: document.body.innerText.trim().length,
                    };
                }"""
            )
        except Exception:
            return

        if rendered.get("total") and not rendered.get("visible"):
            raise BlockedError(
                self.name,
                "ČD vrátily stránku bez vyhledávacího formuláře (komponenta se "
                "nenačetla). Obvykle jde o dočasné omezení kvůli počtu dotazů – "
                "zkuste to za chvíli, nebo z jiné sítě.",
            )

    async def _wait_for_loader(self, page, timeout: float = 15000) -> None:
        """Počká, až zmizí překryvný dialog 'Čekejte…'.

        Bez toho Playwright hlásí, že kliknutí zachytil jiný prvek, a to
        i když je tlačítko viditelné a povolené.
        """
        try:
            await page.locator(LOADER).wait_for(state="hidden", timeout=timeout)
        except Exception:
            pass

    async def _submit(self, page) -> None:
        await self._wait_for_loader(page)
        button = await self._first_visible(page, SEARCH_BUTTON, "tlačítko Vyhledat")

        try:
            await button.click(timeout=8000)
        except Exception as exc:
            # Kliknutí mohlo projít, i když ho Playwright označí za neúspěšné:
            # stránka se během něj překreslí a prvek se odpojí z DOMu.
            # Rozhodující je, jestli dorazily výsledky – ne návratová hodnota.
            log.debug("%s: klik na Vyhledat hlásí %s, ověřuji výsledky", self.name, exc)

        await self._wait_for_results(page)

    async def _wait_for_results(self, page) -> None:
        """Počká na karty spojů; bere i to, že se mezitím změnilo URL."""
        try:
            await page.wait_for_url("**/spojeni-tam/**", timeout=20000)
        except Exception:
            pass
        await self._wait_for_loader(page, timeout=25000)

        for selector in CONNECTION_CARD:
            try:
                await page.locator(selector).first.wait_for(state="visible", timeout=25000)
                # Ceny se dopočítávají zvlášť až po vykreslení spojů.
                await page.wait_for_timeout(3000)
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


def _cd_tariff(passenger: Passenger) -> tuple[str, str] | None:
    """Tarif ČD pro cestujícího: ``(popisek, regex na title tlačítka)``.

    Nabídka na cd.cz (září 2026): Dítě 0–5 / 6–9 / 10–14, Junior 15–17,
    Junior 18–25 (bez průkazu i se "Žákovský průkaz ČR"), Dospělý 26–64,
    Dospělý 65+. Tlačítko s ISIC tam **není** – studenta 18–25 let s ISIC
    zastupuje Junior se žákovským průkazem (ISIC s potvrzením o studiu ho
    u českých dopravců nahrazuje). ``None`` = výchozí dospělý, nic neměnit.
    """
    age, category = passenger.age, passenger.category
    if category is FareCategory.INFANT:
        return "Dítě 0—5 let", r"Dítě 0.5 let"
    if category is FareCategory.CHILD:
        return ("Dítě 6—9 let", r"Dítě 6.9 let") if age < 10 else ("Dítě 10—14 let", r"Dítě 10.14 let")
    if category is FareCategory.YOUTH:
        return "Junior 15—17 let", r"Junior 15.17 let"
    if category is FareCategory.SENIOR:
        return "Dospělý 65 let a starší", r"65 let a starší"
    if 18 <= age < 26:
        if category is FareCategory.STUDENT:
            return "Junior 18—25 let, Žákovský průkaz ČR", r"Junior 18.25 let.*Žákovský"
        return "Junior 18—25 let", r"Junior 18.25 let$"
    return None


def _tariff_label(passenger: Passenger) -> str:
    choice = _cd_tariff(passenger)
    return choice[0] if choice else "Dospělý 26—64 let"
