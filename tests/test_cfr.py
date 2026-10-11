import datetime as dt
import gzip
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import cfr

DATA = Path(__file__).parent / "data"
DAY = dt.date(2026, 10, 14)


def _data():
    return json.load(gzip.open(DATA / "cfr.json.gz", "rt", encoding="utf-8"))


def test_stations_from_page_and_matching():
    names = cfr.parse_stations(_data()["stations_js"])
    assert len(names) > 1500 and "București Nord" in names
    assert cfr.pick_station("Bukurešť", names) == "București (toate stațiile)"
    assert cfr.pick_station("Bucuresti Nord", names) == "București Nord"
    assert cfr.pick_station("Brasov", names) == "Brașov"
    assert cfr.pick_station("Cluj", names) == "Cluj Napoca"
    assert cfr.pick_station("Timisoara", names) == "Timișoara Nord"
    assert cfr.pick_station("Budapest", names) is None
    assert cfr.slug("București (toate stațiile)") == "Bucuresti-(toate-statiile)"
    assert cfr.slug("Timișoara Nord") == "Timisoara-Nord"


def test_hidden_search_form():
    form = cfr.parse_hidden_form(_data()["route_page_form"], "form-search")
    assert form["DepartureStationName"] == "Timisoara-Nord" and form["ArrivalStationName"] == "Iasi"
    assert form["ConfirmationKey"] and form["__RequestVerificationToken"]


def test_itineraries_with_changes_and_stops():
    items = cfr.parse_itineraries(_data()["itineraries"], DAY)
    assert len(items) == 3
    direct = items[1]["legs"]
    assert [l.line for l in direct] == ["IR 1831"]
    assert direct[0].departure == dt.datetime(2026, 10, 14, 5, 45, tzinfo=DEFAULT_TZ)   # 6:45 v Rumunsku
    assert direct[0].mode is Mode.TRAIN and len(direct[0].stops) > 10
    three = items[2]["legs"]
    assert [l.line for l in three] == ["IR 16099", "IRN 473", "IR 12543"]
    assert [l.origin for l in three] == ["Timișoara Nord", "Arad", "Brașov"] and three[-1].destination == "Iași"
    assert three[1].departure.date() == dt.date(2026, 10, 15)            # přes půlnoc
    assert any("IR 12543" in n for n in items[2]["notes"])               # náhradní bus 21.09–02.11
    assert not any("IR 16099" in n for n in items[2]["notes"])           # výluka až v listopadu
    form = items[1]["form"]
    assert form["TransactionString"] and form["TrainServiceKeys[0]"] == "B&B"


def test_price_and_fare_types():
    assert cfr.parse_price(_data()["price_adult"]) == Decimal("197.0")
    assert cfr.parse_price("<strong>1.234,50 lei</strong>") == Decimal("1234.50")
    assert [cfr.fare_type(a) for a in (2, 5, 9, 10, 40)] == [None, "101", "101", "102", "102"]


def test_journey_from_itinerary():
    provider = cfr.CfrProvider()
    item = cfr.parse_itineraries(_data()["itineraries"], DAY)[2]
    journey = provider.to_journey(item, "https://bilete.cfrcalatori.ro/ro-RO/Rute-trenuri/Timisoara-Nord/Iasi",
                                  "14.10.2026")
    assert journey.transfers == 2 and journey.fare_status is FareStatus.UNKNOWN
    assert journey.notes and journey.deep_link.endswith("DepartureDate=14.10.2026")


@pytest.mark.asyncio
async def test_skips_routes_outside_romania():
    provider = cfr.CfrProvider()
    query = SearchQuery(origin="Praha", destination="Wien",
                        departure=dt.datetime(2026, 10, 14, 8, tzinfo=DEFAULT_TZ), passengers=(Passenger(),))
    with pytest.raises(StationNotFoundError):
        await provider.search(query)
    assert provider._client is None


def _online(host: str) -> bool:
    try:
        socket.create_connection((host, 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_live_search():
    if not _online("bilete.cfrcalatori.ro"):
        pytest.skip("bez sítě")
    provider = cfr.CfrProvider()
    try:
        start = dt.datetime.now(DEFAULT_TZ).replace(hour=6, minute=0, second=0, microsecond=0) + dt.timedelta(days=3)
        journeys = await provider.search(SearchQuery(origin="București Nord", destination="Brașov",
                                                     departure=start, window=dt.timedelta(hours=4)))
    finally:
        await provider.aclose()
    assert journeys and any(j.price is not None for j in journeys)
