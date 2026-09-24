"""ÖBB (Rakouské spolkové dráhy): jízdní řád z HAFASu, ceny z e-shopu.

Původní verze spouštěla Firefox na ``shop.oebbtickets.at``, psala do
našeptávače znak po znaku a četla ceny z HTML. Občas to prošlo, často ne.

Teď jsou to dvě nezávislé části, které běží souběžně:

* **Jízdní řád** z veřejného HAFASu (``fahrplan.oebb.at``) – spolehlivý,
  bez ochrany, se všemi mezizastávkami. Na něm stojí i lomené jízdenky.
* **Ceny** z JSON API e-shopu s anonymním tokenem, který dostane každý
  návštěvník – žádný klíč (podrobnosti v ``_oebb_shop.py``). Když shop
  přímý dotaz zablokuje, zkusí se totéž zevnitř skutečného prohlížeče.

Ceny jsou nadstavba: když se nepodaří, spoje zůstanou (se stavem "cena
neznámá") a dopravce to nahlásí poznámkou, ne chybou. Jízdní řád tak
nikdy nespadne kvůli tomu, že shop zrovna nemá náladu.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, ClassVar

from ..browser import playwright_available
from ..errors import BlockedError, ProviderError, ProviderUnavailableError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery
from ..registry import PROVIDERS
from ._hafas import HafasClient, HafasProfile, parse_connections
from ._oebb_shop import SHOP_PAGE, BrowserTransport, HttpTransport, OebbShop, ShopOffer
from .base import HttpProvider

log = logging.getLogger(__name__)

OEBB_PROFILE = HafasProfile(
    endpoint="https://fahrplan.oebb.at/bin/mgate.exe",
    client={"type": "IPH", "id": "OEBB", "v": "6030600", "name": "oebbPROD-ADHOC"},
    auth={"type": "AID", "aid": "OWDL4fE4ixNiPBBm"},
    version="1.57",
    ext="OEBB.1",
    language="de",
)

# Bitová maska produktů: všechny druhy dopravy.
ALL_PRODUCTS = 1023
#: Stejný spoj v HAFASu a v shopu se může o chvilku lišit (zaokrouhlení).
MATCH_TOLERANCE = dt.timedelta(seconds=90)
#: Kolik času smí zabrat zjišťování cen. Musí zůstat pod časovým limitem
#: dopravce v enginu, jinak by se kvůli cenám zahodil i jízdní řád.
PRICE_BUDGET = 45.0


@PROVIDERS.register()
class ObbProvider(HttpProvider):
    name = "ÖBB"
    label = "ÖBB – jízdní řád vč. mezizastávek + ceny z e-shopu"
    currency = "EUR"
    provides_prices = True
    provides_stops = True
    countries = ("AT", "CZ", "SK", "DE", "HU", "SI", "IT", "CH")
    min_interval = 0.6

    profile: ClassVar[HafasProfile] = OEBB_PROFILE

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hafas: HafasClient | None = None
        self._browser_shop: OebbShop | None = None
        # Když shop jednou zablokuje přímé dotazy, v téže relaci už to
        # nezkoušíme znovu – rovnou jdeme přes prohlížeč.
        self._http_blocked = False

    @property
    def hafas(self) -> HafasClient:
        if self._hafas is None:
            self._hafas = HafasClient(self.name, self.profile, self.client, self.cache)
        return self._hafas

    async def search(self, query: SearchQuery) -> list[Journey]:
        prices_task = None
        if self.options.get("prices", True):
            prices_task = asyncio.create_task(self._shop_offers(query))
        try:
            journeys = await self._timetable(query)
        except ProviderError:
            # Jízdní řád selhal. Shop ale zná spoje taky – když odpoví,
            # vrátíme aspoň ty (bez mezizastávek, zato s cenou).
            if prices_task is None:
                raise
            try:
                offers = await asyncio.wait_for(prices_task, timeout=PRICE_BUDGET)
            except Exception:
                offers = []
            if not offers:
                raise
            self.note("jízdní řád ÖBB nedostupný – spoje jen z e-shopu, bez mezizastávek")
            return merge_offers([], offers, self, query)
        except BaseException:
            if prices_task is not None:
                prices_task.cancel()
            raise

        offers: list[ShopOffer] = []
        if prices_task is not None:
            try:
                offers = await asyncio.wait_for(prices_task, timeout=PRICE_BUDGET)
            except asyncio.TimeoutError:
                self.note("ceny z e-shopu ÖBB nedorazily včas – zobrazen jen jízdní řád")
            except BlockedError:
                self.note(_blocked_note())
            except ProviderUnavailableError as exc:
                self.note(f"ceny ÖBB nelze zjistit: {exc.message}")
            except ProviderError as exc:
                self.note(f"ceny ÖBB se nepodařilo zjistit ({exc.message})")

        return merge_offers(journeys, offers, self, query)

    async def _timetable(self, query: SearchQuery) -> list[Journey]:
        origin, destination = await asyncio.gather(
            self.hafas.find_location(query.origin),
            self.hafas.find_location(query.destination),
        )
        res = await self.hafas.trip_search(
            origin,
            destination,
            query.departure,
            count=int(self.options.get("count", 6)),
            passlist=True,
            products=ALL_PRODUCTS,
        )
        journeys = parse_connections(res, self.name)
        for journey in journeys:
            journey.fare_status = FareStatus.UNKNOWN
            journey.deep_link = _deep_link(query)
        return journeys

    async def _shop_offers(self, query: SearchQuery) -> list[ShopOffer]:
        """Ceny ze shopu: nejdřív přímo, při blokaci přes prohlížeč."""
        pages = int(self.options.get("price_pages", 2))
        if not self._http_blocked:
            shop = OebbShop(self.name, HttpTransport(self.client))
            try:
                return await shop.search(query.origin, query.destination, query.departure,
                                         pages=pages)
            except BlockedError:
                self._http_blocked = True
                log.info("ÖBB: shop blokuje přímé dotazy, zkouším prohlížeč")
                if not self._browser_allowed():
                    raise

        if not self._browser_allowed():
            raise BlockedError(self.name, "shop ÖBB blokuje přímé dotazy")
        if self._browser_shop is None:
            self._browser_shop = OebbShop(self.name, BrowserTransport(
                self.name,
                headless=self.options.get("headless", True),
                engine=self.options.get("engine", "chromium"),
            ))
        return await self._browser_shop.search(query.origin, query.destination,
                                               query.departure, pages=pages)

    def _browser_allowed(self) -> bool:
        return bool(self.options.get("browser_fallback", True)) and playwright_available()

    async def aclose(self) -> None:
        if self._browser_shop is not None:
            await self._browser_shop.close()
            self._browser_shop = None
        await super().aclose()


def merge_offers(journeys: list[Journey], offers: list[ShopOffer], provider,
                 query: SearchQuery) -> list[Journey]:
    """Přiřadí ceny ze shopu ke spojům z HAFASu.

    Oba zdroje jsou data ÖBB, takže stačí shoda odjezdu i příjezdu. Spoje,
    které zná jen shop (HAFAS vrací užší okno), se přidají zvlášť – bez
    mezizastávek, ale s cenou, a o tu tu jde.
    """
    used: set[str] = set()
    for journey in journeys:
        match = next(
            (o for o in offers
             if o.connection_id not in used
             and abs(o.departure - journey.departure) <= MATCH_TOLERANCE
             and abs(o.arrival - journey.arrival) <= MATCH_TOLERANCE),
            None,
        )
        if match is None:
            continue
        used.add(match.connection_id)
        _price_journey(journey, match, provider)

    for offer in offers:
        if offer.connection_id in used or not offer.sections:
            continue
        journey = Journey(
            provider=provider.name,
            legs=[
                Leg(origin=s.origin, destination=s.destination, departure=s.departure,
                    arrival=s.arrival, mode=Mode.TRAIN if s.is_train else Mode.BUS,
                    carrier="ÖBB", line=s.line)
                for s in offer.sections
            ],
            journey_id=offer.connection_id,
            deep_link=SHOP_PAGE,
        )
        _price_journey(journey, offer, provider)
        journeys.append(journey)
    return journeys


def _price_journey(journey: Journey, offer: ShopOffer, provider) -> None:
    if offer.price is not None:
        journey.fares = [provider.make_fare(offer.price, "EUR", label="od",
                                            tariff="dospělý bez slevy")]
        if offer.first_class is not None:
            journey.fares.append(provider.make_fare(offer.first_class, "EUR",
                                                    label="1. třída", tariff="dospělý bez slevy"))
        journey.fare_status = FareStatus.PRICED
        journey.deep_link = SHOP_PAGE
    elif offer.sold_out:
        journey.fare_status = FareStatus.SOLD_OUT
    elif offer.has_offer:
        journey.fare_status = FareStatus.NOT_BOOKABLE


def _blocked_note() -> str:
    if not playwright_available():
        return ("e-shop ÖBB odmítl dotaz na ceny – zobrazen jen jízdní řád. "
                "Pomoct může Playwright (pip install playwright && "
                "playwright install chromium), pak se ceny zkusí přes prohlížeč.")
    return ("e-shop ÖBB odmítl dotaz na ceny i přes prohlížeč – zobrazen jen "
            "jízdní řád. Obvykle to je dočasné; z datacentra či VPN trvale.")


def _deep_link(query: SearchQuery) -> str:
    return (
        "https://fahrplan.oebb.at/webapp/?context=TP#!"
        f"P|TP!histKey|HISTORY|S|{query.origin}|Z|{query.destination}"
    )
