"""Zpáteční cesta (tentýž den / do 24 h), celodenní jízdenky IDS, letadla mimo hledání."""

import datetime as dt
from decimal import Decimal

from vyhledavac import round_trip, transitous
from vyhledavac.ids import ideska, pid
from vyhledavac.models import Fare, FareStatus, Journey, Passenger, SearchQuery

from conftest import at, make_journey


def _ticket(price, name="Jihočeská 24 plné jízdné"):
    return Journey(provider="IDESKA", legs=[], fare_status=FareStatus.PRICED,
                   fares=[Fare(amount=Decimal(price), currency="CZK", amount_czk=Decimal(price),
                               label="IDESKA", tariff=name)])


def test_valid_minutes_same_day_and_24h():
    assert round_trip.valid_minutes("24h", at(8)) == 1440
    assert round_trip.valid_minutes("day", at(22)) == 120


def test_round_trip_takes_day_ticket_when_cheaper(monkeypatch):
    query = SearchQuery(origin="Třeboň", destination="Strakonice", departure=at(7), round_trip="24h")
    journey = make_journey(provider="IDS", origin="Třeboň", destination="Strakonice", price=99)
    monkeypatch.setattr(round_trip, "day_ticket", lambda q, legs, m: _ticket(195))
    info = round_trip.compute(journey, query)
    assert info["price"] == Decimal(195) and info["saving"] == Decimal(3)
    monkeypatch.setattr(round_trip, "day_ticket", lambda q, legs, m: _ticket(250))
    assert round_trip.compute(journey, query)["price"] == Decimal(198)


def test_round_trip_per_ticket_part(monkeypatch):
    """IDESKA na začátek celodenní, ČD na zbytek dvakrát."""
    query = SearchQuery(origin="Třeboň", destination="Praha", departure=at(7), round_trip="day")
    ids_part = make_journey(provider="IDESKA", origin="Třeboň", destination="Tábor", price=120)
    cd_part = make_journey(provider="ČD", origin="Tábor", destination="Praha", price=150)
    journey = Journey(provider="IDESKA + ČD", legs=ids_part.legs + cd_part.legs, segments=[ids_part, cd_part],
                      fares=[Fare(amount=Decimal(270), currency="CZK", amount_czk=Decimal(270))],
                      fare_status=FareStatus.PRICED)
    monkeypatch.setattr(round_trip, "day_ticket",
                        lambda q, legs, m: _ticket(195) if legs[-1].destination == "Tábor" else None)
    info = round_trip.compute(journey, query)
    assert info["price"] == Decimal(195 + 300)
    round_trip.annotate([journey], query)
    assert journey.raw["round_trip"]["price"] == Decimal(495) and journey.notes[-1].startswith("↔")


def test_no_round_trip_without_option():
    journey = make_journey(price=100)
    round_trip.annotate([journey], SearchQuery(origin="A", destination="B", departure=at(7)))
    assert "round_trip" not in journey.raw


def test_pid_day_tickets_2026():
    adult, student = Passenger(age=30), Passenger(age=20, cards=())
    assert pid.best_fare("P", "B", adult, 1440).price == 140
    assert pid.best_fare("1", "3", adult, 1440).price == 96
    assert pid.best_fare("2", "7", adult, 1440).price == 168
    assert pid.best_fare("P", "3", adult, 1440).price == 288


def test_ideska_south_bohemia_24_everywhere():
    fare = ideska.best_fare("326", "700", Passenger(age=30), 1440)
    assert fare is not None and fare.name.startswith("Jihočeská 24") and fare.price == 195


def test_transitous_drops_flights():
    leg = {"mode": "RAIL", "startTime": "2026-10-25T08:00:00Z", "endTime": "2026-10-25T09:00:00Z",
           "from": {"name": "Oslo S"}, "to": {"name": "Oslo lufthavn"}}
    flight = {**leg, "mode": "AIRPLANE", "from": {"name": "Oslo lufthavn"}, "to": {"name": "Trondheim lufthavn"}}
    assert transitous.to_connections({"itineraries": [{"legs": [leg, flight]}]}) == []
    assert transitous.is_air({"routeType": 1100}) and not transitous.is_air({"routeType": 2})
    assert "AIRPLANE" not in transitous.GROUND_MODES


def test_transitous_trims_city_sightseeing_bus_at_czech_end():
    from vyhledavac.models import Leg, Mode
    from vyhledavac.providers.transitous_jr import _trim_foreign_ends

    query = SearchQuery(origin="Praha", destination="Berlín", departure=at(10))
    city = Leg("Praha, Václavské náměstí", "Praha, Hlavní nádraží", at(10, 21), at(10, 26), Mode.BUS,
               "PREMIANT CITY TOUR s.r.o.", "Bus 103904 21")
    train = Leg("Praha hl.n.", "Berlin Hbf", at(10, 31), at(14, 29), Mode.TRAIN, "ČD", "RJ 382")
    trimmed = _trim_foreign_ends(Journey(provider="Transitous", legs=[city, train]), query)
    assert [leg.line for leg in trimmed.legs] == ["RJ 382"]
    # cesta po jednom městě se neořezává
    inside = SearchQuery(origin="Praha, Václavské náměstí", destination="Praha, Hlavní nádraží", departure=at(10))
    assert _trim_foreign_ends(Journey(provider="Transitous", legs=[city]), inside).legs == [city]
