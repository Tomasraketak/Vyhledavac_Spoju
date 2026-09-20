import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vyhledavac.models import (  # noqa: E402
    DEFAULT_TZ, Fare, FareStatus, Journey, Leg, Mode, SearchQuery, Stop,
)


def at(hour, minute=0, day=1):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=DEFAULT_TZ)


def make_journey(
    provider="Test",
    dep=None,
    arr=None,
    price=None,
    line="EC 100",
    origin="Praha",
    destination="Vídeň",
    stops=None,
    mode=Mode.TRAIN,
    reservation_required=False,
):
    dep = dep or at(8)
    arr = arr or at(12)
    stop_list = [Stop(name=n, arrival=arr if i else None, departure=dep if not i else None)
                 for i, n in enumerate(stops or [origin, destination])]
    leg = Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
              mode=mode, carrier=provider, line=line, stops=stop_list,
              reservation_required=reservation_required)
    fares = []
    status = FareStatus.UNKNOWN
    if price is not None:
        amount = Decimal(str(price))
        fares = [Fare(amount=amount, currency="CZK", amount_czk=amount)]
        status = FareStatus.PRICED
    return Journey(provider=provider, legs=[leg], fares=fares, fare_status=status)


@pytest.fixture
def query():
    return SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))


@pytest.fixture
def journey_factory():
    return make_journey


@pytest.fixture
def clock():
    return at
