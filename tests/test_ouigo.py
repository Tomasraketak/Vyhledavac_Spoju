import base64
import datetime as dt
import gzip
import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Passenger, SearchQuery
from vyhledavac.providers import ouigo

DATA = Path(__file__).parent / "data"


def _data():
    return json.load(gzip.open(DATA / "ouigo.json.gz", "rt", encoding="utf-8"))


def _stations(country):
    return ouigo.compact_stations(_data()[f"stations_{country.lower()}"], country)


def at(h, m=0):
    return dt.datetime(2026, 10, 14, h, m, tzinfo=DEFAULT_TZ)


def test_spain_group_price_is_for_whole_group():
    # 2 dospělí (33 €) + dítě (7 €) + batole (0 €) = 73 €
    item = _data()["es_madrid_barcelona_2A_C_B"]["outbound"][0]
    journey = ouigo.OuigoProvider().to_journey(item)
    assert journey.fare_status is FareStatus.PRICED
    assert journey.fares[0].amount == Decimal("73.0") and journey.fares[0].tariff == "OUIGO Esencial"
    leg = journey.legs[0]
    assert leg.line == "OUIGO 6471" and leg.reservation_required
    assert leg.origin.startswith("Madrid - Puerta de Atocha") and leg.destination == "Barcelona - Sants"
    assert journey.departure == at(6, 22) and journey.arrival == at(9, 45)


def test_france_group_price_and_sold_out():
    data = _data()
    # 2 dospělí (25 €) + dítě (8 €), Toupti zdarma = 58 €
    journey = ouigo.OuigoProvider().to_journey(data["fr_paris_lyon_2A_C_1baby"]["outbound"][0])
    assert journey.fares[0].amount == Decimal("58.0") and journey.fares[0].tariff == "OUIGO ESSENTIEL"
    assert journey.legs[0].carrier == "OUIGO Grande Vitesse" and journey.legs[0].line == "OUIGO 7867"
    assert journey.fares[0].seats_left == 10
    full = ouigo.OuigoProvider().to_journey(data["fr_full"]["outbound"][0])
    assert full.fare_status is FareStatus.SOLD_OUT and full.price is None


def test_station_picking_prefers_city_group():
    fr, es = _stations("FR"), _stations("ES")
    assert ouigo.pick_station("Paříž", fr)["code"] == "PT1"
    assert ouigo.pick_station("Lyon", fr)["code"] == "LY1"
    assert ouigo.pick_station("Lyon Part Dieu", fr)["code"] == "87723197"
    assert ouigo.pick_station("Brusel", fr)["country"] == "BE"
    assert ouigo.pick_station("Madrid", es)["code"] == "MT1"
    assert ouigo.pick_station("Valencie", es)["code"] == "7103216"
    assert ouigo.pick_station("Praha", es) is None


def test_connected_stations():
    es = {s["code"]: s for s in _stations("ES")}
    assert ouigo.connected(es["MT1"], es["7103216"])               # Madrid (vše) – Valencia
    assert not ouigo.connected(es["7160000"], es["7103216"])       # z Atochy do Valencie ne


def test_passenger_types_by_country():
    group = (Passenger(40), Passenger(12), Passenger(13), Passenger(3))
    query = SearchQuery("Paris", "Lyon", at(8), passengers=group)
    assert ouigo.passenger_types(query, "FR") == (["A", "A", "A"], 1)
    assert ouigo.passenger_types(query, "ES") == (["A", "C", "C", "B"], 0)
    kids = SearchQuery("Paris", "Lyon", at(8), passengers=(Passenger(9), Passenger(2)))
    assert ouigo.passenger_types(kids, "FR") == (["A"], 1)


def test_token_ttl_from_jwt():
    claims = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600}).encode()).decode().rstrip("=")
    assert 3000 < ouigo.token_ttl(f"x.{claims}.y") <= 3300
    assert ouigo.token_ttl("garbage") == 1800


def test_deep_link():
    link = ouigo.deep_link("ES", "MT1", "7171801", "2026-10-14", ["A", "C"])
    assert link.startswith("https://ventas.ouigo.com/es-ES/_deeplink/journey-search?target=calendar")
    assert "outboundDate=2026-10-14" in link and "passengers%5B1%5D%5Btype%5D=C" in link


@pytest.mark.asyncio
async def test_search_spain(monkeypatch):
    provider = ouigo.OuigoProvider()
    calls = []

    async def call(country, method, path, body=None):
        calls.append((country, path, body))
        if path == "Data/GetStations":
            return _data()["stations_es"]
        return _data()["es_madrid_barcelona_2A_C_B"]

    monkeypatch.setattr(provider, "_call", call)
    monkeypatch.setattr(provider, "stations", lambda country: _stations_async(country))
    query = SearchQuery("Madrid", "Barcelona", at(8),
                        passengers=(Passenger(40), Passenger(38), Passenger(8), Passenger(2)))
    journeys = await provider.search(query)
    country, path, body = calls[-1]
    assert (country, path) == ("ES", "Sale/journeysearch")
    assert body["origin"] == "MT1" and body["destination"] == "7171801"
    assert [p["type"] for p in body["passengers"]] == ["A", "A", "C", "B"]
    assert [j.departure for j in journeys] == [at(9, 31)]           # 6:22 je před zadaným časem


async def _stations_async(country):
    return _stations(country)


@pytest.mark.asyncio
async def test_skips_irrelevant_routes_without_network(monkeypatch):
    provider = ouigo.OuigoProvider()

    async def no_network(*args, **kwargs):
        raise AssertionError("síť se nemá volat")

    monkeypatch.setattr(provider, "stations", no_network)
    monkeypatch.setattr(provider, "_call", no_network)
    for origin, destination in (("Praha", "Wien"), ("Praha", "Brno"), ("Madrid", "Paříž")):
        with pytest.raises(StationNotFoundError):
            await provider.search(SearchQuery(origin=origin, destination=destination, departure=at(8)))
