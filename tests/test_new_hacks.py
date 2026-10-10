"""Přestupy v Praze/Brně, Včasná Evropa, regionální jízdenky a PKP (Koleo)."""

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac import citytransfer, geo
from vyhledavac.hacks.cd_europe import (CdEuropeBeyondHack, CdEuropeBorderHack, RegionalPassHack,
                                        continues)
from vyhledavac.hacks.hub_transfer import HubTransferHack
from vyhledavac.models import Journey, Leg, Mode, Passenger, SearchQuery
from vyhledavac.providers import pkp

from conftest import at, make_journey


# ---- přejezdy po městě -------------------------------------------------------------
def test_prague_transfers_by_metro_and_walk():
    t = citytransfer.transfer("Praha hl.n.", "Praha, Černý Most")
    assert t.how.startswith("metro C → B") and 25 <= t.minutes <= 45 and not t.walk
    walk = citytransfer.transfer("Praha hl. n.", "Praha,,Florenc")
    assert walk.walk and citytransfer.city_fare(walk, (Passenger(age=30),)) == 0
    assert citytransfer.city_fare(t, (Passenger(age=30),)) in (Decimal(36), Decimal(46))
    assert citytransfer.city_fare(t, (Passenger(age=10),)) == 0         # dítě v Praze zdarma
    assert citytransfer.transfer("Praha hl.n.", "Brno hl.n.") is None


def test_brno_transfers():
    assert citytransfer.transfer("Brno,,Benešova tř.hotel GRAND", "Brno hl. n.").walk
    tram = citytransfer.transfer("Brno hl.n.", "Brno, ÚAN Zvonařka")
    assert not tram.walk and citytransfer.city_fare(tram, (Passenger(age=30),)) > 0


@pytest.mark.asyncio
async def test_hub_transfer_combines_two_carriers_with_metro():
    query = SearchQuery(origin="Plzeň", destination="Liberec", departure=at(6))
    direct = make_journey(provider="FlixBus", origin="Plzeň", destination="Liberec", dep=at(7),
                          arr=at(12), price=500, stops=["Plzeň", "Praha, Florenc", "Liberec"])
    train = make_journey(provider="ČD", origin="Plzeň", destination="Praha hl.n.", dep=at(6, 10),
                         arr=at(7, 40), price=150)
    bus = make_journey(provider="RegioJet", origin="Praha, Černý Most", destination="Liberec",
                       dep=at(8, 30), arr=at(9, 40), price=120, mode=Mode.BUS)
    tight = make_journey(provider="RegioJet", origin="Praha, Černý Most", destination="Liberec",
                         dep=at(7, 50), arr=at(9, 0), price=100, mode=Mode.BUS)
    calls = []

    async def search(q, allow_browser=False, only=None):
        calls.append((q.origin, q.destination))
        return [train] if q.destination == "Praha" else [tight, bus]

    found = await HubTransferHack().find(query, [direct], search)
    assert calls == [("Plzeň", "Praha"), ("Praha", "Liberec")]
    assert len(found) == 1
    journey = found[0]
    assert [s.provider for s in journey.segments][0] == "ČD" and journey.segments[-1] is bus
    assert any(leg.mode is Mode.METRO for leg in journey.legs)          # metro C → B
    assert journey.price == Decimal(150 + 120 + 46)                     # + jízdenka PID 90 min
    assert "Černý Most" in journey.notes[0]


# ---- země a koridor -------------------------------------------------------------------
@pytest.mark.parametrize("name, code", [("Třeboň", "CZ"), ("Marchegg", "AT"), ("Wien Hbf", "AT"),
                                        ("Bratislava hl.st.", "SK"), ("Kraków Główny", "PL"),
                                        ("München Hbf", "DE")])
def test_country_of(name, code):
    assert geo.country_of(name) == code


def test_corridor_targets():
    assert geo.corridor_targets("Wien Hbf", "Linz Hbf") == ["Horní Dvořiště"]
    assert geo.corridor_targets("Salzburg Hbf", "Wien Hbf") == ["Břeclav"]
    assert geo.corridor_targets("Linz Hbf", "Salzburg Hbf") == []       # od Česka – nevyplatí se


# ---- Včasná Evropa ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_border_split_cd_to_marchegg_then_obb():
    query = SearchQuery(origin="Třeboň", destination="Bratislava hl.st.", departure=at(6))
    direct = make_journey(provider="ÖBB", origin="Třeboň", destination="Bratislava hl.st.",
                          dep=at(7), arr=at(13), price=1200,
                          stops=["Třeboň", "Wien Hbf", "Marchegg", "Bratislava hl.st."])
    head = make_journey(provider="ČD", origin="Třeboň", destination="Marchegg", dep=at(7),
                        arr=at(12, 30), price=349, line="R 321")
    tail = make_journey(provider="ÖBB", origin="Marchegg", destination="Bratislava hl.st.",
                        dep=at(12, 40), arr=at(13, 10), price=109, line="REX 7")
    asked = []

    async def search(q, allow_browser=False, only=None):
        asked.append((q.destination, allow_browser, tuple(only or ())))
        if q.destination == "Marchegg":
            return [head]
        if q.origin == "Marchegg" and q.destination == "Bratislava hl.st.":
            return [tail]
        return []

    found = await CdEuropeBorderHack().find(query, [direct], search)
    assert found and found[0].price == Decimal(458)
    assert asked[0] == ("Marchegg", True, ("ČD",))                     # jen ČD, jednou
    assert "Včasná" in found[0].notes[0] and "Marchegg" in found[0].notes[0]


@pytest.mark.asyncio
async def test_beyond_uses_obb_times_when_cd_shows_only_border_train():
    query = SearchQuery(origin="Wien Hbf", destination="Linz Hbf", departure=at(6))
    obb = make_journey(provider="ÖBB", origin="Wien Hbf", destination="Linz Hbf", dep=at(7, 57),
                       arr=at(9, 12), price=1500, line="RJ 542")
    cd = make_journey(provider="ČD", origin="Wien Hbf", destination="Horní Dvořiště", dep=at(7, 57),
                      arr=at(10, 58), price=411, line="Sp 3802")

    async def search(q, allow_browser=False, only=None):
        assert only == ["ČD"] and q.destination == "Horní Dvořiště"
        return [cd]

    found = await CdEuropeBeyondHack().find(query, [obb], search)
    assert len(found) == 1 and found[0].price == Decimal(411)
    assert found[0].arrival == at(9, 12) and found[0].destination == "Linz Hbf"


def test_same_train_or_transfer():
    a = make_journey(dep=at(7), arr=at(9), line="R 321")
    assert continues(a, make_journey(dep=at(9, 1), arr=at(10), line="R 321"))
    assert not continues(a, make_journey(dep=at(9, 1), arr=at(10), line="REX 7"))
    assert continues(a, make_journey(dep=at(9, 10), arr=at(10), line="REX 7"))


# ---- regionální jízdenky -----------------------------------------------------------
def test_regional_pass_rules():
    era, bayern = geo.PASSES
    assert era.price(1) is None and era.price(3) == Decimal(46)          # jen skupina 2–5
    assert bayern.price(1) == Decimal(34) and bayern.price(3) == Decimal(54)
    assert era.train_ok("REX 7") and not era.train_ok("RJX 66")
    assert bayern.train_ok("ALX 84110") and not bayern.train_ok("EC 360")
    assert bayern.covers("München Hbf") and not bayern.covers("Berlin Hbf")
    assert not bayern.valid_at(dt.datetime(2026, 10, 20, 8, 0)) and bayern.valid_at(
        dt.datetime(2026, 10, 24, 8, 0))                                     # sobota


@pytest.mark.asyncio
async def test_regional_pass_needs_regional_trains_after_border():
    query = SearchQuery(origin="Praha", destination="München Hbf", departure=at(6, day=20))
    ec = make_journey(provider="ČD", origin="Praha", destination="München Hbf", dep=at(7, 35, 20),
                      arr=at(13, 0, 20), price=1500, line="EC 360")

    async def search(q, allow_browser=False, only=None):
        raise AssertionError("EC v Německu – na ČD se ani nemá ptát")

    assert await RegionalPassHack().find(query, [ec], search) == []


# ---- PKP / Koleo ---------------------------------------------------------------------------
def test_koleo_connection_and_price():
    raw = {"id": 1, "purchasable": True, "trains": [{
        "train_full_name": "411 LE411", "brand_id": 47, "start_station_id": 1, "end_station_id": 2,
        "departure": "2026-10-13T11:59:00.000+02:00", "arrival": "2026-10-13T14:22:00.000+02:00",
        "stops": [{"station_id": 1, "in_path": True, "departure": "2026-10-13T11:59:00.000+02:00"},
                  {"station_id": 3, "in_path": False},
                  {"station_id": 2, "in_path": True, "arrival": "2026-10-13T14:22:00.000+02:00"}]}]}
    journey = pkp.to_journey(raw, {1: "Ostrava hl.n.", 2: "Kraków Główny", 3: "x"}, {47: "Leo Express"})
    leg = journey.legs[0]
    assert (leg.origin, leg.destination, leg.carrier) == ("Ostrava hl.n.", "Kraków Główny", "Leo Express")
    assert [s.name for s in leg.stops] == ["Ostrava hl.n.", "Kraków Główny"]
    price = {"prices": [{"value": "28.5", "tariff_names": ["Przejazd tam"], "valid_price": True},
                        {"value": "35", "tariff_names": ["X"], "valid_price": True}]}
    assert pkp.parse_price(price) == (Decimal("28.5"), "Przejazd tam")
