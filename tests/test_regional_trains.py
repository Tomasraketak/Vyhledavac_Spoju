"""Deutschlandticket a volba „jen regionální vlaky“ (Německo, Rakousko)."""

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac import geo
from vyhledavac.cli import build_parser, build_passenger
from vyhledavac.filters.builtin import RegionalOnly
from vyhledavac.hacks.cd_europe import RegionalPassHack
from vyhledavac.models import DEFAULT_TZ, DiscountCard, Journey, Leg, Mode, Passenger, SearchQuery
from vyhledavac.providers import db
from vyhledavac.web import server as web

from conftest import make_journey

DT = (DiscountCard.DE_DEUTSCHLANDTICKET,)


def at(hour, minute=0, day=20):                  # úterý 20. 10. 2026
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=DEFAULT_TZ)


def leg(a, b, line, mode=Mode.TRAIN, dep=8, arr=9):
    return Leg(a, b, at(dep), at(arr), mode, "x", line)


# ---- které vlaky jsou regionální ------------------------------------------------------
@pytest.mark.parametrize("line, ok", [("RE 1", True), ("RB 23", True), ("S 3", True), ("ALX 84110", True),
                                      ("REX 7", True), ("CJX 9", True), ("ICE 123", False), ("IC 2020", False),
                                      ("RJX 66", False), ("NJ 40490", False), ("WB 911", False)])
def test_regional_categories_in_germany_and_austria(line, ok):
    assert geo.is_regional_leg(leg("München Hbf", "Augsburg Hbf", line)) is ok
    assert geo.is_regional_leg(leg("Wien Hbf", "Linz/Donau Hbf", line)) is ok


def test_regional_check_only_in_germany_and_austria():
    assert geo.is_regional_leg(leg("Praha hl.n.", "Brno hl.n.", "EC 171"))       # po Česku se nehlídá
    assert not geo.is_regional_leg(leg("Praha hl.n.", "München Hbf", "EC 360"))  # do Německa ano
    assert geo.is_regional_leg(leg("München Hbf", "Augsburg Hbf", "Bus 100", Mode.BUS))


def test_regional_only_filter_follows_query():
    rj = make_journey(origin="Wien Hbf", destination="Salzburg Hbf", line="RJX 66")
    rex = make_journey(origin="Wien Hbf", destination="Salzburg Hbf", line="REX 1")
    plain = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8))
    regional = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8), regional_only=True)
    assert RegionalOnly().apply([rj, rex], plain) == [rj, rex]
    assert RegionalOnly().apply([rj, rex], regional) == [rex]
    assert regional.leg_query("Wien", "Linz").regional_only        # hacky ptají stejně


# ---- Deutschlandticket ------------------------------------------------------------------
def dticket():
    return next(p for p in geo.PASSES if p.name == "Deutschlandticket")


def test_deutschlandticket_price_needs_everyone_to_hold_it():
    d = dticket()
    assert d.price((Passenger(age=30, cards=DT),)) == Decimal(0)
    assert d.price((Passenger(age=30, cards=DT), Passenger(age=4))) == Decimal(0)   # do 6 let zdarma
    assert d.price((Passenger(age=30, cards=DT), Passenger(age=30))) is None
    assert d.price(1) is None
    assert d.covers("Salzburg Hbf") and d.covers("München Hbf") and not d.covers("Linz/Donau Hbf")
    assert d.valid_at(at(6)) and d.valid_until(at(6), at(23, day=21))
    assert d.train_ok("RE 2") and not d.train_ok("ICE 1") and not d.train_ok("REX 1")


@pytest.mark.asyncio
async def test_deutschlandticket_inside_germany_replaces_db_ticket():
    query = SearchQuery(origin="München Hbf", destination="Augsburg Hbf", departure=at(7),
                        passengers=(Passenger(age=30, cards=DT),))
    re_ = make_journey(provider="DB", origin="München Hbf", destination="Augsburg Hbf", dep=at(8),
                       arr=at(8, 45), price=500, line="RE 9")
    ice = make_journey(provider="DB", origin="München Hbf", destination="Augsburg Hbf", dep=at(8, 10),
                       arr=at(8, 40), price=700, line="ICE 600")

    async def search(q, allow_browser=False, only=None):
        raise AssertionError("v Německu se nic nedoptává")

    found = await RegionalPassHack().find(query, [re_, ice], search)
    assert len(found) == 1 and found[0].departure == re_.departure
    assert found[0].price == Decimal(0) and "Deutschlandticket" in found[0].notes[0]
    # bez Deutschlandticketu se nenabídne (Bayern-Ticket 34 € je dražší než 500 Kč)
    plain = SearchQuery(origin="München Hbf", destination="Augsburg Hbf", departure=at(7))
    assert all(j.provider != "Deutschlandticket" for j in await RegionalPassHack().find(plain, [re_, ice], search))


@pytest.mark.asyncio
async def test_deutschlandticket_from_czechia_cd_to_border():
    query = SearchQuery(origin="Praha", destination="Regensburg Hbf", departure=at(6),
                        passengers=(Passenger(age=30, cards=DT),))
    through = Journey(provider="DB", legs=[
        Leg("Praha hl.n.", "Furth im Wald", at(7), at(9, 30), Mode.TRAIN, "ČD", "R 760"),
        Leg("Furth im Wald", "Regensburg Hbf", at(9, 45), at(11), Mode.TRAIN, "DB", "RE 25")])
    direct = make_journey(provider="ČD", origin="Praha", destination="Regensburg Hbf", dep=at(7),
                          arr=at(11), price=900, line="R 760")
    head = make_journey(provider="ČD", origin="Praha hl.n.", destination="Furth im Wald", dep=at(7),
                        arr=at(9, 30), price=400, line="R 760")

    async def search(q, allow_browser=False, only=None):
        assert only == ["ČD"]
        return [head] if q.destination == "Furth im Wald" else []

    found = await RegionalPassHack().find(query, [through, direct], search)
    best = min(found, key=lambda j: j.price)
    assert best.price == Decimal(400) and "Deutschlandticket" in best.provider
    assert "Furth im Wald" in best.notes[0]


# ---- dopravci a rozhraní -----------------------------------------------------------------
def test_db_request_flags():
    assert db.has_deutschlandticket((Passenger(age=30, cards=DT), Passenger(age=3)))
    assert not db.has_deutschlandticket((Passenger(age=30, cards=DT), Passenger(age=30)))
    assert "ICE" not in db.REGIONAL_PRODUCTS and "REGIONAL" in db.REGIONAL_PRODUCTS


@pytest.mark.asyncio
async def test_obb_shop_asks_for_regional_trains_only():
    from test_obb_prices import FakeTransport
    from vyhledavac.providers._oebb_shop import OebbShop

    bodies = []

    class Recording(FakeTransport):
        async def post(self, url, body, headers):
            bodies.append(body)
            return await super().post(url, body, headers)

    await OebbShop("ÖBB", Recording()).search("Wien", "Salzburg", at(8), pages=1, regional=True)
    assert any((b.get("filter") or {}).get("regionaltrains") is True for b in bodies)


def test_cli_and_web_options():
    args = build_parser().parse_args(["München", "Augsburg", "--dticket", "--regional"])
    assert build_passenger(args).has(DiscountCard.DE_DEUTSCHLANDTICKET) and args.regional
    q = web.parse_query({"from": "München", "to": "Augsburg", "date": "2026-10-20", "time": "08:00",
                         "regional": True, "passengers": [{"age": 30, "dTicket": True}]})
    assert q.regional_only and q.lead.has(DiscountCard.DE_DEUTSCHLANDTICKET)
    assert "regional_only" in web.SERVER_FILTERS
