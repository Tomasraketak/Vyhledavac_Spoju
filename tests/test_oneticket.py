"""OneTicket – čtení spojů a cen z JSON odpovědí, které si stáhne web.

``data/oneticket_search.json`` je zkrácená skutečná odpověď ``/search``
(Praha → Brno, říjen 2026).
"""

import json
from decimal import Decimal
from pathlib import Path

from vyhledavac.cache import MemoryCache
from vyhledavac.models import FareStatus, Mode, ProviderResult, SearchQuery
from vyhledavac.providers.oneticket import (
    OneTicketProvider,
    _ApiListener,
    path_to_journey,
    route_key,
)

from conftest import at

SEARCH = json.loads((Path(__file__).parent / "data" / "oneticket_search.json")
                    .read_text(encoding="utf-8"))


class _Converter:
    def to_czk(self, amount, currency):
        return amount


def test_path_becomes_journey_with_stops_and_carrier():
    journey = path_to_journey(SEARCH["paths"][1], "OneTicket")
    leg = journey.legs[0]
    assert (journey.departure.strftime("%H:%M"), journey.arrival.strftime("%H:%M")) == ("08:37", "11:13")
    assert leg.line == "RJ 55 Vindobona"
    assert leg.carrier == "ČD"
    assert leg.mode is Mode.TRAIN
    assert leg.stops[0].name == "Praha hl.n." and leg.stops[-1].name == "Brno hl.n."
    assert any(s.name == "Pardubice hl.n." for s in leg.stops)
    assert leg.stops[-1].arrival == journey.arrival


def test_ticket_price_applies_to_every_connection_on_the_route():
    listener = _ApiListener()
    listener.search = SEARCH
    # Web zjistil cenu jen pro jeden spoj trasy přes Pardubice ...
    listener.prices = {SEARCH["paths"][1]["pathId"]: Decimal("537")}
    provider = OneTicketProvider(cache=MemoryCache(), converter=_Converter())
    query = SearchQuery(origin="Praha", destination="Brno", departure=at(8))

    journeys = provider._from_api(listener, query)
    by_dep = {j.departure.strftime("%H:%M"): j for j in journeys}
    # ... a platí i pro další spoj po stejné trase,
    assert route_key(SEARCH["paths"][1]) == route_key(SEARCH["paths"][2])
    assert by_dep["09:01"].price == Decimal("537")
    # ale ne pro spoj přes Havlíčkův Brod (jiný lístek).
    assert by_dep["08:04"].price is None
    assert by_dep["08:04"].fare_status is FareStatus.UNKNOWN


def test_station_not_found_is_marked_as_not_served():
    result = ProviderResult(provider="OneTicket", error="nezná stanici", not_served=True)
    assert not result.ok and result.not_served
