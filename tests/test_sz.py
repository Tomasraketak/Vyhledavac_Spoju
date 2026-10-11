import datetime as dt
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import sz

DATA = json.load(gzip.open(Path(__file__).parent / "data" / "sz_eshop.json.gz", "rt", encoding="utf-8"))


def at(h, m=0, day=14):
    return dt.datetime(2026, 10, day, h, m, tzinfo=DEFAULT_TZ)


def test_parse_stations_and_pick_main_station():
    items = sz.parse_stations(DATA["autocomplete_ljubljana"])
    assert items[0] == ("136755", "Ljubljana", False)
    assert sz.pick_station("Ljubljana", items)[0] == "136755"
    assert sz.pick_station("Ljubljana Tivoli", items)[1] == "Ljubljana Tivoli"
    novo = sz.parse_stations(DATA["autocomplete_novo_mesto"])
    assert sz.pick_station("Novo mesto", novo)[1] == "Novo mesto"


def test_regional_rows_with_change_and_group_price():
    rows = sz.parse_results(DATA["regional_2a1c"])
    assert len(rows) == 4
    first = rows[0]
    # LP 2251 do Zidaného Mostu, tam přestup na LP 2800 do Mariboru
    assert [(l.line, l.origin, l.destination) for l in first["legs"]] == [
        ("LP 2251", "Ljubljana", "Zidani Most"), ("LP 2800", "Zidani Most", "Maribor")]
    assert first["legs"][0].departure == at(4, 45) and first["legs"][-1].arrival == at(7, 29)
    assert "Litija" in first["legs"][0].stop_names()
    assert first["price"] == Decimal("36.00")          # 2 dospělí + 1 dítě 6–14 let
    # náhradní autobus Ljubljana – Celje a upozornění e-shopu
    bus = rows[1]
    assert bus["legs"][0].mode is Mode.BUS and bus["legs"][0].line.startswith("BUS")
    assert any("Avtobusni" in n for n in bus["notices"])
    # noční spoj přes půlnoc
    late = rows[-1]["legs"][-1]
    assert late.arrival == at(1, 6, day=15)


def test_to_journey_ic_fare():
    rows = sz.parse_results(DATA["ic_2a1c1i"])
    journey = sz.SzProvider(cache=MemoryCache()).to_journey(rows[0], ic=True, children=1)
    assert journey.legs[0].line == "IC 351" and journey.transfers == 0
    assert journey.fare_status is FareStatus.PRICED
    fare = journey.fares[0]
    assert fare.amount == Decimal("42.00") and fare.currency == "EUR" and "IC" in fare.tariff


def test_passenger_counts():
    q = SearchQuery(origin="Ljubljana", destination="Maribor", departure=at(8),
                    passengers=(Passenger(age=40), Passenger(age=38), Passenger(age=10), Passenger(age=3)))
    assert sz.passenger_counts(q) == (2, 1, 1)


@pytest.mark.asyncio
async def test_search_merges_regional_and_ic(monkeypatch):
    provider = sz.SzProvider(cache=MemoryCache())
    sent = []

    async def fake_post(action, form):
        sent.append((action, dict(form)))
        if action == "TravelAutocomplete":
            return DATA["autocomplete_ljubljana"] if form["SearchText"].startswith("Lj") else \
                '<li data-value="136864" data-text="Maribor" data-abroad="False" onclick="SetValueTo(this)">'
        return DATA["ic_2a1c1i"] if form["IcMcVc"] == "true" else DATA["regional_2a1c"]

    monkeypatch.setattr(provider, "_post", fake_post)
    q = SearchQuery(origin="Ljubljana", destination="Maribor", departure=at(4),
                    passengers=(Passenger(age=40), Passenger(age=38), Passenger(age=10)))
    journeys = await provider.search(q)
    lines = [j.legs[0].line for j in journeys]
    assert "IC 351" in lines and "LP 2251" in lines
    assert journeys == sorted(journeys, key=lambda j: j.departure)
    searches = [f for a, f in sent if a == "FilterSingle"]
    assert {f["IcMcVc"] for f in searches} == {"true", "false"}
    assert searches[0]["DepartureDate"] == "14.10.2026" and searches[0]["NumOfPassengers[1]"] == "1"


@pytest.mark.asyncio
async def test_skips_routes_outside_slovenia_without_network():
    provider = sz.SzProvider(cache=MemoryCache())
    for origin, destination in (("Praha", "Wien"), ("Ljubljana", "Zagreb"), ("Villach", "Ljubljana")):
        with pytest.raises(StationNotFoundError):
            await provider.search(SearchQuery(origin=origin, destination=destination, departure=at(8)))
    assert provider._client is None          # žádný HTTP klient se nevytvořil
