"""Transitous – všechna spojení jako v IDOS (vlaky, autobusy, MHD), bez cen.

Jízdní řád z otevřeného plánovače Transitous nad daty CIS JŘ (viz
``vyhledavac/transitous.py``). Hodí se, když IDOS nejde nebo když chcete
vidět i spoje, ke kterým žádný e-shop cenu nedá. Dopravce u úseku je
skutečný provozovatel (``agencyName``: „ČSAD AUTOBUSY České Budějovice a.s.“,
„České dráhy, a.s.“).

Ve výchozím výběru není – zapněte ho v panelu *Zdroje dat* nebo přes
``--providers Transitous``. IDS ho jako zálohu použije sám, když IDOS
odmítne odpovídat.
"""

from __future__ import annotations

from .. import idos, transitous
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider


@PROVIDERS.register()
class TransitousProvider(HttpProvider):
    name = "Transitous"
    label = "Transitous – všechny spoje jako v IDOS (otevřená data CIS JŘ), bez cen"
    provides_prices = False
    provides_stops = True
    currency = "CZK"
    countries = ("CZ", "SK", "AT", "DE", "PL")
    czech_names = True
    hack_legs = False
    referer = None
    min_interval = 0.5

    async def search(self, query: SearchQuery) -> list[Journey]:
        connections = await transitous.search(self.client, self.cache, self.name, query.origin,
                                              query.destination, query.departure, query.window_end)
        return [to_journey(conn) for conn in connections]


def to_journey(conn: idos.IdosConnection) -> Journey:
    legs = []
    for leg in conn.legs:
        stops = []
        last = len(leg.inline_stops) - 1
        prev = leg.departure
        for i, (name, arr, dep) in enumerate(leg.inline_stops):
            if i == 0:
                stops.append(Stop(name=idos.display_name(name), departure=leg.departure))
                continue
            if i == last:
                stops.append(Stop(name=idos.display_name(name), arrival=leg.arrival))
                continue
            arrival = idos._at(leg.departure.date(), arr, prev)
            departure = idos._at(leg.departure.date(), dep, arrival or prev)
            prev = departure or arrival or prev
            stops.append(Stop(name=idos.display_name(name), arrival=arrival, departure=departure))
        mode = Mode.TRAM if leg.is_city else Mode.BUS if leg.is_bus else Mode.TRAIN
        legs.append(Leg(origin=idos.display_name(leg.origin), destination=idos.display_name(leg.destination),
                        departure=leg.departure, arrival=leg.arrival, mode=mode, carrier=leg.carrier,
                        line=leg.train_label, stops=stops))
    return Journey(provider="Transitous", legs=legs, fare_status=FareStatus.UNKNOWN,
                   deep_link="https://transitous.org/")
