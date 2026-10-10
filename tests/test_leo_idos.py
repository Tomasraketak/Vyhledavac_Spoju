"""Leo Express (GraphQL) a spoje IDS z IDOS."""

import datetime as dt
import gzip
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac import idos
from vyhledavac.cache import MemoryCache
from vyhledavac.models import DiscountCard, Mode, Passenger, SearchQuery
from vyhledavac.providers import ids_idos, leo
from vyhledavac.providers.ids_idos import IdsIdosProvider

from conftest import at

DATA = Path(__file__).parent / "data"

STATIONS = [
    {"id": "5457076", "name": "Praha hl.n.", "name_master": "Praha hl.n.", "type": "train",
     "parent_station": {"short": "PRAHA", "name": "Praha"}},
    {"id": "5100028", "name": "Krakov Główny", "name_master": "Kraków Główny", "type": "train",
     "parent_station": None},
    {"id": "64663", "name": "Krakov, aut. st.", "name_master": "Kraków, dw. aut.", "type": "bus",
     "parent_station": None},
]


def test_leo_only_where_it_runs():
    assert leo.pick_station("Praha", STATIONS) == ("PRAHA", "Praha")
    assert leo.pick_station("Praha hl.n.", STATIONS) == ("5457076", "Praha hl.n.")
    assert leo.pick_station("Kraków", STATIONS) == ("5100028", "Krakov Główny")   # vlak má přednost
    assert leo.pick_station("Brno", STATIONS) is None                          # Leo tam nejezdí


@pytest.mark.parametrize("age, isic, name", [(3, False, "child"), (12, False, "child6"),
                                             (20, True, "student"), (20, False, "adult"),
                                             (70, False, "senior")])
def test_leo_tariffs(age, isic, name):
    assert leo.tariff(Passenger(age=age, cards=(DiscountCard.ISIC,) if isic else ())) == name


def test_leo_cheapest_available_class():
    raw = {"classes": [{"id": "3", "name": "Economy"}, {"id": "1", "name": "Business"}],
           "class_info": [{"record_id": "1", "total": 779, "capacity": 8},
                          {"record_id": "3", "total": 309, "capacity": 0},
                          {"record_id": "4", "total": 499, "capacity": 13}]}
    assert leo.cheapest_class(raw) == (Decimal(499), "", 13)     # Economy vyprodaná


# ---- IDOS ---------------------------------------------------------------------------
def _page(name):
    return gzip.open(DATA / name, "rt", encoding="utf-8").read()


def test_idos_results_parse_legs_and_transfers():
    conns = idos.parse_results(_page("idos_results.html.gz"), dt.date(2026, 10, 20))
    assert len(conns) == 3
    bus = conns[1].legs[0]
    assert bus.line == "Bus 300" and bus.is_bus and bus.origin == "Třeboň,,aut.nádr."
    assert bus.departure.hour == 6 and bus.departure.minute == 5
    train = conns[2].legs
    assert [l.train_label for l in train] == ["R 730 Lužnice", "R 653 Rožmberk"]
    assert train[0].destination == "Veselí n.Lužnicí" and not train[0].is_bus


def test_idos_detail_line_system_and_route():
    page = _page("idos_detail.html.gz")
    assert idos.parse_line_info(page) == ("340300", {"IDESKA"})
    route = idos.parse_route(page)
    assert route[0] == "Třeboň,,aut.nádr." and route[-1] == "Jindřichův Hradec,,Pravdova"
    assert len(route) == 16


def test_ids_line_membership():
    assert ids_idos.line_systems("", {"IDESKA"}) == {"IDESKA"}
    assert ids_idos.line_systems("340300", set()) == {"IDESKA"}
    assert ids_idos.line_systems("320927", set()) == set()          # dálkový autobus mimo IDS


@pytest.mark.asyncio
async def test_ids_provider_skips_places_outside_one_system():
    from vyhledavac.errors import StationNotFoundError

    provider = IdsIdosProvider(cache=MemoryCache())
    with pytest.raises(StationNotFoundError):
        await provider.search(SearchQuery(origin="Plzeň", destination="Praha", departure=at(6)))


@pytest.mark.asyncio
async def test_ids_provider_prices_ideska_bus(monkeypatch):
    provider = IdsIdosProvider(cache=MemoryCache())
    pages = {"results": _page("idos_results.html.gz"), "detail": _page("idos_detail.html.gz"),
             "long": _page("idos_detail_long.html.gz")}

    class Client:
        async def get_text(self, url, **kwargs):
            if "/draha/" not in url:
                return pages["results"]
            return pages["long"] if "from=36746" in url else pages["detail"]

    provider._client = Client()
    query = SearchQuery(origin="Třeboň", destination="Jindřichův Hradec",
                        departure=dt.datetime(2026, 10, 20, 6, 0, tzinfo=at(6).tzinfo))
    journeys = await provider.search(query)
    buses = {j.legs[0].line: j for j in journeys if j.legs[0].mode is Mode.BUS}
    assert buses["Bus 300"].price > 0 and "IDESKA" in buses["Bus 300"].fares[0].tariff
    # dálkový autobus IDS neplatí: spoj zůstane bez ceny, úsek čeká na hack idos_complete
    assert buses["Bus 320927 101"].price is None
    (plan,) = buses["Bus 320927 101"].raw["plans"]
    assert [seg["kind"] for seg in plan] == ["buy"] and plan[0]["from"] == [0, 0]


def test_idos_leg_after_walk_is_separate():
    page = '''<div class="box connection detail-box">
    <div class="outside-of-popup"><h3 title="místní autobus (x)"><span>Bus 350</span></h3>
      <p class="reset time" >6:05</p><p class="station"><strong class="name ">Domanín,,Vrchy</strong></p>
      <p class="reset time" >6:38</p><p class="station"><strong class="name ">Borovany,,žel.st.</strong></p></div>
    <div class="outside-of-popup outside-of-popup--with-link-dist"><div class="walk walk--detail"></div>
      <h3 title="osobní vlak (x)"><span>Os 2146</span></h3>
      <p class="reset time" >6:44</p><p class="station"><strong class="name ">Borovany</strong></p>
      <p class="reset time" >7:03</p><p class="station"><strong class="name ">České Budějovice</strong></p></div>
    </div>'''
    (conn,) = idos.parse_results(page, dt.date(2026, 10, 20))
    assert [l.line for l in conn.legs] == ["Bus 350", "Os 2146"]
    assert conn.legs[0].destination == "Borovany,,žel.st."


def test_same_named_stop_prefers_neighbouring_system():
    # „Domanín“ je obec u Třeboně (IDESKA) i na jižní Moravě (IDS JMK)
    place = ids_idos._place("Domanín", False, {"IDESKA"})
    assert "IDESKA" in place.systems


def test_drop_unpriced_idos_when_hack_priced_it():
    from vyhledavac.engine import drop_priced_duplicates
    from vyhledavac.models import Journey, Leg

    leg = Leg("A", "B", at(6), at(7), Mode.BUS, "x", "Bus 350")
    raw = Journey(provider="IDS", legs=[leg])
    priced = Journey(provider="IDESKA + Arriva", legs=[leg], segments=[raw])
    priced.fares = [__import__("vyhledavac.models", fromlist=["Fare"]).Fare(
        amount=Decimal(10), currency="CZK", amount_czk=Decimal(10))]
    assert drop_priced_duplicates([raw, priced]) == [priced]


@pytest.mark.asyncio
async def test_idos_complete_prices_rest_with_same_train():
    from vyhledavac.hacks.idos_complete import IdosCompleteHack
    from vyhledavac.models import Journey, Leg

    bus = Leg("Domanín, Vrchy", "České Budějovice", at(6, 5), at(7, 3), Mode.BUS, "ČSAD", "Bus 350")
    train = Leg("České Budějovice", "Praha hl.n.", at(7, 10), at(9, 10), Mode.TRAIN, "ČD", "R 728")
    journey = Journey(provider="IDS", legs=[bus, train],
                      raw={"ids_pieces": [(0, 0, "49", "IDESKA", "Plné jízdné")], "unpriced": [1]})
    from conftest import make_journey
    sjt = make_journey(provider="Arriva", origin="České Budějovice", destination="Praha hl.n.",
                       dep=at(7, 10), arr=at(9, 10), price=352, line="R 728")
    other = make_journey(provider="Arriva", origin="České Budějovice", destination="Praha hl.n.",
                         dep=at(7, 58), arr=at(9, 39), price=300, line="IC 538")

    async def search(q, allow_browser=False, only=None):
        assert "IDS" not in (only or [])
        return [sjt, other]

    (done,) = await IdosCompleteHack().find(SearchQuery(origin="Domanín vrchy", destination="Praha",
                                                        departure=at(6)), [journey], search)
    assert done.price == Decimal(401) and [s.provider for s in done.segments] == ["IDESKA", "Arriva"]


def _infos(stops_by_leg):
    """Úseky pro plánovač jízdenek: [(jméno, čas, vlak?), ...] po úsecích."""
    from vyhledavac import stations
    from vyhledavac.hacks.ids_ticket import SYSTEMS, RouteStop

    infos = []
    for stops, systems, rail in stops_by_leg:
        route = []
        for name, hh, mm in stops:
            place = stations.rail_place(name) if rail else stations.place(name)
            zones = {k: v for k, v in place.systems.items() if k in SYSTEMS}
            route.append(RouteStop(name, at(hh, mm), zones, place.kraj, name if rail else "", rail=rail))
        infos.append((route, systems, True))
    return infos


def test_ids_plans_split_train_at_a_halt_between_systems():
    infos = _infos([
        ([("Domanín, Vrchy", 8, 43), ("České Budějovice, aut.nádr.", 9, 26)], {"IDESKA"}, False),
        ([("České Budějovice", 10, 10), ("Tábor", 10, 50), ("Benešov u Prahy", 11, 30),
          ("Praha hl.n.", 12, 10)], None, True),
    ])
    query = SearchQuery(origin="Domanín, Vrchy", destination="Praha", departure=at(8))
    plans = IdsIdosProvider._plans(query, infos)
    whole = [p for p in plans if all(seg["kind"] == "ids" for seg in p)]
    assert whole, plans
    # Praha neleží v IDESKA: celou cestu jedním systémem nejde – lom ve stanici vlaku
    best = min(whole, key=lambda p: sum(Decimal(s["price"]) for s in p))
    assert len({s["system"] for s in best}) == 2 and best[-1]["to"] == [1, 3]
    assert best[-1]["from"][0] == 1 and best[-1]["from"][1] in (1, 2)       # Tábor nebo Benešov
    # a varianta „vlak u dopravce“ (jízdenka ČD může být levnější)
    assert any(seg["kind"] == "buy" and seg["from"] == [1, 0] and seg["to"] == [1, 3]
               for p in plans for seg in p)


def test_cheaper_hack_replaces_priced_idos_journey():
    from vyhledavac.engine import drop_priced_duplicates
    from vyhledavac.models import Fare, Journey, Leg

    leg = Leg("A", "B", at(6), at(7), Mode.TRAIN, "ČD", "R 722")

    def priced(provider, amount, hack):
        j = Journey(provider=provider, legs=[leg], segments=[Journey(provider="x", legs=[leg])] if hack else [])
        j.fares = [Fare(amount=Decimal(amount), currency="CZK", amount_czk=Decimal(amount))]
        return j

    ids, cheaper, dearer = priced("IDS", 150, False), priced("IDESKA + ČD", 140, True), priced("IDESKA + ČD", 160, True)
    assert drop_priced_duplicates([ids, cheaper]) == [cheaper]
    assert drop_priced_duplicates([ids, dearer]) == [ids, dearer]


def test_slice_cuts_a_train_at_a_stop():
    from vyhledavac.hacks.idos_complete import _slice
    from vyhledavac.models import Leg, Stop

    train = Leg("České Budějovice", "Praha hl.n.", at(10, 10), at(12, 10), Mode.TRAIN, "České dráhy, a.s.", "R 722",
                stops=[Stop("České Budějovice", departure=at(10, 10)), Stop("Tábor", at(10, 50), at(10, 52)),
                       Stop("Praha hl.n.", arrival=at(12, 10))])
    (part,) = _slice([train], [0, 1], [0, 2])
    assert (part.origin, part.departure, part.arrival) == ("Tábor", at(10, 52), at(12, 10))
