"""Jízdenky IDS: tarif IDESKA, přesahy, trasa vlaku a hack s lomem na hranici systému.

Síť se nevolá – trasa vlaku z cd.cz je nahrazená prázdnou odpovědí, takže
hack použije síť z jízdního řádu (data v repozitáři), a „ČD“ pro druhý
úsek je falešný hledač.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac import cdtrain, railnet, stations
from vyhledavac.engine import SearchEngine
from vyhledavac.hacks import ids_ticket
from vyhledavac.hacks.ids_ticket import IdsTicketHack
from vyhledavac.ids import ideska, vdv
from vyhledavac.models import DiscountCard, Passenger, SearchQuery
from vyhledavac.providers.base import Provider
from tests.conftest import at, make_journey


# ---- tarif ---------------------------------------------------------------------
def test_ideska_fare_by_zones_and_validity():
    fare = ideska.best_fare("326", "932", Passenger(age=30), 60)      # Třeboň -> Počátky
    assert fare is not None and fare.price > 0 and fare.minutes >= 60
    city = ideska.best_fare("100", "100", Passenger(age=30), 25)       # jen po Budějovicích
    assert city is not None and city.price < fare.price


def test_ideska_profiles():
    adult = ideska.best_fare("100", "200", Passenger(age=30), 60)
    child = ideska.best_fare("100", "200", Passenger(age=12), 60)
    assert child.price < adult.price
    assert ideska.best_fare("100", "200", Passenger(age=4), 60).price == 0


def test_validity_too_short_gives_no_fare():
    assert ideska.best_fare("100", "100", Passenger(age=30), 24 * 60 * 10) is None


# ---- přesahy a síť -------------------------------------------------------------
@pytest.mark.parametrize("station, zone", [
    ("Počátky-Žirovnice", "932"), ("Benešov u Prahy", "425"),
    ("Nepomuk", "977"), ("Horažďovice předměstí", "973"), ("Třeboň", "326"),
])
def test_ideska_overlaps(station, zone):
    assert zone in stations.place(station).systems["IDESKA"]


def test_havlickuv_brod_is_outside_ideska():
    assert "IDESKA" not in stations.place("Havlíčkův Brod").systems


def test_rail_route_follows_real_line():
    route = railnet.route("Třeboň", "Havlíčkův Brod")
    assert route[0] == "Třeboň" and route[-1] == "Havlíčkův Brod"
    assert "Počátky-Žirovnice" in route and "Praha hl. n." not in route


def test_domestic_detection():
    assert stations.is_domestic("Praha", "Brno")
    assert not stations.is_domestic("Praha", "Vídeň")


def test_cd_train_page_parsing():
    html = ('{"route":[{"name":"Praha hl.n.","timeArr":null,"timeDep":"18:14",'
            '"stationUrl":"/stanice/5457076/","stationInfo":[{"text":"x"}]},'
            '{"name":"Nymburk hl.n.","timeArr":"18:53","timeDep":"18:55","stationUrl":"/stanice/5453214/"},'
            '{"name":"Hradec Králové hl.n.","timeArr":"19:50","timeDep":null,"stationUrl":"/stanice/5453120/"}]}')
    stops = cdtrain.parse(html, at(0))
    assert [s.name for s in stops] == ["Praha hl.n.", "Nymburk hl.n.", "Hradec Králové hl.n."]
    assert stops[1].arrival == at(18, 53) and stops[0].code == "5457076"
    assert cdtrain.train_number("R 933 Krakonoš") == "933"


# ---- hack ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def offline_cd(monkeypatch):
    async def no_route(*args, **kwargs):
        return []

    async def no_prices(pairs):
        return None

    monkeypatch.setattr(cdtrain, "train_stops", no_route)
    from vyhledavac.ids import online
    for tariff in (online.vdv, online.iredo, online.idol, online.duk, online.idzk):  # počítadla se nevolají
        monkeypatch.setattr(tariff, "prefetch", no_prices)
        monkeypatch.setattr(tariff, "_prices", {})


def cd_journey(origin, destination, dep, arr, price, line="Os 8800", stops=None):
    """Spoj ČD; ``stops`` = stanice, kde vlak zastavuje (jen tam smí být lom)."""
    j = make_journey(provider="ČD", origin=origin, destination=destination,
                     dep=dep, arr=arr, price=price, line=line, stops=stops)
    j.legs[0].carrier = "ČD"
    return j


@pytest.mark.asyncio
async def test_split_at_ideska_boundary():
    query = SearchQuery(origin="Třeboň", destination="Havlíčkův Brod", departure=at(9))
    base = cd_journey("Třeboň", "Havlíčkův Brod", at(10), at(12, 30), 250,
                      stops=["Třeboň", "Veselí nad Lužnicí", "Jindřichův Hradec", "Počátky-Žirovnice",
                             "Horní Cerekev", "Havlíčkův Brod"])
    calls = []

    async def search(q, allow_browser=False):
        calls.append((q.origin, q.destination, allow_browser))
        rest = cd_journey(q.origin, q.destination, at(11, 20), at(12, 30), 90)
        return [rest]

    found = await IdsTicketHack().find(query, [base], search)
    # zbytek cesty u ČD (VDV tu nemá ceny); jediný úsek na stanici lomu, s prohlížečem
    assert ("Počátky-Žirovnice", "Havlíčkův Brod", True) in calls
    assert len(calls) <= 2
    assert len(found) == 1
    hacked = found[0]
    first, second = hacked.segments
    assert first.provider == "IDESKA" and first.destination == "Počátky-Žirovnice"
    assert second.origin == "Počátky-Žirovnice" and second.price == Decimal(90)
    assert hacked.price == first.price + second.price < Decimal(250)
    assert "Nepřestupujete" in hacked.notes[0] and "ušetříte" in hacked.notes[0]
    assert hacked.legs == base.legs                 # pořád tentýž vlak


@pytest.mark.asyncio
async def test_whole_trip_inside_ideska():
    query = SearchQuery(origin="České Budějovice", destination="Tábor", departure=at(9))
    base = cd_journey("České Budějovice", "Tábor", at(10), at(11), 160)

    async def search(q, allow_browser=False):
        raise AssertionError("celá cesta v IDESKA nepotřebuje další dotaz")

    found = await IdsTicketHack().find(query, [base], search)
    assert len(found) == 1 and found[0].segments[0].provider == "IDESKA"
    assert found[0].price < Decimal(160)


@pytest.mark.asyncio
async def test_not_offered_when_not_cheaper_or_express():
    query = SearchQuery(origin="České Budějovice", destination="Tábor", departure=at(9))

    async def search(q, allow_browser=False):
        return []

    cheap = cd_journey("České Budějovice", "Tábor", at(10), at(11), 20)
    express = cd_journey("České Budějovice", "Tábor", at(10), at(11), 300, line="EC 335")
    assert await IdsTicketHack().find(query, [cheap, express], search) == []


@pytest.mark.asyncio
async def test_ideska_valid_on_intercity_but_pid_is_not():
    # IDESKA platí i ve vybraných dálkových vlacích ČD (trať 220 až do Benešova)
    query = SearchQuery(origin="České Budějovice", destination="Tábor", departure=at(9))

    async def search(q, allow_browser=False):
        return []

    ic = cd_journey("České Budějovice", "Tábor", at(10), at(11), 300, line="IC 536")
    (found,) = await IdsTicketHack().find(query, [ic], search)
    assert found.provider == "IDESKA" and found.price < Decimal(300)
    from vyhledavac.hacks.ids_ticket import train_systems
    assert train_systems("IC 536") == {"IDESKA"} and train_systems("EC 335") == set()
    assert train_systems("R 722") is None


# ---- engine: vnitrostátní cesta bez ÖBB a ZSSK -------------------------------------
class Fake(Provider):
    def __init__(self, name, international_only=False, **kw):
        super().__init__(**kw)
        self.name = name
        self.international_only = international_only
        self.asked = 0

    async def search(self, query):
        self.asked += 1
        return [make_journey(provider=self.name, price=100)]


@pytest.mark.asyncio
async def test_domestic_trip_skips_international_providers():
    cd, obb = Fake("ČD"), Fake("ÖBB", international_only=True)
    seen = []
    engine = SearchEngine([cd, obb])
    await engine.search(SearchQuery(origin="Praha", destination="Brno", departure=at(8)),
                        on_partial=seen.append, enable_hacks=False)
    assert cd.asked == 1 and obb.asked == 0
    assert any(r.provider == "ÖBB" and r.skipped for r in seen)

    await engine.search(SearchQuery(origin="Praha", destination="Vídeň", departure=at(8)),
                        enable_hacks=False)
    assert obb.asked == 1


# ---- PID a IDPK ---------------------------------------------------------------------
from vyhledavac.ids import idpk, pid  # noqa: E402


def test_pid_counts_consecutive_zones():
    assert pid.zone_count("P", "4") == 7
    fare = pid.best_fare("P", "4", Passenger(age=30), 40)
    assert fare.price == Decimal(84) and fare.minutes >= 40
    assert pid.best_fare("P", "4", Passenger(age=21, cards=(DiscountCard.ISIC,)), 40).price == Decimal(42)
    city = pid.best_fare("P", "0", Passenger(age=30), 20)            # jen po Praze
    assert city.price == Decimal(36)


def test_idpk_price_formula_from_tariff():
    zones = ["052", "051", "063", "062", "061", "001"]
    full = min(idpk.fares_for_path(zones, Passenger(age=30)), key=lambda f: f.price)
    reduced = min(idpk.fares_for_path(zones, Passenger(age=12)), key=lambda f: f.price)
    assert full.price == Decimal(14 + 12 + 10 + 10 + 10 + 18) and full.minutes == 180
    assert reduced.price == Decimal(7 + 6 + 5 + 5 + 5 + 12)
    seven = ["021", "022", "023", "024", "025", "026", "027"]
    assert min(f.price for f in idpk.fares_for_path(seven, Passenger(age=30))) == Decimal(84)


def test_idpk_path_prefers_fewest_zones_and_plzen():
    assert idpk.path_zones([("052",), ("052", "051"), ("051",), ("001", "061")]) == ["052", "051", "001"]


@pytest.mark.parametrize("station, system, zone", [
    ("Plzeň hl. n.", "IDPK", "001"), ("Klatovy", "IDPK", "084"),
    ("Horažďovice předměstí", "IDPK", "052"), ("Beroun", "PID", "4"),
])
def test_rail_station_zones(station, system, zone):
    assert zone in stations.rail_place(station).systems[system]


def test_physical_route_includes_stations_express_skips():
    route = railnet.physical_route("Nepomuk", "Plzeň hl. n.")
    assert "Blovice" in route and "Starý Plzenec" in route


@pytest.mark.asyncio
async def test_two_systems_in_a_row():
    query = SearchQuery(origin="České Budějovice", destination="Plzeň", departure=at(5))
    base = cd_journey("České Budějovice", "Plzeň", at(6), at(8), 300,
                      stops=["České Budějovice", "Strakonice", "Horažďovice předměstí", "Nepomuk", "Plzeň"])

    async def search(q, allow_browser=False):
        return []

    found = await IdsTicketHack().find(query, [base], search)
    assert len(found) == 1
    assert [s.provider for s in found[0].segments] == ["IDESKA", "IDPK"]
    assert found[0].price < Decimal(300)


# ---- VDV ---------------------------------------------------------------------------
VDV_HTML = ("<table><tr><td>Plné jednotlivé VDV</td><td>240 min</td><td>88,00 Kč</td>"
            "<td>88,00 Kč</td></tr><tr><td>Student jednotlivé VDV</td><td>240 min</td>"
            "<td>44,00 Kč</td><td>44,00 Kč</td></tr><tr><td>24 hodinová celosíťová VDV</td>"
            "<td>1440 min</td><td>150,00 Kč</td><td>150,00 Kč</td></tr></table>")


def test_vdv_parse_calculator_table():
    rows = vdv.parse(VDV_HTML)
    assert ("Plné jednotlivé VDV", 240, Decimal("88.00")) in rows
    assert ("24 hodinová celosíťová VDV", 1440, Decimal("150.00")) in rows


@pytest.mark.asyncio
async def test_ideska_then_vdv(monkeypatch):
    from vyhledavac.ids import online
    monkeypatch.setitem(online.vdv._prices, ("758", "600"), online.vdv.parse(VDV_HTML))
    query = SearchQuery(origin="Třeboň", destination="Havlíčkův Brod", departure=at(5))
    base = cd_journey("Třeboň", "Havlíčkův Brod", at(6, 6), at(8, 51), 269,
                      stops=["Třeboň", "Veselí n.Lužnicí", "Jindřichův Hradec", "Počátky-Žirovnice",
                             "Horní Cerekev", "Jihlava", "Havlíčkův Brod"])

    async def search(q, allow_browser=False):
        return []

    found = await IdsTicketHack().find(query, [base], search)
    assert [s.provider for s in found[0].segments] == ["IDESKA", "VDV"]
    assert found[0].segments[0].destination == "Počátky-Žirovnice"
    assert found[0].price == Decimal(81) + Decimal(88)


# ---- IREDO, IDOL, IDS JMK, řetěz systémů --------------------------------------------
from vyhledavac.ids import jmk, online  # noqa: E402

IREDO_HTML = ("<tr><td>plná</td><td>180 min</td><td>54,00 Kč</td><td>46,00 Kč</td></tr>"
              "<tr><td>student 18-26 let</td><td>180 min</td><td>27,00 Kč</td><td>23,00 Kč</td></tr>"
              "<tr><td>7denní plná</td><td>7 d</td><td>368,00 Kč</td><td>368,00 Kč</td></tr>")
IDOL_HTML = ("<tr><td>dospělý 18+</td><td>Jednotlivé jízdné</td><td>210 min</td><td>106,-</td>"
             "<td>84,-</td><td>84,-</td><td>84,-</td></tr><tr><td>student -26</td>"
             "<td>Jednotlivé jízdné</td><td>210 min</td><td>53,-</td><td>42,-</td></tr>")


def test_iredo_takes_cheapest_column(monkeypatch):
    monkeypatch.setitem(online.iredo._prices, ("600", "100"), online.iredo.parse(IREDO_HTML))
    assert online.iredo.best_fare("600", "100", Passenger(age=30), 60).price == Decimal("46.00")
    student = Passenger(age=21, cards=(DiscountCard.ISIC,))
    assert online.iredo.best_fare("600", "100", student, 60).price == Decimal("23.00")


def test_idol_parse_single_fares():
    rows = online.idol.parse(IDOL_HTML)
    assert ("dospělý 18+", 210, Decimal(84)) in rows and ("student -26", 210, Decimal(42)) in rows


def test_jmk_price_by_zone_count():
    zones = ["100", "101", "310", "320"]
    assert jmk.best_for_route([(z,) for z in zones], Passenger(age=30), 60).price == Decimal(37)
    assert jmk.best_for_route([(z,) for z in zones], Passenger(age=12), 60).price == Decimal("18.50")


@pytest.mark.asyncio
async def test_vdv_then_jmk_chain(monkeypatch):
    """Žďár nad Sázavou → Brno: VDV do Křižanova, dál IDS JMK."""
    monkeypatch.setitem(online.vdv._prices, ("400", "347"), online.vdv.parse(VDV_HTML))
    query = SearchQuery(origin="Žďár nad Sázavou", destination="Brno", departure=at(9))
    base = cd_journey("Žďár nad Sázavou", "Brno", at(10), at(11, 10), 200,
                      stops=["Žďár nad Sázavou", "Křižanov", "Tišnov", "Brno hl. n."])

    async def search(q, allow_browser=False):
        return []

    found = await IdsTicketHack().find(query, [base], search)
    assert [s.provider for s in found[0].segments] == ["VDV", "IDS JMK"]
    assert found[0].segments[0].destination == "Křižanov"


@pytest.mark.asyncio
async def test_group_sums_fares_per_passenger():
    """Dospělý + dítě + student + batole: každý svou jízdenku, cena se sečte."""
    from vyhledavac.models import DiscountCard

    people = (Passenger(age=40), Passenger(age=10), Passenger(age=21, cards=(DiscountCard.ISIC,)),
              Passenger(age=3))
    query = SearchQuery(origin="České Budějovice", destination="Tábor", departure=at(9),
                        passengers=people)
    single = [await IdsTicketHack().find(SearchQuery(origin="České Budějovice", destination="Tábor",
                                                     departure=at(9), passengers=(p,)),
                                         [cd_journey("České Budějovice", "Tábor", at(10), at(11), 999)],
                                         None) for p in people]
    found = await IdsTicketHack().find(query, [cd_journey("České Budějovice", "Tábor", at(10), at(11),
                                                          999)], None)
    expected = sum(s[0].price for s in single)
    assert found[0].price == expected
    assert single[3][0].price == 0                       # do 6 let zdarma
    assert single[1][0].price < single[0][0].price       # dítě má slevu


def test_pid_free_in_prague_for_children_and_seniors():
    assert pid.best_fare("P", "B", Passenger(age=10), 30).price == 0
    assert pid.best_fare("P", "0", Passenger(age=72), 30).price == 0
    assert pid.best_fare("P", "3", Passenger(age=10), 30).price > 0       # mimo Prahu jen sleva


def test_idsok_ceník_a_b_and_regional_710():
    from vyhledavac.ids import idsok

    adult, junior, student = Passenger(age=30), Passenger(age=12), Passenger(age=20, cards=(DiscountCard.ISIC,))
    assert idsok.best_for_route([("1",), ("12",), ("17",)], adult, 60).price == Decimal(25)
    assert idsok.best_for_route([("1",), ("12",), ("17",)], junior, 60).price == Decimal(6)
    # přes Olomouc: vlakem stačí regionální zóna 710 (Ceník A), s MHD by to byl Ceník B
    via = [("72",), ("71",), ("75",)]
    assert idsok.best_for_route(via, adult, 60).price == Decimal(25)
    assert any(f.price == Decimal(43) for f in idsok.fares_for_path(["72", "71", "75"], adult))
    assert any(f.price == Decimal(29) for f in idsok.fares_for_path(["72", "71", "75"], student))
    assert idsok.best_for_route([("71",)], adult, 30) is None           # jen MHD Olomouc


def test_duk_parse_takes_cheapest_offer_per_level():
    data = {"coverages": [
        {"isNetwork": False, "offers": [
            {"price": 5900, "cp": 1, "format": "Paper", "products": [{"validDurationMinutes": 120}]},
            {"price": 5310, "cp": 1, "format": "MobileApp", "products": [{"validDurationMinutes": 120}]},
            {"price": 2650, "cp": 6, "format": "MobileApp", "products": [{"validDurationMinutes": 120}]},
            {"price": 1320, "cp": 53, "products": [{"validDurationMinutes": 120}]}]},
        {"isNetwork": True, "offers": [{"price": 20000, "cp": 1, "products": [{}]}]}]}
    rows = online.duk.parse(data)
    assert ("jednotlivé plné", 120, Decimal("53.1")) in rows
    assert ("jednotlivé zvýhodněné 50 %", 120, Decimal("26.5")) in rows
    assert len(rows) == 2


def test_idzk_parse_city_ticket_variants():
    html = ('<p class="thePrice">18<span> Kč<em> &nbsp;/ &nbsp;Platnost jízdenky 30 min</em></span><br />'
            '24<span> Kč<em> &nbsp;/ &nbsp;Platnost jízdenky 60 min</em></span></p>')
    assert online.idzk.parse(html) == [(30, Decimal(18)), (60, Decimal(24))]


def test_pid_prague_single_fares_by_age():
    """Jen po Praze: junioři a studenti platí plné, senioři 60–65 zvýhodněné (Tarif PID 2026)."""
    junior, student = Passenger(age=16), Passenger(age=20, cards=(DiscountCard.ISIC,))
    assert pid.best_fare("P", "P", junior, 20).price == Decimal(36)
    assert pid.best_fare("P", "P", student, 20).price == Decimal(36)
    assert pid.best_fare("P", "P", Passenger(age=62), 20).price == Decimal(18)
    assert pid.best_fare("P", "3", junior, 40).price == Decimal(36)     # v regionu sleva (6 pásem)


def _route(*stops):
    return [ids_ticket.RouteStop(name, at(10) + dt.timedelta(minutes=5 * k), {system: zones},
                                 "Jihočeský kraj", name)
            for k, (name, system, zones) in enumerate(stops)]


def test_season_pass_splits_route_into_uncovered_runs(monkeypatch):
    monkeypatch.setattr(railnet, "physical_route", lambda a, b: [a, b])
    route = _route(("A", "IDESKA", ("100",)), ("B", "IDESKA", ("200",)),
                   ("C", "IDESKA", ("300",)), ("D", "IDESKA", ("400",)))
    runs = ids_ticket._uncovered_runs(route, "IDESKA", 0, 3, frozenset({"100", "200"}))
    assert [(a, b, z) for a, b, z in runs] == [(2, 3, [("300",), ("400",)])]
    assert ids_ticket._uncovered_runs(route, "IDESKA", 0, 3, frozenset({"*"})) == []
    middle = ids_ticket._uncovered_runs(route, "IDESKA", 0, 3, frozenset({"100", "400"}))
    assert [(a, b) for a, b, _ in middle] == [(1, 2)]


def test_season_pass_prices_only_zones_outside(monkeypatch):
    monkeypatch.setattr(railnet, "physical_route", lambda a, b: [a, b])
    route = _route(("Praha", "PID", ("P",)), ("Libeň", "PID", ("0",)), ("Klánovice", "PID", ("B",)),
                   ("Úvaly", "PID", ("1",)), ("Tuklaty", "PID", ("2",)))
    for stop in route:
        stop.kraj = "Středočeský kraj"
    hack = IdsTicketHack()
    adult = Passenger(age=30)
    full = hack._passenger_fare(route, "PID", 0, 4, 40, adult)
    assert full.price == Decimal(60)                                   # P–2 = 5 pásem
    prague = Passenger(age=30, passes=(("PID", ("P", "0", "B")),))
    rest = hack._passenger_fare(route, "PID", 0, 4, 40, prague)
    assert rest.price == Decimal(24) and "předplatné" in rest.name     # doplatek 1–2
    covered = Passenger(age=30, passes=(("PID", ("*",)),))
    assert hack._passenger_fare(route, "PID", 0, 4, 40, covered).price == 0


@pytest.mark.asyncio
async def test_season_pass_in_whole_hack():
    query = SearchQuery(origin="České Budějovice", destination="Tábor", departure=at(9),
                        passengers=(Passenger(age=30, passes=(("IDESKA", ("*",)),)),))
    found = await IdsTicketHack().find(query, [cd_journey("České Budějovice", "Tábor", at(10),
                                                          at(11), 999)], None)
    assert found and found[0].price == 0


@pytest.mark.asyncio
async def test_no_split_where_train_only_passes(monkeypatch):
    """Lom jen ve stanici, kde vlak staví (SPPO ČD): R jede Náměšť n.O. – Brno bez
    zastavení v Kralicích, takže tam jízdenka VDV končit nesmí."""
    monkeypatch.setitem(online.vdv._prices, ("758", "303"), online.vdv.parse(VDV_HTML))
    query = SearchQuery(origin="Počátky-Žirovnice", destination="Brno", departure=at(9))
    base = cd_journey("Počátky-Žirovnice", "Brno", at(10), at(12), 400, line="R 653",
                      stops=["Počátky-Žirovnice", "Horní Cerekev", "Jihlava", "Třebíč",
                             "Náměšť n.Oslavou", "Brno hl. n."])

    async def search(q, allow_browser=False):
        return []

    found = await IdsTicketHack().find(query, [base], search)
    for journey in found:
        assert all(s.origin != "Kralice nad Oslavou" and s.destination != "Kralice nad Oslavou"
                   for s in journey.segments)
    route = [ids_ticket._stop(n) for n in railnet.route("Počátky-Žirovnice", "Brno")]
    assert any(s.name == "Kralice nad Oslavou" for s in route)      # síť ji zná…
    ids_ticket._mark_halts(route, {ids_ticket._station_key(n) for n in base.legs[0].stop_names()})
    halts = {s.name for s in route if s.halts}
    assert "Kralice nad Oslavou" not in halts and "Náměšť nad Oslavou" in halts   # …ale vlak tam nestaví
