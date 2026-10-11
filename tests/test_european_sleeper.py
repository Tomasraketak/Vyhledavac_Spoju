import datetime as dt
import gzip
import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import european_sleeper as es

DATA = Path(__file__).parent / "data"


def _data():
    return json.load(gzip.open(DATA / "european_sleeper.json.gz", "rt", encoding="utf-8"))


def at(day, h, m=0):
    return dt.datetime(2026, 10, day, h, m, tzinfo=DEFAULT_TZ)


def _query(origin="Praha", destination="Amsterdam", ages=(35, 33, 8), when=None):
    return SearchQuery(origin=origin, destination=destination, departure=when or at(13, 12),
                       passengers=tuple(Passenger(age=a) for a in ages))


def test_stations_and_routes():
    assert es.pick_station("Praha") == es.pick_station("Praha hl.n.") == "5457076"
    assert es.pick_station("Amsterdam Centraal") == "8400058"
    assert es.pick_station("Brusel") == es.pick_station("Bruxelles-Midi") == "8800104"
    assert es.pick_station("Drážďany") == "8001305"
    assert es.pick_station("Praha-Holešovice") is None and es.pick_station("Kolín") is None
    assert es.same_route("5457076", "8400058")                  # Praha – Amsterdam
    assert es.same_route("8400058", "5457076")                  # i zpět
    assert es.same_route("8010100", "8001305")                  # Berlín – Drážďany (DE vnitrostátně smí)
    assert not es.same_route("5457076", "5455659")              # Praha – Děčín se neprodává
    assert not es.same_route("5457076", "8300112")              # Praha – Milán není jedna trasa


def test_passenger_types_by_age():
    assert es.passenger_types(_query(ages=(35, 11, 4, 3))) == [72, 73, 73]
    assert es.passenger_types(_query(ages=(12,))) == [72]
    assert es.passenger_types(_query(ages=(30,) * 6)) == []     # víc než 5 online nekoupíte


def test_trips_only_running_days():
    trips = es.parse_trips(_data()["trains"])
    assert [t["departureTime"] for t in trips] == ["2026-10-13T18:00:00", "2026-10-15T18:00:00"]
    assert trips[0]["trainNumber"] == "452" and trips[0]["routeId"]


def test_journey_with_group_price():
    data = _data()
    provider = es.EuropeanSleeperProvider()
    query = _query()
    trip = es.parse_trips(data["trains"])[0]
    journey = es.to_journey(provider.name, trip, query, [72, 72, 73])
    leg = journey.legs[0]
    assert leg.line == "ES 452" and leg.mode is Mode.TRAIN and leg.origin == "Praha hl. n."
    assert journey.departure == at(13, 18) and journey.arrival == at(14, 7, 52)
    assert "passengerTypes=73" in journey.deep_link
    es.apply_offers(provider, journey, data["availability"])
    assert journey.fare_status is FareStatus.PRICED
    best = journey.cheapest_fare
    # 2 dospělí + dítě v lehátku Easy Night – cena za celou skupinu
    assert best.amount == Decimal("439.97") and best.currency == "EUR"
    assert "lehátko" in best.tariff and "Easy Night" in best.tariff
    assert not any("ženy" in f.tariff for f in journey.fares)
    assert any(o["class"] == "couchette-5-women-only" for o in journey.raw["offers"])


def test_private_compartment_for_bigger_group():
    provider = es.EuropeanSleeperProvider()
    trip = {"trainNumber": "401", "departureTime": "2026-10-15T19:06:00", "arrivalTime": "2026-10-16T13:30:00",
            "fromLocationId": "8800104", "toLocationId": "8300112", "routeId": "x", "bookable": True}
    journey = es.to_journey(provider.name, trip, _query("Brusel", "Milano", (30,) * 5), [72] * 5)
    es.apply_offers(provider, journey, _data()["milan_group5"])
    by_tariff = {f.tariff.split(",")[0]: f.amount for f in journey.fares}
    assert journey.cheapest_fare.amount == Decimal("349.95")           # 5 sedadel
    couchettes = [f for f in journey.fares if f.tariff.startswith("lehátko")]
    assert min(couchettes, key=lambda f: f.amount).amount == Decimal("629.99")   # celé kupé
    assert "sedadlo 2. tř." in by_tariff


def test_sold_out():
    provider = es.EuropeanSleeperProvider()
    trip = es.parse_trips(_data()["trains"])[1]
    journey = es.to_journey(provider.name, trip, _query(), [72, 72, 73])
    es.apply_offers(provider, journey, _data()["sold_out"])
    assert journey.fare_status is FareStatus.SOLD_OUT and journey.price is None


@pytest.mark.asyncio
async def test_skips_routes_off_the_line():
    provider = es.EuropeanSleeperProvider()
    for origin, destination in (("Brno", "Amsterdam"), ("Praha", "Děčín"), ("Praha", "Milano")):
        with pytest.raises(StationNotFoundError):
            await provider.search(_query(origin, destination))
    assert provider._client is None            # bez jediného dotazu na síť


def _online(host: str) -> bool:
    try:
        socket.create_connection((host, 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_live_search():
    if not _online("europeansleeperprod-api.azurewebsites.net"):
        pytest.skip("bez sítě")
    provider = es.EuropeanSleeperProvider()
    try:
        today = dt.datetime.now(DEFAULT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        query = SearchQuery(origin="Praha", destination="Amsterdam", departure=today + dt.timedelta(days=3),
                            window=dt.timedelta(days=4))
        journeys = await provider.search(query)
    finally:
        await provider.aclose()
    assert journeys and all(j.legs[0].line.startswith("ES ") for j in journeys)
