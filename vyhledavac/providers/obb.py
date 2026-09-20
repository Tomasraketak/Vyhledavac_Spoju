"""ÖBB (Rakouské spolkové dráhy) přes HAFAS.

Původní verze spouštěla Firefox na ``shop.oebbtickets.at``, psala do
našeptávače znak po znaku a bojovala s tím, že prohlížeč na pozadí usíná.
ÖBB má ale veřejný HAFAS endpoint, který vrací jízdní řád včetně všech
mezizastávek – bez prohlížeče a bez antibot ochrany.

Pozor: tenhle endpoint vrací **jízdní řád, ne ceny**. Spoje proto mají
``fare_status = UNKNOWN`` a v UI se zobrazí jako "cena neznámá". Je to
poctivější než původní chování, kdy chybějící cena tiše zmizela ve
``float('inf')`` a spoj se tvářil jako nekonečně drahý.

Zároveň je to náš nejlepší zdroj **trasy vlaku** – i pro české vlaky –
takže z něj těží algoritmus lomených jízdenek.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from ..models import FareStatus, Journey, SearchQuery
from ..registry import PROVIDERS
from ._hafas import HafasClient, HafasProfile, parse_connections
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


@PROVIDERS.register()
class ObbProvider(HttpProvider):
    name = "ÖBB"
    label = "ÖBB – jízdní řád vč. mezizastávek (bez cen)"
    currency = "EUR"
    provides_prices = False
    provides_stops = True
    countries = ("AT", "CZ", "SK", "DE", "HU", "SI", "IT", "CH")
    min_interval = 0.6

    profile: ClassVar[HafasProfile] = OEBB_PROFILE

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._hafas: HafasClient | None = None

    @property
    def hafas(self) -> HafasClient:
        if self._hafas is None:
            self._hafas = HafasClient(self.name, self.profile, self.client, self.cache)
        return self._hafas

    async def search(self, query: SearchQuery) -> list[Journey]:
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


def _deep_link(query: SearchQuery) -> str:
    return (
        "https://fahrplan.oebb.at/webapp/?context=TP#!"
        f"P|TP!histKey|HISTORY|S|{query.origin}|Z|{query.destination}"
    )
