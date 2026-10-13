"""Spoj z IDOS i do ciziny: pěší přesuny, kilometry (tarif ODIS), prodejci
úseků podle zemí a dělení kusu cesty v přestupu / na hranici."""

from __future__ import annotations

import datetime as dt
import gzip
from decimal import Decimal
from pathlib import Path

from vyhledavac import idos
from vyhledavac.hacks import idos_complete
from vyhledavac.ids import odis
from vyhledavac.models import DEFAULT_TZ, DiscountCard, Journey, Leg, Mode, Passenger, Stop, looks_like_walk, mark_walks
from vyhledavac.providers import _oebb_shop, ids_idos

DATA = Path(__file__).parent / "data"
DAY = dt.date(2026, 10, 15)


def page(name):
    return gzip.decompress((DATA / name).read_bytes()).decode("utf-8")


def at(h, m=0):
    return dt.datetime(2026, 10, 15, h, m, tzinfo=DEFAULT_TZ)


# ---- pěší přesuny ----------------------------------------------------------------
def test_walk_labels():
    assert all(looks_like_walk(x) for x in ("W", "Fußweg", "přesun", "Walk", "gyalog"))
    assert not any(looks_like_walk(x) for x in ("Bus 330", "R 4415", "U2", "WEST 920"))


def test_obb_walk_is_not_a_transfer():
    leg = lambda o, d, h, line, mode: Leg(o, d, at(h), at(h, 30), mode=mode, line=line, carrier="ÖBB")
    journey = Journey(provider="ÖBB", legs=[
        leg("Třeboň", "Wien Handelskai (11-12)", 8, "S 45", Mode.TRAIN),
        leg("Wien Handelskai (11-12)", "Wien Handelskai (1-2)", 9, "W", Mode.BUS),
        leg("Wien Handelskai (1-2)", "Marchegg", 10, "S 1", Mode.TRAIN)])
    mark_walks([journey])
    assert journey.legs[1].mode is Mode.WALK and journey.transfers == 1


def test_obb_shop_walk_section():
    raw = {"id": "x", "from": {"departure": "2026-10-15T08:00:00"}, "to": {"arrival": "2026-10-15T09:00:00"},
           "sections": [{"from": {"name": "A", "departure": "2026-10-15T08:00:00"},
                         "to": {"name": "B", "arrival": "2026-10-15T08:05:00"},
                         "category": {"shortName": "W", "name": "Fußweg", "train": False}}]}
    assert _oebb_shop.parse_connection(raw).sections[0].walk


# ---- IDOS: cesta do ciziny ----------------------------------------------------------
def test_idos_international_connection_with_walks():
    conns = idos.parse_results(page("idos_international.html.gz"), DAY)
    first = conns[0]
    assert [leg.line for leg in first.legs][-1] == "R 4415"
    assert first.legs[-1].destination == "Bad Ischl" and "ÖBB" in first.legs[-1].carrier
    assert [leg.walk_before for leg in first.legs] == [0, 2, 0, 0, 0]     # Borovany: přesun asi 2 min


def test_idos_detail_distances():
    detail = page("idos_detail_km.html.gz")
    assert [s[0] for s in idos.parse_stops(detail)] == ["Borovany", "České Budějovice"]
    assert idos.parse_distances(detail) == [29.0, 50.0]


def test_with_walks_keeps_ride_positions():
    conn = idos.parse_results(page("idos_international.html.gz"), DAY)[0]
    legs = [Leg(c.origin, c.destination, c.departure, c.arrival, mode=Mode.TRAIN, line=c.line) for c in conn.legs]
    shown = ids_idos.with_walks(legs, conn)
    assert [leg.mode for leg in shown].count(Mode.WALK) == 1 and len(shown) == len(legs) + 1
    assert ids_idos.rides(Journey(provider="IDS", legs=shown)) == legs


def test_head_only_keeps_following_legs():
    infos = [([None, None], None, True), ([None, None], set(), False), ([None, None], set(), False)]
    plan = [{"kind": "ids", "from": [0, 0], "to": [0, 1], "price": "49", "system": "IDESKA", "name": "x"},
            {"kind": "buy", "from": [1, 0], "to": [2, 1]}]
    assert ids_idos._head_only(plan, infos)[-1]["to"] == [2, 1]


# ---- ODIS ---------------------------------------------------------------------------
def test_odis_kilometre_tariff():
    adult, child, student = Passenger(age=40), Passenger(age=10), Passenger(age=20, cards=(DiscountCard.ISIC,))
    assert odis.fare_for_km(33, adult).price == Decimal("59.5")          # 10 + 33 × 1,50
    assert odis.fare_for_km(33, child).price == Decimal("29.8")          # 5 + 33 × 0,75 = 29,75
    assert odis.fare_for_km(33, student).price == Decimal("29.8")
    assert odis.fare_for_km(33, Passenger(age=4)).price == 0


# ---- prodejci a dělení kusu ------------------------------------------------------------
def test_sellers_by_country():
    from vyhledavac.registry import load_plugins

    load_plugins()
    train = [Leg("České Budějovice", "Linz Hbf", at(8), at(10), mode=Mode.TRAIN,
                 carrier="České dráhy, a.s. , ÖBB Personenverkehr", line="EC 331")]
    plain, browser = idos_complete.sellers_for("České Budějovice", "Bad Ischl", train)
    assert "ÖBB" in plain and "IDS" not in plain and browser == ["ČD"]
    plain, browser = idos_complete.sellers_for("Kúty", "Malacky", train)
    assert "ZSSK" in browser and "ČD" not in browser
    plain, browser = idos_complete.sellers_for("Třeboň", "Praha", train)
    assert plain == idos_complete.SELLERS


def test_split_at_transfer_and_border():
    stops = [Stop("České Budějovice", departure=at(8, 5)), Stop("Horní Dvořiště", at(8, 50), at(8, 52)),
             Stop("Summerau", at(9, 0), at(9, 5)), Stop("Linz Hbf", arrival=at(10, 6))]
    ec = Leg("České Budějovice", "Linz Hbf", at(8, 5), at(10, 6), mode=Mode.TRAIN, line="EC 331", stops=stops)
    ir = Leg("Linz Hbf", "Bad Ischl", at(12, 4), at(13, 46), mode=Mode.TRAIN, line="IR 703")
    points = idos_complete._split_points([ec, ir])
    assert points == [(0, 1), (0, 2), (1, 0)]
    head, tail = idos_complete._split([ec, ir], (0, 1))
    assert head[-1].destination == "Horní Dvořiště" and tail[0].origin == "Horní Dvořiště"
    assert tail[-1].destination == "Bad Ischl"


def test_country_from_route_context():
    from vyhledavac import geo

    assert geo.country_of("Jesenice") == "CZ"                       # obec u Prahy
    assert geo.country_near("Jesenice", {"AT", "SI"}) == "SI"       # stanice u Lublaně
    stops = [Stop("Villach Hbf", departure=at(21, 38)), Stop("Rosenbach", at(21, 55), at(21, 56)),
             Stop("Jesenice", at(22, 10), at(22, 20)), Stop("Lesce-Bled", at(22, 30), at(22, 31)),
             Stop("Ljubljana", arrival=at(23, 18))]
    d39 = Leg("Villach Hbf", "Ljubljana", at(21, 38), at(23, 18), mode=Mode.TRAIN, line="D 39", stops=stops)
    assert idos_complete._run_countries([d39]) == {"AT", "SI"}
    assert idos_complete._split_points([d39]) == [(0, 2)]          # lom v Jesenicích (Rosenbach neznáme)


def test_cd_sells_far_abroad_and_city_names():
    from vyhledavac.registry import load_plugins

    load_plugins()
    ec = [Leg("České Budějovice", "Ljubljana", at(8), at(16), mode=Mode.TRAIN, carrier="České dráhy", line="EC 331")]
    _, browser = idos_complete.sellers_for("České Budějovice", "Ljubljana", ec, {"CZ", "SI"})
    assert browser == ["ČD"]
    bus = [Leg("České Budějovice, aut.nádr.", "Salzburg South", at(9, 5), at(12, 50), mode=Mode.BUS,
               carrier="FlixBus CZ s.r.o.", line="Bus 861")]
    plain, browser = idos_complete.sellers_for("České Budějovice", "Salcburk", bus, {"CZ", "AT"})
    assert "FlixBus" in plain and browser == ["ČD"]                 # do ciziny i vlak ČD k porovnání
    _, browser = idos_complete.sellers_for("Třeboň", "Praha", bus, {"CZ"})
    assert browser == []                                            # autobus po Česku: ČD ne
    assert idos_complete._query_name("České Budějovice, aut.nádr.", True) == "České Budějovice"
    assert idos_complete._query_name("Salzburg South", True) == "Salcburk"


def test_marked_country():
    from vyhledavac import geo

    assert geo.country_of("Rybnik(PL)") == "PL" and geo.country_of("Jesenice(SL)") == geo.country_of("Jesenice(SLO)") == "SI"
    assert not idos_complete._marked_ok("Rybnik(PL)", "CZ") and idos_complete._marked_ok("Jesenice(SL)", "SI")
