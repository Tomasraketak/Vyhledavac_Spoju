"""SNCF přes Virail: převod odpovědi (zkrácená skutečná odpověď Paříž → Lyon, říjen 2026)."""

import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import sncf

DATA = json.loads((Path(__file__).parent / "data" / "virail_paris_lyon.json").read_text("utf-8"))


def query(hour=6, **kwargs):
    return SearchQuery(origin="Paris", destination="Lyon",
                       departure=dt.datetime(2026, 10, 14, hour, tzinfo=DEFAULT_TZ), **kwargs)


def test_journeys_trains_with_prices():
    journeys = sncf.SncfProvider(cache=MemoryCache()).journeys(DATA, query(window=dt.timedelta(hours=18)))
    assert all(j.provider == "SNCF" for j in journeys)
    # auto (BlaBlaCar) a čistý autobus se zahodí
    assert all(any(leg.mode is Mode.TRAIN for leg in j.legs) for j in journeys)
    inoui = next(j for j in journeys if j.legs[0].line == "inOui" and j.departure.hour == 11)
    assert inoui.legs[0].origin == "Paris-Gare-de-Lyon"
    assert inoui.fare_status is FareStatus.PRICED and inoui.fares[0].amount == Decimal(67)
    assert inoui.fares[0].currency == "EUR" and "kombo" in inoui.fares[0].label
    assert inoui.deep_link.startswith("https://www.kombo.co/")
    assert inoui.notes
    ouigo = next(j for j in journeys if j.legs[0].line == "Ouigo")
    assert ouigo.fares[0].amount == Decimal(30)


def test_bus_feeder_is_bus_leg():
    journeys = sncf.SncfProvider(cache=MemoryCache()).journeys(DATA, query(window=dt.timedelta(hours=18)))
    lyria = next(j for j in journeys if j.legs[0].line == "Lyria")
    assert [leg.mode for leg in lyria.legs] == [Mode.TRAIN, Mode.BUS]
    assert lyria.fares[0].amount == Decimal(64)          # cena celé cesty, autobus 0 €


def test_window_filter():
    journeys = sncf.SncfProvider(cache=MemoryCache()).journeys(DATA, query(hour=19, window=dt.timedelta(hours=2)))
    assert {j.departure.hour for j in journeys} == {19}


def test_pax_params():
    assert sncf.pax_params((Passenger(age=30),)) == {"adult_pax": "1", "youth_pax": "_", "senior_pax": "_"}
    group = (Passenger(age=40), Passenger(age=8), Passenger(age=19), Passenger(age=67))
    assert sncf.pax_params(group) == {"adult_pax": "1", "youth_pax": "8.0,19.0", "senior_pax": "67.0"}
    # jen mladí – nejstarší z nich jako dospělý
    assert sncf.pax_params((Passenger(age=20), Passenger(age=10))) == \
        {"adult_pax": "1", "youth_pax": "10.0", "senior_pax": "_"}


def test_pick_city():
    items = [{"k": "a.1", "n": "Paris", "c": "FR", "stations": [1]},
             {"k": "a.2", "n": "Lyon", "c": "FR", "stations": [1, 2]},
             {"k": "a.3", "n": "Sainte-Foy-les-Lyon", "c": "FR", "stations": []}]
    assert sncf.pick_city("Lyon Part-Dieu", items)["k"] == "a.2"
    assert sncf.pick_city("Paris", items)["k"] == "a.1"


@pytest.mark.parametrize("origin,destination", [("Praha", "Brno"), ("Praha", "Wien")])
def test_not_france_without_network(origin, destination):
    provider = sncf.SncfProvider(cache=MemoryCache())
    q = SearchQuery(origin=origin, destination=destination,
                    departure=dt.datetime(2026, 10, 14, 8, tzinfo=DEFAULT_TZ))
    with pytest.raises(StationNotFoundError):
        asyncio.run(provider.search(q))


def test_group_price_only_indicative():
    q = query(window=dt.timedelta(hours=18), passengers=(Passenger(age=30), Passenger(age=8)))
    journeys = sncf.SncfProvider(cache=MemoryCache()).journeys(DATA, q)
    inoui = next(j for j in journeys if j.legs[0].line == "inOui" and j.departure.hour == 11)
    assert inoui.fare_status is FareStatus.UNKNOWN and not inoui.fares
    assert inoui.raw["indicative_eur"] == "67" and "skupin" in inoui.notes[0]
