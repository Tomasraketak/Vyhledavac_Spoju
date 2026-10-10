import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac.models import DEFAULT_TZ, Mode, Passenger, SearchQuery
from vyhledavac.providers import mav

STATIONS = [
    ["Bp (BUDAPEST*)", "005510009", "HU", "1"],
    ["Budapest-Keleti", "005510017", "HU", "0"],
    ["Keleti (Budapest-Keleti)", "005510017", "HU", "1"],
    ["Győr", "005501289", "HU", "0"],
    ["Győr, autóbusz-állomás", "004257665", "HU", "0"],
    ["Praha hl. n.", "005457076", "CZ", "0"],
    ["Brno hl. n.", "005433295", "CZ", "0"],
    ["Wien Hbf", "008101003", "AT", "0"],
]
TYPES = [{"key": "109_000-004", "min": 0, "max": 3}, {"key": "30", "min": 6, "max": 11},
         {"key": "107_18-26", "min": 18, "max": 25}, {"key": "44", "min": 26, "max": 199},
         {"key": "KUTYA_105", "min": None, "max": None}]


def at(h, m=0):
    return dt.datetime(2026, 10, 12, h, m, tzinfo=DEFAULT_TZ)


def test_pick_station():
    assert mav.pick_station("Budapest", STATIONS)[1] == "005510009"       # všechna nádraží
    assert mav.pick_station("Gyor", STATIONS)[0] == "Győr"                 # vlak, ne autobus
    assert mav.pick_station("Praha hl.n.", STATIONS)[1] == "005457076"
    assert mav.pick_station("Brno", STATIONS)[0] == "Brno hl. n."
    assert mav.pick_station("Wien", STATIONS)[0] == "Wien Hbf"


def test_customer_type_by_age():
    assert mav.customer_type(Passenger(age=30), TYPES) == "44"
    assert mav.customer_type(Passenger(age=20), TYPES) == "107_18-26"
    assert mav.customer_type(Passenger(age=8), TYPES) == "30"
    assert mav.customer_type(Passenger(age=5), TYPES) is None


def test_offer_to_journey():
    raw = {"departure": {"time": "2026-10-12T06:37:00+02:00"}, "arrival": {"time": "2026-10-12T14:35:00+02:00"},
           "name": "53 - 61", "pricingFailed": False, "orderDisabled": False,
           "travelClasses": [{"name": "2", "price": {"amount": 228.8, "currency": {"name": "EUR", "uicCode": "EUR"}}}],
           "details": {"tickets": [{"name": "Multilaterális"}, {"name": "Multilaterális"}], "routes": [
               {"departure": {"time": "2026-10-12T06:37:00+02:00"}, "arrival": {"time": "2026-10-12T10:58:00+02:00"},
                "startStation": {"name": "Praha hl. n."}, "destionationStation": {"name": "Wien Meidling"},
                "trainDetails": {"trainKind": {"name": "railjet"}, "trainNumber": "53"}},
               {"departure": {"time": "2026-10-12T11:35:00+02:00"}, "arrival": {"time": "2026-10-12T14:35:00+02:00"},
                "startStation": {"name": "Wien Meidling"}, "destionationStation": {"name": "Budapest-Keleti"},
                "trainDetails": {"trainKind": {"name": "railjet xpress"}, "trainNumber": "61"}}]}}
    journey = mav.MavProvider().to_journey(raw)
    assert [(l.line, l.origin, l.destination) for l in journey.legs] == [
        ("rj 53", "Praha hl. n.", "Wien Meidling"), ("rjx 61", "Wien Meidling", "Budapest-Keleti")]
    assert journey.legs[0].mode is Mode.TRAIN and journey.departure == at(6, 37)
    fare = journey.fares[0]
    assert fare.amount == Decimal("228.8") and fare.currency == "EUR" and fare.tariff == "Multilaterális"


@pytest.mark.asyncio
async def test_skips_trips_without_hungary():
    from vyhledavac.errors import StationNotFoundError

    provider = mav.MavProvider()
    with pytest.raises(StationNotFoundError):        # bez jediného dotazu na MÁV
        await provider.search(SearchQuery(origin="Praha", destination="Wien", departure=at(8)))
