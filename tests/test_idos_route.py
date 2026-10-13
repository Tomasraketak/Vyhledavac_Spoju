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


def test_idos_suggest_picks_foreign_city():
    text = ('cb([{"selectedText":"Košice; Slovensko","description":"obec, Slovensko, okres Košice, vlaky"},'
            '{"selectedText":"Košice; okres Kutná Hora","description":"obec, okres Kutná Hora, autobusy"},'
            '{"selectedText":"Amsterdam; Nizozemí","description":"obec, Nizozemí, vlaky, autobusy"}])')
    items = idos.parse_suggest(text)
    assert idos.pick_foreign(items, "Slovensko") == "Košice; Slovensko"
    assert idos.pick_foreign(items, "Nizozemsko") == "Amsterdam; Nizozemí"      # IDOS píše „Nizozemí“
    assert idos.pick_foreign(items, "Polsko") is None


def test_idos_price_offer():
    price, parts = idos.parse_offer('cb({"price":"2 117   Kč","errorMsg":null,'
                                    '"sellablePartsDesc":"České Budějovice - Linz Hbf<br>Salzburg Hbf - Ljubljana"});')
    assert price == Decimal(2117) and parts == [("České Budějovice", "Linz Hbf"), ("Salzburg Hbf", "Ljubljana")]
    assert idos.parse_offer('cb({"price":"286   Kč","sellablePartsDesc":null});') == (Decimal(286), None)
    assert idos.parse_offer('cb({"price":null,"errorMsg":"nelze"});') == (None, None)
    conns = idos.parse_results(page("idos_international.html.gz"), DAY)
    assert conns[0].handle and conns[0].conn_id and conns[0].conn_id != conns[1].conn_id


# ---- skutečné přestupy mezi jízdenkami ----------------------------------------------
def test_same_station_spellings():
    from vyhledavac.transfer import same_station

    assert same_station("Linz/Donau Hbf", "Linz Hbf")
    assert same_station("Wien Hbf (Bahnsteige 3-12)", "Wien Hauptbahnhof")
    assert same_station("S+U Berlin Hauptbahnhof", "Berlin Hbf")
    assert same_station("Bad Ischl Bahnhof (Zugang Bahnsteig 1)", "Bad Ischl")
    assert not same_station("Berlin Südkreuz", "Berlin Hbf")
    assert not same_station("Praha hl.n.", "Praha-Holešovice")
    assert not same_station("České Budějovice", "České Budějovice, aut.nádr.")
    assert not same_station("Budapest-Keleti", "Budapest-Nyugati")


def test_reachable_uses_real_transfer(monkeypatch):
    import asyncio

    from vyhledavac import transfer

    async def fake(a, b, when):
        return dt.timedelta(minutes=22)               # Südkreuz → Hbf S-Bahnem

    monkeypatch.setattr(transfer, "transfer_time", fake)
    run = lambda *args: asyncio.run(idos_complete._reachable(*args))
    assert not run(at(10), "Berlin Südkreuz", "Berlin Hbf", at(10, 15))      # dřív stačilo 10 min
    assert run(at(10), "Berlin Südkreuz", "Berlin Hbf", at(10, 25))
    assert run(at(10), "Wien Hbf", "Wien Hauptbahnhof", at(10, 2))            # tatáž stanice
    assert not run(at(10), "Wien Hbf", "Wien Hauptbahnhof", at(10, 2), idos_complete.TRANSFER_SAME)


def test_lonely_namesake_is_not_a_border():
    stops = [Stop("Zagreb Glavni kolodvor", departure=at(10, 28)), Stop("Savski Marof", at(10, 50), at(10, 51)),
             Stop("Dobova", at(11, 5), at(11, 9)), Stop("Sevnica", at(11, 40), at(11, 43)),
             Stop("Blanca", at(11, 50), at(11, 51)), Stop("Sava", at(12, 20), at(12, 21)),
             Stop("Litija", at(12, 34), at(12, 35)), Stop("Ljubljana", arrival=at(13, 7))]
    rer = Leg("Zagreb Glavni kolodvor", "Ljubljana", at(10, 28), at(13, 7), mode=Mode.TRAIN, line="RER 432",
              stops=stops)
    assert idos_complete._split_points([rer]) == [(0, 1), (0, 2)]                # jen Savski Marof | Dobova


def test_home_country_sellers():
    from vyhledavac.registry import load_plugins

    load_plugins()
    rer = [Leg("Zagreb Glavni kolodvor", "Ljubljana", at(10, 28), at(13, 7), mode=Mode.TRAIN, line="RER 432")]
    plain, _ = idos_complete.sellers_for("Záhřeb", "Lublaň", rer, {"HR", "SI"})
    assert "ÖBB" in plain and "MÁV" not in plain                  # MÁV jen z/do Maďarska
    ec = [Leg("Kraków Główny", "Wien Hauptbahnhof", at(13, 52), at(19, 49), mode=Mode.TRAIN, line="EC 207",
              stops=[Stop("Kraków Główny", departure=at(13, 52)), Stop("Bohumín", at(16, 30), at(16, 34)),
                     Stop("Břeclav", at(18, 38), at(18, 40)), Stop("Wien Hauptbahnhof", arrival=at(19, 49))])]
    plain, browser = idos_complete.sellers_for("Krakov", "Vídeň", ec, {"PL", "AT"})
    assert "ÖBB" in plain and browser == ["ČD"]                    # ČD prodá i vlak přes Česko


def test_browser_learns_countries_without_prices():
    hack = idos_complete.IdosCompleteHack()
    assert not hack._browser_unpriced(("SE", "CZ"))
    hack._learn_unpriced(("SE", "CZ"))                    # Malmö → Břeclav: ČD bez ceny
    assert hack._browser_unpriced(("SE", "AT")) and hack._browser_unpriced(("DE", "SE"))
    assert not hack._browser_unpriced(("PL", "CZ"))
    hack._learn_unpriced(("CZ", "CZ"))
    assert not hack._browser_unpriced(("CZ", "AT")) and not hack._browser_unpriced(("CZ", "CZ"))


def test_split_train_shown_once():
    a = Leg("Koebenhavn H", "Břeclav", at(8, 22), at(22, 47), mode=Mode.TRAIN, line="rj 177 Berliner")
    b = Leg("Břeclav", "Wien Hbf", at(22, 55), at(23, 49), mode=Mode.TRAIN, line="RJ 177")
    c = Leg("Wien Hbf", "Graz Hbf", at(23, 58), at(23, 59), mode=Mode.TRAIN, line="RJ 559")
    joined = idos_complete._join_split_trains([a, b, c])
    assert [(leg.origin, leg.destination) for leg in joined] == [("Koebenhavn H", "Wien Hbf"), ("Wien Hbf", "Graz Hbf")]
