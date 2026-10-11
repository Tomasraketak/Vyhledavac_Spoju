"""SNCF přes Kombo: převod odpovědi (zkrácená skutečná odpověď Paříž → Lyon,
dospělý + dítě 8 let, říjen 2026)."""

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

DATA = json.loads((Path(__file__).parent / "data" / "kombo_paris_lyon.json").read_text("utf-8"))


def query(hour=0, window=dt.timedelta(hours=24), **kwargs):
    return SearchQuery(origin="Paris", destination="Lyon", window=window,
                       departure=dt.datetime(2026, 10, 14, hour, tzinfo=DEFAULT_TZ), **kwargs)


def journeys(q=None):
    return sncf.SncfProvider(cache=MemoryCache()).journeys(DATA, q or query())


def test_trains_with_group_price():
    found = journeys()
    assert found and all(any(leg.mode is Mode.TRAIN for leg in j.legs) for j in found)
    tgv = next(j for j in found if j.legs[0].line == "TGV INOUI 6609")
    assert tgv.departure.hour == 10 and tgv.legs[0].carrier == "inOui"
    assert tgv.fare_status is FareStatus.PRICED
    assert tgv.fares[0].amount == Decimal("118.50") and tgv.fares[0].currency == "EUR"
    assert "143.50" in tgv.notes[0]                     # cena u Komba s poplatkem
    assert tgv.deep_link.startswith("https://www.kombo.co")


def test_buses_only_dropped_and_feeder_bus_kept():
    found = journeys()
    assert not any(j.legs[0].carrier in ("FlixBus", "BlaBlaCar Bus", "Itabus", "Gipsyy") for j in found)
    lyria = next(j for j in found if j.legs[0].carrier == "Lyria")
    assert [leg.mode for leg in lyria.legs] == [Mode.TRAIN, Mode.BUS]
    assert lyria.legs[0].line == "Lyria 9773" and lyria.legs[1].line == "TER 30115"


def test_window_filter():
    found = journeys(query(hour=10, window=dt.timedelta(hours=1)))
    assert [j.legs[0].line for j in found] == ["TGV INOUI 6609"]


def test_passengers():
    group = (Passenger(age=40), Passenger(age=8), Passenger(age=19), Passenger(age=67), Passenger(age=2))
    assert [p["ageGroup"] for p in sncf.passengers(group)] == ["Adult", "Child", "Young", "Senior", "Infant"]
    assert sncf.passengers(group)[-1]["onLap"] is True
    assert sncf.passengers((Passenger(age=10),))[0]["ageGroup"] == "Adult"


def test_pick_city():
    items = [{"i": 18, "n": "Lyon", "c": "France"}, {"i": 1, "n": "Paris", "c": "France"}]
    assert sncf.pick_city("Lyon Part-Dieu", items)["i"] == 18
    assert sncf.pick_city("Paris", items)["i"] == 1


def test_train_name():
    assert sncf.train_name("TER 17777 operated by SNCF VOYAGEURS 1187", "TER") == "TER 17777"
    assert sncf.train_name("OUIGO N° 7867", "Ouigo") == "OUIGO 7867"
    assert sncf.train_name("9773", "Lyria") == "Lyria 9773"


@pytest.mark.parametrize("origin,destination", [("Praha", "Brno"), ("Praha", "Wien")])
def test_not_france_without_network(origin, destination):
    provider = sncf.SncfProvider(cache=MemoryCache())
    q = SearchQuery(origin=origin, destination=destination,
                    departure=dt.datetime(2026, 10, 14, 8, tzinfo=DEFAULT_TZ))
    with pytest.raises(StationNotFoundError):
        asyncio.run(provider.search(q))
