"""Bileto a Arriva: převod odpovědí API (zkrácené skutečné odpovědi, říjen 2026)."""

import asyncio
from decimal import Decimal

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.models import DiscountCard, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import arriva, bileto
from vyhledavac.providers.arriva import ArrivaProvider
from vyhledavac.providers.bileto import BiletoProvider

from conftest import at


# ---- Bileto ------------------------------------------------------------------------
def bileto_connection(dep="2026-10-13T07:30:00+02:00", arr="2026-10-13T10:45:00+02:00"):
    return {"Id": "conn-1", "Legs": [{
        "LegType": "TransitLeg", "DepartureAt": dep, "ArrivalAt": arr,
        "DepartureStation": {"Name": "Třeboň, Táboritská", "Type": ["bus"]},
        "ArrivalStation": {"Name": "Brno, Dolní nádraží", "Type": ["bus"]},
        "Trip": {"Route": {"Name": "332004",
                           "FullName": "Lipno nad Vltavou - České Budějovice - Brno - Olomouc"},
                 "Operator": {"Name": "RETROBUS Line, s.r.o."}},
    }]}


def test_bileto_connection_to_journey():
    q = SearchQuery(origin="Třeboň", destination="Brno", departure=at(6))
    journey = BiletoProvider(cache=MemoryCache())._to_journey(bileto_connection(), q, "a", "b")
    leg = journey.legs[0]
    assert (leg.origin, leg.destination, leg.mode) == ("Třeboň, Táboritská", "Brno, Dolní nádraží", Mode.BUS)
    assert leg.carrier == "RETROBUS Line, s.r.o." and "Brno" in leg.line
    assert journey.journey_id == "conn-1" and "from=a&to=b" in journey.deep_link


def test_bileto_price_and_passengers():
    data = [{"FreeSeatsCount": 48, "Prices": {"userPrice": {"amount": 600, "currency": "CZK"}}}]
    assert bileto.parse_price(data) == (Decimal(600), "CZK", 48)
    student = bileto.passenger_body(Passenger(age=20, cards=(DiscountCard.ISIC,)), 1)
    assert student == {"Age": 20, "Cards": [bileto.ISIC_CARD], "Sequence": 1}
    assert bileto.passenger_body(Passenger(age=30), 0)["Cards"] == []


def test_bileto_prefers_city_on_same_name():
    places = [{"Id": "s", "Name": "Třeboň", "Type": "station"},
              {"Id": "c", "Name": "Třeboň", "Type": "city"},
              {"Id": "x", "Name": "Třebíč", "Type": "city"}]
    assert bileto.pick_place("Treboň", places) == ("c", "Třeboň")
    assert bileto.pick_place("Ostrava", [{"Id": "x", "Name": "Třebíč", "Type": "city"}]) is None


# ---- Arriva ------------------------------------------------------------------------
def station(number, name, arr, dep):
    return {"station": {"type": "arriva", "number": number}, "name": name,
            "arrivalTime": arr, "departureTime": dep, "isOnRoute": True, "platform": ""}


ROUTE = {"searchId": "search-1", "routes": [{
    "sourceStationDepartureTime": "2026-10-13T06:45:00Z",
    "targetStationArrivalTime": "2026-10-13T08:22:00Z",
    "line": {"prefix": "Bus", "number": 157710, "carrierId": 27},
    "stations": [station(8208, "Praha,,Nádraží Holešovice", "2026-10-13T06:45:00Z", "2026-10-13T06:45:00Z"),
                 station(8686, "Louny,,aut.nádr.", "2026-10-13T07:54:00Z", "2026-10-13T07:54:00Z"),
                 station(8212, "Most,,1.nám.", "2026-10-13T08:22:00Z", "2026-10-13T08:22:00Z")],
}]}


def test_arriva_route_to_journey():
    journey = arriva.to_journey(ROUTE, "https://shop.arriva.cz/x")
    leg = journey.legs[0]
    assert leg.mode is Mode.BUS and leg.line == "Bus 157710" and leg.carrier == "Arriva City"
    assert [s.name for s in leg.stops] == ["Praha, Nádraží Holešovice", "Louny, aut.nádr.", "Most, 1.nám."]
    assert journey.departure.hour == 8 and journey.departure.minute == 45      # UTC -> Praha
    sjt = dict(ROUTE, routes=[dict(ROUTE["routes"][0], line={"prefix": "R", "number": 705, "carrierId": 0})])
    leg = arriva.to_journey(sjt, "").legs[0]
    assert leg.mode is Mode.TRAIN and leg.line == "R 705" and "SJT" in leg.carrier


def test_arriva_price_is_already_per_quantity():
    data = {"prices": [{"tariffId": 12, "prices": [
        {"passengerPrice": 238.0, "reservationPrice": 0.0, "quantity": 2, "abstractPassengerCategoryId": 1},
        {"passengerPrice": 59.0, "reservationPrice": 0.0, "quantity": 1, "abstractPassengerCategoryId": 4}]}]}
    assert arriva.parse_price(data) == (Decimal(297), "Tarif Arriva Express")


@pytest.mark.parametrize("age, isic, expected", [
    (3, False, arriva.INFANT), (10, False, arriva.CHILD), (17, False, arriva.CHILD),
    (20, True, arriva.STUDENT), (20, False, arriva.ADULT), (70, False, arriva.SENIOR)])
def test_arriva_categories(age, isic, expected):
    cards = (DiscountCard.ISIC,) if isic else ()
    assert arriva.category(Passenger(age=age, cards=cards)) == expected


PLACES = [
    {"id": 8212, "name": "Most,,1.nám.", "typeId": 2, "priority": 45},
    {"id": 8211, "name": "Most,,Rudolická MUS a.s.", "typeId": 2, "priority": 30},
    {"id": 1177, "name": "Most", "typeId": 1, "priority": 0},
    {"id": 1178, "name": "Most-Kopisty", "typeId": 1, "priority": 0},
    {"id": 9999, "name": "Kamenný Most u Kralup nad Vltavou", "typeId": 1, "priority": 0},
]


def test_arriva_town_gets_station_and_main_stop():
    found = arriva.pick_places("Most", PLACES)
    assert found[arriva.TRAIN]["id"] == 1177 and found[arriva.BUS]["id"] == 8212
    assert arriva.pick_places("Most-Kopisty", PLACES) == {arriva.TRAIN: PLACES[3]}
    assert arriva.pick_places("Most,,Rudolická MUS a.s.", PLACES) == {arriva.BUS: PLACES[1]}


class FakeClient:
    def __init__(self):
        self.routes = []

    async def post_json(self, url, body, **kwargs):
        if url.endswith("/Route/Search"):
            self.routes.append((body["source"]["number"], body["target"]["number"]))
            return [ROUTE]
        return {"prices": [{"tariffId": 12, "prices": [{"passengerPrice": 119.0, "quantity": 1}]}]}


def test_arriva_search_tries_station_and_stop_pairs_and_dedupes(monkeypatch):
    provider = ArrivaProvider(cache=MemoryCache())
    fake = FakeClient()
    provider._client = fake

    async def places(name):
        return PLACES if name == "Most" else [
            {"id": 2236, "name": "Praha hlavní nádraží", "typeId": 1, "priority": 45},
            {"id": 8208, "name": "Praha,,Nádraží Holešovice", "typeId": 2, "priority": 49}]

    async def headers():
        return {}

    monkeypatch.setattr(provider, "places", places)
    monkeypatch.setattr(provider, "_headers", headers)
    query = SearchQuery(origin="Praha", destination="Most", departure=at(6, day=13))
    journeys = asyncio.run(provider.search(query))
    assert sorted(fake.routes) == [(2236, 1177), (8208, 8212)]
    assert len(journeys) == 1                       # tentýž autobus jen jednou
    assert journeys[0].price == 119 and journeys[0].fare_status is FareStatus.PRICED


def test_arriva_merges_renumbered_train():
    def part(number, a, b, t1, t2):
        return {"sourceStationDepartureTime": t1, "targetStationArrivalTime": t2,
                "line": {"prefix": "R", "number": number, "carrierId": 0},
                "stations": [station(1, a, t1, t1), station(2, b, t2, t2)]}
    raw = {"searchId": "s", "routes": [
        part(653, "Veselí n.Lužnicí", "Horní Cerekev", "2026-10-13T05:00:00Z", "2026-10-13T06:00:00Z"),
        part(96653, "Horní Cerekev", "Náměšť n.Oslavou", "2026-10-13T06:06:00Z", "2026-10-13T07:00:00Z"),
        part(653, "Náměšť n.Oslavou", "Brno hl.n.", "2026-10-13T07:00:00Z", "2026-10-13T07:30:00Z")]}
    journey = arriva.to_journey(raw, "")
    assert len(journey.legs) == 1 and journey.legs[0].destination == "Brno hl.n."
    assert [s.name for s in journey.legs[0].stops][1] == "Horní Cerekev"


def test_arriva_keeps_real_transfer():
    def part(number, a, b, t1, t2):
        return {"sourceStationDepartureTime": t1, "targetStationArrivalTime": t2,
                "line": {"prefix": "R", "number": number, "carrierId": 0},
                "stations": [station(1, a, t1, t1), station(2, b, t2, t2)]}
    raw = {"searchId": "s", "routes": [
        part(730, "Třeboň", "Veselí n.Lužnicí", "2026-10-13T04:00:00Z", "2026-10-13T04:30:00Z"),
        part(653, "Veselí n.Lužnicí", "Brno hl.n.", "2026-10-13T04:42:00Z", "2026-10-13T07:00:00Z")]}
    assert len(arriva.to_journey(raw, "").legs) == 2
