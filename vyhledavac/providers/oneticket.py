"""OneTicket – Systém jednotného tarifu (SJT).

OneTicket prodává jízdenky napříč českými dopravci (ČD, Arriva, GW Train
a další) pod jednotným tarifem, takže je to **nejlepší zdroj cen českých
vlaků** – a nevyžaduje scrapovat ČD.

## Proč prohlížeč

OneTicket má zdokumentované REST API na ``clientapi.oneticket.cz``
(OpenAPI: ``/swagger/source/sjt.yml``, UI na ``/swagger/index.html``).
Zjištěný postup by byl:

1. ``POST /search`` s ``sourceStation`` / ``targetStation`` / ``datetime``
   → vrátí spoje a jejich ``pathId``
2. ``POST /price?method=priceBySearchId`` s ``pathId`` a ``passengers``
   ``[{"fare": "1", "quantity": "1"}]`` → vrátí cenu
3. Číselníky: ``/system/data/station``, ``/system/data/fare``, ``/carrier``

**Endpointy pro vyhledávání i ceny ale vyžadují partnerský klíč** –
bez něj vrací ``401 Auth Invalid header`` (veřejně odpovídá jen
``/carrier``, ``/system/data/fare`` a ``/system/codelist/*``). Klíč se
vydává smluvním prodejcům, ne jednotlivcům, a projekt je postavený tak,
aby žádný klíč nepotřeboval. Popis API zůstává jen pro případ, že by ho
OneTicket jednou otevřel.

Proto se čte veřejný web v prohlížeči.

## Skrytý prohlížeč stačí

Starší verze tvrdila, že OneTicket pozná skrytý (headless) prohlížeč a nic
nevykreslí, a proto otevírala viditelné okno. Mýlila se: React aplikace
je po načtení HTML 1–3 s prázdná a kontrola proběhla dřív, než se stihla
vykreslit. Ověřeno živě (září 2026) – headless vrací stejné spoje i ceny.
Zbylá příčina prázdné stránky je výpadek jejich API ``clientapi.oneticket.cz``;
aplikace pak neukáže nic, ani chybovou hlášku.

Dopravce je ve výchozím nastavení vypnutý jen proto, že je pomalý
(~15 s) – zapněte ho, když chcete ceny SJT.

## Data z odpovědí aplikace, ne z HTML

Prohlížeč jen vyplní formulář; spoje i ceny se čtou z JSON odpovědí, které
si aplikace sama stáhne (``/search/location``, ``/search``,
``/price?method=priceBySearchId``). Klíč, který k nim aplikace přidává,
nikde nepoužíváme – posloucháme jen, co prohlížeč dostal. Odpovědi nesou
všechno, co HTML zamlčí: všechny vlaky spoje, mezizastávky s časy,
dopravce a směr. Čtení HTML zůstává jako záloha.

Cena v SJT patří **lístku na trasu** (stejné ``viaStationIds``) a platí na
každý spoj po té trase – web ji proto zjišťuje jen jednou za trasu. Stejně
ji přiřazujeme i my.

Zahraniční stanice OneTicket nezná: ``/search/location`` vrátí 404 a
dopravce to okamžitě nahlásí jako neznámou stanici (dřív se ~45 s čekalo
na spojení, která nepřijdou).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from decimal import Decimal
from typing import Any

from ..errors import ProviderError, StationNotFoundError, TransientProviderError
from ..models import (
    DEFAULT_TZ,
    DiscountCard,
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

SEARCH_URL = "https://oneticket.cz/search"

COOKIE_BUTTONS = [
    "button:has-text('Povolit jen nezbytné')",
    "button:has-text('Souhlasím')",
    "button:has-text('Přijmout')",
    "button:has-text('Rozumím')",
]
FROM_INPUT = ["#from-searchbox", "input[placeholder*='Odkud']", "input[name='from']"]
TO_INPUT = ["#to-searchbox", "input[placeholder*='Kam']", "input[name='to']"]
DATE_INPUT = ['input[data-testid="react-datepicker-date-input"]', "input[type='date']"]
SUBMIT = ['button[data-testid="submit-search"]', "button:has-text('Vyhledat')"]
CONNECTION = ['div[data-testid="connection-item"]', ".connection-item"]

#: Názvy tarifů tak, jak je web ukazuje ve výběru cestujících.
FARE_LABELS = {
    "ADULT": "Plné jízdné",
    "UNDER_18": "Junior 15 - 18 let",
    "STUDENT_26_ISIC": "Student 18 - 26 let",
    "SENIOR": "Senior 65+ let",
}

API_HOST = "clientapi.oneticket.cz"

_PRICE_RE = re.compile(r"(\d[\d\s ]*(?:[.,]\d{1,2})?)\s*(?:Kč|CZK)", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


@PROVIDERS.register()
class OneTicketProvider(BrowserProvider):
    name = "OneTicket"
    label = "OneTicket / SJT – ceny českých vlaků (prohlížeč, ~20 s)"
    currency = "CZK"
    provides_prices = True
    provides_stops = False
    countries = ("CZ",)
    headless = True

    def _fare_key(self, passenger: Passenger) -> str:
        """Vybere tarif SJT podle společné kategorie cestujícího."""
        return {
            FareCategory.SENIOR: "SENIOR",
            FareCategory.STUDENT: "STUDENT_26_ISIC",
            FareCategory.YOUTH: "UNDER_18",
        }.get(passenger.category, "ADULT")

    async def search(self, query: SearchQuery) -> list[Journey]:
        session = await self.browser()
        page = await session.new_page()
        api = _ApiListener()
        page.on("response", api.on_response)
        try:
            await session.goto(page, SEARCH_URL)
            await session.accept_cookies(page, COOKIE_BUTTONS)
            await self._guard_empty_render(page)

            await self._fill_route(session, page, query, api)
            await self._fill_datetime(page, query)
            await self._choose_fares(page, query)
            api.search = None
            await self._submit(page, api)
            if api.search is not None:
                await api.wait_prices(page)
                return self._from_api(api, query)
            log.info("%s: odpověď /search nezachycena, čtu HTML", self.name)
            return await self._parse(page, query)
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _guard_empty_render(self, page, timeout: float = 20.0) -> None:
        """Počká, až React aplikace vykreslí formulář.

        Po načtení HTML je ``body`` 1–3 s prázdné – obsah dokreslí až
        JavaScript. Dřívější verze kontrolovala hned a hlásila, že
        OneTicket "poznal robota", přitože stačilo chvíli počkat.

        Když se nevykreslí ani po ``timeout``, je to skoro vždy tím, že
        aplikaci selhalo její vlastní API (``clientapi.oneticket.cz``) –
        OneTicket pak neukáže vůbec nic, ani chybovou hlášku.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                if await page.locator(", ".join(FROM_INPUT)).first.count():
                    return
            except Exception:
                pass
            if asyncio.get_running_loop().time() > deadline:
                break
            await page.wait_for_timeout(400)
        raise TransientProviderError(
            self.name,
            f"stránka OneTicketu se do {timeout:.0f} s nevykreslila – nejspíš "
            "nedostupné jejich API nebo pomalé připojení",
        )

    async def _first(self, page, selectors: list[str], what: str):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=2500):
                    return locator
            except Exception:
                continue
        raise ProviderError(
            self.name,
            f"prvek '{what}' nebyl na OneTicketu nalezen – aktualizujte "
            "selektory v providers/oneticket.py",
        )

    async def _fill_route(self, session, page, query: SearchQuery, api: "_ApiListener") -> None:
        for selectors, what, name in ((FROM_INPUT, "pole Odkud", query.origin),
                                      (TO_INPUT, "pole Kam", query.destination)):
            field = await self._first(page, selectors, what)
            await field.click()
            await field.fill("")
            await field.press_sequentially(name, delay=40)
            # Našeptávač se plní z /search/location. Když stanici nezná
            # (zahraničí), vrátí 404 – to víme hned a nemusíme čekat na
            # spojení, která nikdy nepřijdou.
            found = await api.wait_location(page, name)
            if found is False:
                raise StationNotFoundError(
                    self.name, f"OneTicket nezná stanici {name!r} – prodává jen jízdenky po Česku")
            await page.wait_for_timeout(250)
            await field.press("ArrowDown")
            await field.press("Enter")
            await page.wait_for_timeout(300)

    async def _fill_datetime(self, page, query: SearchQuery) -> None:
        field = await self._first(page, DATE_INPUT, "pole Datum")
        await field.click()
        await field.press("Control+A")
        await field.press("Backspace")
        await field.press_sequentially(
            f"{query.departure:%d.%m.%Y} {query.departure:%H:%M}", delay=45
        )
        await page.wait_for_timeout(350)
        await page.keyboard.press("Enter")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    async def _choose_fares(self, page, query: SearchQuery) -> None:
        """Vynuluje výchozí cestující a nastaví ty naše."""
        toggle = page.locator('button[data-testid="fare-toggle"]').first
        if not await toggle.count():
            return
        await toggle.click()
        await page.wait_for_timeout(800)

        # Nejdřív všechno na nulu – jinak se naši cestující přičtou k výchozím.
        items = await page.locator("div.fare-item").all()
        for item in items:
            minus = item.locator('button[data-testid="minus"]')
            value = item.locator('span[data-testid="value"]')
            for _ in range(6):
                try:
                    if (await value.inner_text()).strip() == "0":
                        break
                    if await minus.evaluate("node => node.disabled"):
                        break
                    await minus.click()
                    await page.wait_for_timeout(120)
                except Exception:
                    break

        wanted: dict[str, int] = {}
        for passenger in query.passengers:
            key = self._fare_key(passenger)
            wanted[key] = wanted.get(key, 0) + 1

        for key, count in wanted.items():
            label = FARE_LABELS.get(key)
            if not label:
                continue
            row = page.locator(f'div.fare-item:has(span:has-text("{label}"))').first
            if not await row.count():
                log.debug("%s: tarif %r na stránce není", self.name, label)
                continue
            plus = row.locator("button").last
            for _ in range(count):
                await plus.click()
                await page.wait_for_timeout(180)

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

    async def _submit(self, page, api: "_ApiListener | None" = None) -> None:
        button = await self._first(page, SUBMIT, "tlačítko Vyhledat")
        await button.click()
        if api is not None and await api.wait_search(page):
            return
        for selector in CONNECTION:
            try:
                await page.locator(selector).first.wait_for(state="visible", timeout=20000)
                # React dopisuje ceny o chvíli později než samotné spoje.
                await page.wait_for_timeout(2500)
                return
            except Exception:
                continue
        raise TransientProviderError(self.name, "OneTicket nevrátil žádné spojení")

    async def _parse(self, page, query: SearchQuery) -> list[Journey]:
        cards = None
        for selector in CONNECTION:
            locator = page.locator(selector)
            if await locator.count():
                cards = locator
                break
        if cards is None:
            return []

        # Základní cena bývá mimo kartu; slouží jako záloha.
        base_price = None
        base_locator = page.locator(".route-item .price").first
        if await base_locator.count():
            base_price = _parse_czk(await base_locator.inner_text())

        count = min(await cards.count(), int(self.options.get("max_connections", 12)))
        journeys: list[Journey] = []
        for index in range(count):
            try:
                data = await cards.nth(index).evaluate(
                    """(el) => {
                        const pick = (sel) => {
                            const node = el.querySelector(sel);
                            return node ? node.innerText.trim() : '';
                        };
                        return {
                            dep: pick('.connection-direction-from-decorator.starting span'),
                            arr: pick('.connection-direction-to-decorator.final span'),
                            train: pick('.train-line-name span'),
                            button: pick('.connection-item-tail button.btn-primary'),
                            all: el.innerText,
                        };
                    }"""
                )
            except Exception:
                continue

            journey = self._to_journey(data, base_price, query, index)
            if journey is not None:
                journeys.append(journey)
        return journeys

    # ---- JSON z /search -------------------------------------------------------
    def _from_api(self, api: "_ApiListener", query: SearchQuery) -> list[Journey]:
        paths = list((api.search or {}).get("paths") or [])
        limit = int(self.options.get("max_connections", 12))
        prices = api.route_prices(paths)
        tariff = FARE_LABELS.get(self._fare_key(query.lead), "")
        journeys: list[Journey] = []
        for path in paths[:limit]:
            journey = path_to_journey(path, self.name)
            if journey is None:
                continue
            amount = prices.get(route_key(path))
            if amount is not None:
                needs_reservation = any(leg.reservation_required for leg in journey.legs)
                journey.fares = [self.make_fare(
                    amount, "CZK",
                    label="OneTicket + místenka" if needs_reservation else "OneTicket",
                    tariff=tariff)]
                journey.fare_status = FareStatus.PRICED
            journeys.append(journey)
        return journeys

    def _to_journey(self, data, base_price, query: SearchQuery, index: int) -> Journey | None:
        times = _TIME_RE.findall(data.get("dep", "") + " " + data.get("arr", ""))
        if len(times) < 2:
            times = _TIME_RE.findall(data.get("all", ""))
        if len(times) < 2:
            return None

        departure = _combine(query.departure, times[0])
        arrival = _combine(query.departure, times[-1])
        if arrival < departure:
            arrival += dt.timedelta(days=1)

        button_text = (data.get("button") or "").replace("\xa0", " ")
        amount = _parse_czk(button_text) or base_price
        needs_reservation = "místenk" in button_text.lower()

        fares = []
        status = FareStatus.UNKNOWN
        if amount is not None:
            fares.append(
                self.make_fare(
                    amount, "CZK",
                    label="OneTicket + místenka" if needs_reservation else "OneTicket",
                    tariff=FARE_LABELS.get(self._fare_key(query.lead), ""),
                )
            )
            status = FareStatus.PRICED

        train = (data.get("train") or "").replace("\xa0", " ").strip() or "Vlak"
        leg = Leg(
            origin=query.origin,
            destination=query.destination,
            departure=departure,
            arrival=arrival,
            mode=Mode.TRAIN,
            carrier="OneTicket",
            line=train,
            stops=[Stop(name=query.origin, departure=departure),
                   Stop(name=query.destination, arrival=arrival)],
            reservation_required=needs_reservation,
        )
        return Journey(
            provider=self.name,
            legs=[leg],
            fares=fares,
            fare_status=status,
            journey_id=str(index),
            deep_link=SEARCH_URL,
        )


class _ApiListener:
    """Sbírá JSON odpovědi, které si aplikace OneTicketu stáhne sama."""

    def __init__(self) -> None:
        self.locations: dict[str, bool] = {}     # hledaný text → našel něco?
        self.search: dict[str, Any] | None = None
        self.prices: dict[str, Decimal] = {}     # pathId → cena
        self.price_responses = 0

    async def on_response(self, response) -> None:
        url = response.url
        if API_HOST not in url:
            return
        try:
            method = response.request.method
            if "/search/location" in url:
                from urllib.parse import parse_qs, urlparse

                term = (parse_qs(urlparse(url).query).get("search") or [""])[0]
                ok = False
                if response.status < 400:
                    data = await response.json()
                    ok = bool(isinstance(data, dict) and (data.get("cities") or data.get("stations"))
                              or (isinstance(data, list) and data))
                self.locations[term.strip().lower()] = ok
            elif url.split("?")[0].rstrip("/").endswith("/search") and method == "POST":
                self.search = (await response.json() or {}) if response.status < 400 else {"paths": []}
            elif "/price" in url and method == "POST":
                body = json.loads(response.request.post_data or "{}")
                data = await response.json() if response.status < 400 else {}
                amount = (data or {}).get("price")
                if body.get("pathId") and isinstance(amount, (int, float)) and amount >= 0:
                    self.prices[str(body["pathId"])] = Decimal(str(amount))
                self.price_responses += 1
        except Exception as exc:  # odposlech nesmí shodit hledání
            log.debug("OneTicket: odpověď %s nečitelná (%s)", url, exc)

    async def wait_location(self, page, term: str, timeout: float = 6.0) -> bool | None:
        """``True``/``False`` podle odpovědi našeptávače, ``None`` když nepřišla."""
        key = term.strip().lower()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if key in self.locations:
                return self.locations[key]
            await page.wait_for_timeout(150)
        return None

    async def wait_search(self, page, timeout: float = 25.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.search is not None:
                return True
            await page.wait_for_timeout(150)
        return False

    async def wait_prices(self, page, timeout: float = 8.0, quiet: float = 1.2) -> None:
        """Počká, až aplikace dotáhne ceny lístků (jeden dotaz na trasu)."""
        if not (self.search or {}).get("paths"):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen, quiet_since = -1, loop.time()
        while loop.time() < deadline:
            if self.price_responses != seen:
                seen, quiet_since = self.price_responses, loop.time()
            elif seen > 0 and loop.time() - quiet_since > quiet:
                return
            await page.wait_for_timeout(150)

    def route_prices(self, paths: list[dict[str, Any]]) -> dict[tuple, Decimal]:
        by_path = {str(p.get("pathId")): route_key(p) for p in paths}
        result: dict[tuple, Decimal] = {}
        for path_id, amount in self.prices.items():
            key = by_path.get(path_id)
            if key is not None:
                result.setdefault(key, amount)
        return result


def route_key(path: dict[str, Any]) -> tuple:
    """Lístek SJT platí pro trasu: stejné koncové stanice a stejné "přes"."""
    return (str(path.get("startStationId")), str(path.get("finishStationId")),
            tuple(str(v) for v in path.get("viaStationIds") or ()),
            path.get("distance"))


def _api_time(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DEFAULT_TZ)
    return parsed.astimezone(DEFAULT_TZ)


_COMPANY_SUFFIX = re.compile(r",?\s*(a\.\s*s\.|s\.\s*r\.\s*o\.|s\.\s*p\.)\s*$", re.IGNORECASE)


def _short_carrier(name: str) -> str:
    name = _COMPANY_SUFFIX.sub("", name or "").strip()
    return {"České dráhy": "ČD"}.get(name, name)


def _line_label(line: dict[str, Any]) -> str:
    """'rj 55' + 'Vindobona' → 'RJ 55 Vindobona'."""
    parts = str(line.get("name") or "").split()
    if parts and not parts[0].isdigit():
        parts[0] = parts[0].upper()
    label = " ".join(parts) or "Vlak"
    caption = str(line.get("nameCaption") or "").strip()
    return f"{label} {caption}" if caption else label


def path_to_journey(path: dict[str, Any], provider: str) -> Journey | None:
    """Jeden ``path`` z odpovědi /search → ``Journey`` se všemi vlaky."""
    trains = path.get("trains") or []
    legs: list[Leg] = []
    for train in trains:
        departure = _api_time(train.get("dateTime1"))
        arrival = _api_time(train.get("dateTime2"))
        if departure is None or arrival is None:
            continue
        items = train.get("items") or []
        used = [i for i in items if i.get("used")]
        stops = [Stop(name=str(i.get("stationName") or ""),
                      arrival=_api_time(i.get("arrTime")),
                      departure=_api_time(i.get("depTime"))) for i in used]
        # Položky jsou zastávky vlaku; ``used`` označuje ty na naší cestě
        # včetně nástupní a výstupní.
        if stops:
            stops[0].arrival = None
            stops[-1].departure = None
            stops[-1].arrival = stops[-1].arrival or arrival
        line = train.get("line") or {}
        mode = Mode.BUS if "bus" in str(line.get("categoryName") or "").lower() else Mode.TRAIN
        legs.append(Leg(
            origin=stops[0].name if stops else str(train.get("stationName") or ""),
            destination=stops[-1].name if stops else str(path.get("finishStationName") or ""),
            departure=departure,
            arrival=arrival,
            mode=mode,
            carrier=_short_carrier(str(line.get("carrierName") or "")) or "OneTicket",
            line=_line_label(line),
            stops=stops,
            reservation_required=str(line.get("hasReservation") or "") == "mandatory",
        ))
    if not legs:
        return None
    return Journey(provider=provider, legs=legs, journey_id=str(path.get("pathId") or ""),
                   deep_link=SEARCH_URL)


def _combine(base: dt.datetime, hhmm: tuple[str, str]) -> dt.datetime:
    hour, minute = int(hhmm[0]), int(hhmm[1])
    return base.replace(hour=min(hour, 23), minute=min(minute, 59),
                        second=0, microsecond=0, tzinfo=base.tzinfo or DEFAULT_TZ)


def _parse_czk(text: str) -> Decimal | None:
    """Vytáhne korunovou částku; zvládne 'Koupit za: 1 234 Kč'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    if re.search(r"vyprod|nedostup|not available", cleaned, re.IGNORECASE):
        return None
    match = _PRICE_RE.search(cleaned)
    if not match:
        return None
    raw = match.group(1).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:
        return None
