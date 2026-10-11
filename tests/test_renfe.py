import datetime as dt
import gzip
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Passenger, SearchQuery
from vyhledavac.providers import renfe

DATA = Path(__file__).parent / "data"


def _text(name):
    return gzip.open(DATA / name, "rt", encoding="utf-8").read()


def _stations():
    return renfe.parse_stations(gzip.open(DATA / "renfe_stations.js.gz", "rb").read().decode("latin-1"))


def at(h, m=0):
    return dt.datetime(2026, 10, 14, h, m, tzinfo=DEFAULT_TZ)


def _trains(name):
    return renfe.trains_of(renfe.parse_dwr(_text(name)))


def test_parse_dwr_reads_js_literal():
    trains = _trains("renfe_barcelona_sevilla.dwr.gz")
    assert len(trains) == 11
    first = trains[0]
    assert first["tarifaMinima"] == "111,1" and first["directo"] is False
    assert [t["cdgoTren"] for t in first["trayectos"]] == ["03052", "02100"]
    # á v odpovědi → „Básico“
    assert first["tarifasDisponibles"][0]["titulo"] == "Básico"


def test_connection_with_change_for_one_adult():
    provider = renfe.RenfeProvider()
    journey = provider.to_journey(_trains("renfe_barcelona_sevilla.dwr.gz")[0], adults=1, children=0)
    assert [leg.line for leg in journey.legs] == ["AVE 3052", "AVE 2100"]
    assert journey.legs[0].destination == journey.legs[1].origin
    assert journey.departure == at(5, 40) and journey.arrival == at(12, 40)
    assert journey.transfers == 1 and journey.legs[0].reservation_required
    assert journey.fare_status is FareStatus.PRICED
    assert journey.fares[0].amount == Decimal("111.10") and journey.fares[0].tariff == "Básico"


def test_avlo_change_with_children_is_only_indicative():
    provider = renfe.RenfeProvider()
    mixed = next(t for t in _trains("renfe_barcelona_sevilla.dwr.gz") if t.get("tipoTrenDos") == "AVLO")
    journey = provider.to_journey(mixed, adults=2, children=1)
    assert journey.price is None and journey.raw["indicative_eur"]
    assert "AVLO" in journey.notes[0]
    # bez dětí je cena přesná: 2 × cena za osobu
    journey = provider.to_journey(mixed, adults=2, children=0)
    assert journey.fares[0].amount == Decimal(mixed["tarifaMinima"].replace(",", ".")) * 2


def test_group_price_children_discount_and_avlo():
    trains = _trains("renfe_madrid_valencia.dwr.gz")
    provider = renfe.RenfeProvider()
    avlo = next(t for t in trains if t["tipoTrenUno"] == "AVLO")      # 29,0 €
    ave = next(t for t in trains if t["tipoTrenUno"] == "AVE")        # 56,9 €
    assert provider.to_journey(avlo, 2, 1).fares[0].amount == Decimal("63.00")     # 2×29 + 5
    assert provider.to_journey(ave, 2, 1).fares[0].amount == Decimal("147.94")     # 56,9 × 2,6
    assert provider.to_journey(ave, 2, 1).legs[0].line == "AVE 5070"


def test_group_price_more_avlo_children_than_allowed():
    total, exact = renfe.group_price(Decimal("25"), ["AVLO"], adults=1, children=3)
    assert not exact


def test_stations_prefer_city_group():
    stations = _stations()
    assert renfe.pick_station("Madrid", stations)["code"] == "MADRI"
    assert renfe.pick_station("Barcelona Sants", stations)["code"] == "71801"
    assert renfe.pick_station("Valencie", stations)["code"] == "VALEN"
    assert renfe.pick_station("Málaga", stations)["code"] == "54413"       # ne letiště
    lyon = renfe.pick_station("Lyon", stations)
    assert lyon["code"] == "LYON-" and lyon["country"] == "FR"
    assert renfe.pick_station("Praha", stations) is None


def test_passenger_counts_by_age():
    query = SearchQuery("Madrid", "Barcelona", at(8),
                        passengers=(Passenger(40), Passenger(13), Passenger(14), Passenger(3)))
    assert renfe.count_passengers(query) == (2, 1, 1)
    alone = SearchQuery("Madrid", "Barcelona", at(8), passengers=(Passenger(10),))
    assert renfe.count_passengers(alone) == (1, 0, 0)


def test_dwr_object_encoding():
    params = renfe.encode_object({"origen": "MADRI", "fechaSalida": "14/10/2026"})
    assert params["c0-e2"] == "string:14%2F10%2F2026"
    assert params["c0-param0"] == "Object_Object:{origen:reference:c0-e1, fechaSalida:reference:c0-e2}"


@pytest.mark.asyncio
async def test_search_filters_by_time(monkeypatch):
    provider = renfe.RenfeProvider()
    sent = {}

    async def stations():
        return _stations()

    async def trains(origin, destination, date, adults, children, infants):
        sent.update(origin=origin["code"], destination=destination["code"], date=date,
                    people=(adults, children, infants))
        return _text("renfe_madrid_valencia.dwr.gz")

    monkeypatch.setattr(provider, "stations", stations)
    monkeypatch.setattr(provider, "_trains", trains)
    journeys = await provider.search(SearchQuery("Madrid", "Valencia", at(12),
                                                 passengers=(Passenger(30), Passenger(8))))
    assert sent == {"origin": "MADRI", "destination": "VALEN", "date": "14/10/2026", "people": (1, 1, 0)}
    assert journeys and all(j.departure >= at(12) for j in journeys)
    assert journeys[0].departure == at(12, 30) and journeys[0].fares[0].amount == Decimal("30.00")  # AVLO 25 + 5


@pytest.mark.asyncio
async def test_skips_routes_outside_spain_without_network(monkeypatch):
    provider = renfe.RenfeProvider()

    async def no_network():
        raise AssertionError("síť se nemá volat")

    monkeypatch.setattr(provider, "stations", no_network)
    with pytest.raises(StationNotFoundError):
        await provider.search(SearchQuery(origin="Praha", destination="Wien", departure=at(8)))
    with pytest.raises(StationNotFoundError):
        await provider.search(SearchQuery(origin="Praha", destination="Brno", departure=at(8)))
