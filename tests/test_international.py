"""Mezinárodní trasy z benchmarku: špatná místa od dopravců se zahazují."""

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import SearchQuery
from vyhledavac.providers.base import _right_place
from vyhledavac.providers.cd import _plausible
from vyhledavac.providers.oneticket import OneTicketProvider

from conftest import at, make_journey


def test_cd_result_from_remembered_origin_is_dropped():
    assert not _plausible("Praha hl.n.", "Ostrava")
    assert _plausible("Č. Budějovice", "České Budějovice")
    assert _plausible("Ústí n.L. hl.n.", "Ústí nad Labem")
    assert _plausible("Krakow Glowny", "Krakov")


@pytest.mark.asyncio
async def test_oneticket_does_not_turn_munich_into_mnichovice():
    query = SearchQuery(origin="Praha", destination="Mnichov", departure=at(8))
    with pytest.raises(StationNotFoundError):
        await OneTicketProvider().search(query)


def test_vaporetto_after_train_reached_venice_is_trimmed():
    from vyhledavac.city import main_part
    from vyhledavac.models import Leg, Mode

    legs = [Leg("Praha hl.n.", "Brno hl.n.", at(6), at(8, 30), Mode.TRAIN, "ČD", "RJ 1"),
            Leg("Brno hl.n.", "Venezia Santa Lucia", at(9, 18), at(20, 3), Mode.TRAIN, "ÖBB", "RJX 135"),
            Leg("Venezia Santa Lucia", 'Ferrovia "D"', at(20, 3), at(20, 8), Mode.WALK),
            Leg('Ferrovia "D"', 'Riva de Biasio "A"', at(20, 10), at(20, 13), Mode.FERRY, "ACTV", "5.2")]
    assert main_part(legs, "Praha", "Benátky") == (0, 2)
    assert main_part(legs[1:], "Brno", "Benátky") == (0, 1)
    # konkrétní zastávka v cíli se neořezává; Bohumín není Ostrava
    assert main_part(legs, "Praha", 'Benátky, Riva de Biasio "A"')[1] == 4
    short = [Leg("Ostrava-Svinov", "Bohumín", at(8), at(8, 10), Mode.TRAIN, "ČD", "Os 1"),
             Leg("Bohumín", "Kraków Główny", at(8, 20), at(10, 30), Mode.TRAIN, "PKP", "EC 1"),
             Leg("Kraków Główny", "Kraków Grzegórzki", at(10, 40), at(10, 45), Mode.TRAIN, "KS", "S1")]
    assert main_part(short, "Ostrava", "Krakov") == (0, 2)


def test_foreign_city_far_away_is_wrong_place():
    query = SearchQuery(origin="Praha", destination="Mnichov", departure=at(8))
    assert not _right_place(make_journey(origin="Praha hl.n.", destination="Mnichovice"), query)
    assert _right_place(make_journey(origin="Praha hl.n.", destination="München Hbf"), query)


def test_gap_between_idos_ticket_and_next_piece_is_bought():
    from vyhledavac.hacks.idos_complete import _cover_plan
    from vyhledavac.models import Leg, Mode, Stop

    re2 = [Stop("Schwandorf", departure=at(12, 10)), Stop("Regensburg Hbf", arrival=at(12, 40)),
           Stop("Landshut", arrival=at(13, 30)), Stop("München Hbf", arrival=at(14, 16))]
    legs = [Leg("Plzen hl.n.", "Schwandorf", at(9, 7), at(10, 57), Mode.BUS, "ČD", "RE25"),
            Leg("Schwandorf", "München Hbf", at(12, 10), at(14, 16), Mode.TRAIN, "DB Regio", "RE2", stops=re2),
            Leg("München Hbf", "Zürich HB", at(14, 31), at(18, 28), Mode.TRAIN, "DB", "EC 190")]
    plan = [{"kind": "offer", "from": [0, 0], "to": [1, 1], "price": "463", "system": "IDOS", "name": "x"},
            {"kind": "buy", "from": [2, 0], "to": [2, 1]}]
    assert _cover_plan(plan, legs)[1] == {"kind": "buy", "from": [1, 1], "to": [1, 3]}
    # navazující kusy se nemění
    whole = [{"kind": "offer", "from": [0, 0], "to": [1, 3], "price": "1", "system": "IDOS", "name": "x"},
             {"kind": "buy", "from": [2, 0], "to": [2, 1]}]
    assert len(_cover_plan(whole, legs)) == 2


@pytest.mark.asyncio
async def test_journeys_over_limit_are_priced_from_known_answers(monkeypatch):
    import vyhledavac.hacks.idos_complete as ic
    from vyhledavac.registry import load_plugins

    load_plugins()                                    # registr prodejců (FlixBus …)
    from vyhledavac.models import Journey, Leg, Mode

    monkeypatch.setattr(ic, "MAX_JOURNEYS", 1)

    def route(hour):
        leg = Leg("Olomouc", "Katowice", at(hour), at(hour + 2, 20), Mode.BUS, "FlixBus-eu", f"N{hour}")
        return Journey(provider="Plánovač", legs=[leg],
                       raw={"plans": [[{"kind": "buy", "from": [0, 0], "to": [0, 1]}]]})

    flix = [make_journey(provider="FlixBus", origin="Olomouc", destination="Katowice", dep=at(h),
                         arr=at(h + 2, 20), price=300 + h, line=f"N{h}", mode=Mode.BUS) for h in (9, 12)]
    asked = []

    async def search(q, allow_browser=False, only=None):
        asked.append(q.departure)
        return flix

    query = SearchQuery(origin="Olomouc", destination="Varšava", departure=at(8))
    found = await ic.IdosCompleteHack().find(query, [route(9), route(12)], search)
    assert {j.departure.hour: j.price for j in found if j.departure.hour == 12} == {12: 312}
    assert all(d.hour < 12 for d in asked)          # druhý spoj bez dotazu, z paměti


def test_same_flixbus_from_planner_and_from_seller_shown_once():
    from vyhledavac.filters.builtin import SameTrip
    from vyhledavac.models import Mode

    query = SearchQuery(origin="Ostrava", destination="Krakov", departure=at(8))
    planner = make_journey(provider="Plánovač", origin="Ostrava (Nám. Republiky, ÚAN)",
                           destination="Krakow, Krakow-Balice Airport", dep=at(8, 15), arr=at(10, 35),
                           line="Bus FlixBus N899", mode=Mode.BUS)
    seller = make_journey(provider="FlixBus", origin="Ostrava (Nám. Republiky)", destination="Krakov Czyzyny",
                          dep=at(8, 15), arr=at(11, 25), price=308, line="FlixBus", mode=Mode.BUS)
    later = make_journey(provider="Plánovač", origin="Ostrava (Nám. Republiky, ÚAN)",
                         destination="Krakow, MDA Bus Station", dep=at(13, 25), arr=at(16), line="Bus FlixBus N3271A",
                         mode=Mode.BUS)
    assert SameTrip().apply([planner, seller, later], query) == [seller, later]


def test_rer_from_airport_into_paris_is_city_transport():
    from vyhledavac.city import main_part
    from vyhledavac.models import Leg, Mode

    legs = [Leg("Prague (Central Bus Station Florenc)", "Paris Charles de Gaulle Airport (Central Bus Station)",
                at(10), at(20, 15), Mode.BUS, "FlixBus-eu", "Bus FlixBus N367"),
            Leg("Aéroport CDG 1 (Terminal 3)", "Saint-Michel Notre-Dame", at(20, 20), at(20, 58), Mode.TRAIN,
                "RER", "B (KOKO32)")]
    assert main_part(legs, "Praha", "Paříž") == (0, 1)


@pytest.mark.asyncio
async def test_czech_train_piece_priced_by_idos_eshop_before_browser():
    import vyhledavac.hacks.idos_complete as ic
    from vyhledavac.models import Journey, Leg, Mode
    from vyhledavac.registry import load_plugins

    load_plugins()
    rj = Leg("Praha hl.n.", "Děčín hl.n.", at(10, 31), at(11, 58), Mode.TRAIN, "České dráhy, a.s.", "rj 382")
    ec = Leg("Děčín hl.n.", "Dresden Hbf", at(12, 2), at(12, 48), Mode.TRAIN, "DSB", "EC 382")
    route = Journey(provider="Plánovač", legs=[rj, ec],
                    raw={"plans": [[{"kind": "buy", "from": [0, 0], "to": [0, 1]},
                                    {"kind": "buy", "from": [1, 0], "to": [1, 1]}]]})
    idos = make_journey(provider="IDS", origin="Praha hl.n.", destination="Děčín hl.n.", dep=at(10, 31),
                        arr=at(11, 58), price=249, line="rj 382")
    obb = make_journey(provider="ÖBB", origin="Děčín hl.n.", destination="Dresden Hbf", dep=at(12, 2),
                       arr=at(12, 48), price=300, line="EC 382")
    browser = []

    async def search(q, allow_browser=False, only=None):
        if allow_browser:
            browser.append(q)
        if only == ["IDS"]:
            return [idos]
        return [obb] if q.origin.startswith("Děčín") else []

    query = SearchQuery(origin="Praha", destination="Drážďany", departure=at(10))
    hack = ic.IdosCompleteHack()
    hack._browser_locks = {}
    found = await hack.find(query, [route], search)
    assert found and found[0].price == 549 and not browser


def test_flixbus_stop_at_station_is_same_station():
    from vyhledavac import transfer

    assert transfer.same_station("Regensburg central train station", "Regensburg Hbf")
    assert not transfer.same_station("Berlin Südkreuz", "Berlin Hauptbahnhof")
