import datetime as dt
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.cache import Cache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import eurostar

DATA = Path(__file__).parent / "data"


def _data():
    return json.load(gzip.open(DATA / "eurostar_search.json.gz", "rt", encoding="utf-8"))


def _journeys(key):
    return _data()[key]["data"]["journeySearch"]["outbound"]["journeys"]


def _stations():
    return _data()["stations"]["data"]["stations"]


def at(h, m=0, day=14):
    return dt.datetime(2026, 10, day, h, m, tzinfo=DEFAULT_TZ)


def test_group_price_and_local_times():
    # Londýn 06:01 (BST) → Paříž 09:26 (SELČ), 2 dospělí + dítě 8 let: 2×207 + 144 = 558 €
    journey = eurostar.EurostarProvider().to_journey(_journeys("london_paris_2a1c")[0])
    leg = journey.legs[0]
    assert leg.line == "Eurostar 9080" and leg.mode is Mode.TRAIN and leg.reservation_required
    assert journey.departure == at(7, 1) and journey.arrival == at(9, 26)
    assert journey.fare_status is FareStatus.PRICED
    assert sorted(f.amount for f in journey.fares) == [Decimal(550), Decimal(558), Decimal(1200)]
    assert {f.tariff for f in journey.fares} == {"Eurostar Standard", "Eurostar Plus", "Eurostar Premier"}


def test_sold_out_class_and_journey():
    journeys = _journeys("brussels_amsterdam_5pax")
    provider = eurostar.EurostarProvider()
    partly = provider.to_journey(_journeys("london_paris_2a1c")[2])   # Plus vyprodaná
    assert len(partly.fares) == 2
    sold = provider.to_journey(journeys[2])
    assert sold.fare_status is FareStatus.SOLD_OUT and not sold.fares and sold.price is None
    assert sold.legs[0].line == "Eurostar 9309"


def test_connection_and_summated_fare():
    provider = eurostar.EurostarProvider()
    koln = provider.to_journey(_journeys("london_koln")[0])
    assert [l.line for l in koln.legs] == ["Eurostar 9106", "Eurostar 9411"]
    assert koln.legs[0].destination == "Brussels Midi / Zuid" and koln.transfers == 1
    lyon = provider.to_journey(_journeys("london_lyon")[0])
    assert lyon.legs[1].carrier == "SNCF" and lyon.legs[1].line == "TGV INOUI 6611"
    assert not lyon.legs[1].reservation_required
    assert "součet" in lyon.cheapest_fare.tariff


def test_travellers_by_age():
    out = eurostar.travellers((Passenger(age=2), Passenger(age=8), Passenger(age=15), Passenger(age=40),
                               Passenger(age=70)))
    assert (out["infant"], out["child"], out["youth"], out["adult"], out["senior"]) == (1, 1, 1, 1, 1)
    assert out["childAges"] == [8] and out["youthAges"] == [15] and out["seniorsAges"] == [70]
    assert eurostar.travellers((Passenger(age=1),))["adult"] == 1


def test_pick_station_by_city_and_czech_name():
    stations = _stations()
    assert eurostar.pick_station("Londýn", stations)["uic"] == "7015400"
    assert eurostar.pick_station("Paris", stations)["uic"] == "8727100"          # Gare du Nord, ne Est
    assert eurostar.pick_station("Brusel", stations)["uic"] == "8814001"
    assert eurostar.pick_station("Kolín nad Rýnem", stations)["uic"] == "8015458"
    assert eurostar.pick_station("Amsterdam", stations)["uic"] == "8400058"
    assert eurostar.pick_station("Lyon Part-Dieu", stations)["uic"] == "8772319"


@pytest.mark.asyncio
async def test_search_filters_window_and_sends_group(tmp_path):
    provider = eurostar.EurostarProvider(cache=Cache(tmp_path / "cache.sqlite"))
    calls = []

    async def fake_gql(operation, query, variables):
        calls.append((operation, variables))
        if operation == "getStations":
            return _data()["stations"]["data"]
        return _data()["london_paris_2a1c"]["data"]

    provider._gql = fake_gql
    query = SearchQuery(origin="Londýn", destination="Paříž", departure=at(8),
                        passengers=(Passenger(age=35), Passenger(age=33), Passenger(age=8)))
    journeys = await provider.search(query)
    search = [v for op, v in calls if op == "NewBookingSearch"]
    assert search[0]["origin"] == "7015400" and search[0]["destination"] == "8727100"
    assert search[0]["adult"] == 2 and search[0]["child"] == 1 and search[0]["childAges"] == [8]
    assert len(search) == 1                                       # okno 8–20 h nepřesahuje půlnoc
    # 06:01 z Londýna = 07:01 u nás – před oknem; zbytek ano
    assert [j.departure for j in journeys] == [at(8, 4), at(9, 1), at(10, 31)]
    assert "adult=2" in journeys[0].deep_link and "outbound=2026-10-14" in journeys[0].deep_link


@pytest.mark.asyncio
async def test_skips_routes_outside_network_without_network():
    provider = eurostar.EurostarProvider()

    async def boom(*args, **kwargs):  # pragma: no cover - nesmí se volat
        raise AssertionError("síť se nemá volat")

    provider._gql = boom
    for origin, destination in [("Praha", "Wien"), ("Praha", "Paris"), ("München", "Paris"),
                                ("Roma", "Milano")]:
        with pytest.raises(StationNotFoundError):
            await provider.search(SearchQuery(origin=origin, destination=destination, departure=at(8)))
