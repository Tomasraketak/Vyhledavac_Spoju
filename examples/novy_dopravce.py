"""Ukázka: jak přidat dalšího dopravce.

Stačí jeden soubor. Nikde se neupravuje seznam ani ``if`` – registr si
třídu najde sám. Spuštění::

    VYHLEDAVAC_PLUGINS=examples.novy_dopravce python -m vyhledavac Praha Brno \
        --providers UkazkovyDopravce
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from vyhledavac.models import Journey, Leg, Mode, SearchQuery, Stop
from vyhledavac.providers.base import HttpProvider
from vyhledavac.registry import PROVIDERS


@PROVIDERS.register()
class UkazkovyDopravce(HttpProvider):
    """Vymyšlený dopravce, který vždy vrátí jeden spoj.

    Skutečný dopravce by v ``search`` zavolal API přes ``self.client``::

        data = await self.client.get_json(
            "https://api.dopravce.cz/spojeni",
            params={"from": query.origin, "to": query.destination},
        )

    O opakování při chybě, detekci blokace, hlavičky i limity se postará
    ``HttpClient``; o izolaci chyb ``Provider.run``.
    """

    name = "UkazkovyDopravce"
    label = "Ukázka pro vlastní integraci"
    currency = "CZK"
    provides_prices = True
    provides_stops = True
    countries = ("CZ",)

    async def search(self, query: SearchQuery) -> list[Journey]:
        departure = query.departure.replace(minute=0, second=0, microsecond=0)
        arrival = departure + dt.timedelta(hours=2, minutes=30)

        leg = Leg(
            origin=query.origin,
            destination=query.destination,
            departure=departure,
            arrival=arrival,
            mode=Mode.TRAIN,
            carrier=self.name,
            line="UD 1",
            stops=[
                Stop(name=query.origin, departure=departure),
                Stop(name="Kolín", arrival=departure + dt.timedelta(minutes=50),
                     departure=departure + dt.timedelta(minutes=52)),
                Stop(name=query.destination, arrival=arrival),
            ],
        )
        return [
            Journey(
                provider=self.name,
                legs=[leg],
                # ``make_fare`` doplní přepočet na CZK podle kurzu ČNB.
                fares=[self.make_fare(Decimal("249"), "CZK", label="základní")],
            )
        ]
