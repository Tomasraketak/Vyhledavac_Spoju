"""Převod odpovědi RegioJetu na ``Journey``.

Data jsou zkrácené skutečné odpovědi ``/routes/search/simple`` (září 2026).
"""

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.models import FareStatus
from vyhledavac.providers.regiojet import RegioJetProvider

from conftest import at


def route(**overrides):
    base = {
        "id": 1,
        "departureTime": "2026-10-01T19:00:00.000+02:00",
        "arrivalTime": "2026-10-01T21:25:00.000+02:00",
        "vehicleTypes": ["BUS"],
        "transfersCount": 0,
        "priceFrom": 329,
        "priceTo": 329,
        "pricesCount": 1,
        "bookable": True,
        "freeSeatsCount": 20,
    }
    base.update(overrides)
    return base


@pytest.fixture
def provider():
    return RegioJetProvider(cache=MemoryCache())


@pytest.fixture
def q():
    from vyhledavac.models import SearchQuery
    return SearchQuery(origin="Praha", destination="Brno", departure=at(8))


def test_normal_route_has_price(provider, q):
    journey = provider._to_journey(route(), q, None)
    assert journey.price == 329
    assert journey.fare_status is FareStatus.PRICED


def test_sold_out_zero_is_not_a_free_ticket(provider, q):
    # Přesně tohle RegioJet vrací u vyprodaného autobusu. Nula není cena –
    # dřív se z ní stal "nejlevnější spoj za 0 Kč".
    sold_out = route(priceFrom=0, priceTo=0, pricesCount=0, bookable=False, freeSeatsCount=0)
    journey = provider._to_journey(sold_out, q, None)
    assert journey.price is None
    assert journey.fare_status is FareStatus.SOLD_OUT
    assert journey.price_label == "Vyprodáno"


def test_unbookable_with_seats_is_not_bookable(provider, q):
    closed = route(priceFrom=0, pricesCount=0, bookable=False, freeSeatsCount=5)
    journey = provider._to_journey(closed, q, None)
    assert journey.price is None
    assert journey.fare_status is FareStatus.NOT_BOOKABLE
