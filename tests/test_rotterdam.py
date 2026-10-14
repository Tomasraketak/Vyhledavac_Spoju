"""Trnava → Rotterdam: časový rozpočet hacků, částečné jízdenky ÖBB, jízdenky po dopravcích."""

import asyncio
import time
from decimal import Decimal

import pytest

from vyhledavac import transitous
from vyhledavac.engine import SearchEngine
from vyhledavac.hacks.base import TravelHack
from vyhledavac.hacks.idos_complete import _carrier_groups, _cover_plan, sellers_for
from vyhledavac.hacks.obb_partial import ObbPartialHack, _span
from vyhledavac.models import Journey, Leg, Mode, SearchQuery, Stop
from vyhledavac.providers.flixbus import _nearest

from conftest import at, make_journey


class _Sleepy(TravelHack):
    def __init__(self, name, seconds):
        super().__init__()
        self.name, self.seconds = name, seconds

    async def find(self, query, journeys, search):
        await asyncio.sleep(self.seconds)
        return [make_journey(provider=self.name, price=100)]


@pytest.mark.asyncio
async def test_hacks_run_side_by_side_and_late_ones_are_stopped():
    engine = SearchEngine([], hacks=[_Sleepy("a", 0.3), _Sleepy("b", 0.3), _Sleepy("c", 30)])
    query = SearchQuery(origin="Trnava", destination="Rotterdam", departure=at(10))
    import vyhledavac.engine as eng

    grace, eng.HACK_GRACE = eng.HACK_GRACE, 0.5
    try:
        started = time.monotonic()
        found = await engine._run_hacks(query, [make_journey()], deadline=time.monotonic() + 0.2)
    finally:
        eng.HACK_GRACE = grace
    assert time.monotonic() - started < 2.5            # souběžně, pomalý hack ukončen
    assert [j.provider for j in found] == ["a", "b"]


@pytest.mark.asyncio
async def test_leg_search_after_deadline_answers_only_from_memory():
    engine = SearchEngine([])
    engine._hack_deadline = time.monotonic() - 1
    query = SearchQuery(origin="Köln Hbf", destination="Rotterdam", departure=at(10))
    assert await engine._leg_search(query) == []


def _partial(legs, start, end, eur="74.4", czk="1880"):
    return Journey(provider="ÖBB", legs=legs, raw={"partial": {"from": start, "to": end, "eur": eur, "czk": czk}})


def test_obb_ticket_ending_inside_a_train():
    stops = [Stop("München Hbf", departure=at(18)), Stop("Köln Hbf", arrival=at(22), departure=at(22, 5)),
             Stop("Düsseldorf Hbf", arrival=at(22, 30))]
    legs = [Leg("Trnava", "München Hbf", at(11), at(17, 40), Mode.TRAIN, "ÖBB", "RJ 1"),
            Leg("München Hbf", "Düsseldorf Hbf", at(18), at(22, 30), Mode.TRAIN, "DB", "ICE 920", stops=stops)]
    part = _span(legs, "Trnava", "Köln Hbf")
    assert [leg.destination for leg in part] == ["München Hbf", "Köln Hbf"]
    assert part[0] is legs[0] and part[1].arrival == at(22)


@pytest.mark.asyncio
async def test_obb_partial_rest_can_be_another_partial_ticket():
    query = SearchQuery(origin="Trnava", destination="Rotterdam Centraal", departure=at(10))
    legs = [Leg("Trnava", "Köln Hbf", at(11), at(20), Mode.TRAIN, "ÖBB", "RJ 1"),
            Leg("Köln Hbf", "Rotterdam Centraal", at(20, 30), at(23), Mode.TRAIN, "NS", "IC 2")]
    journey = _partial(legs, "Trnava", "Köln Hbf")
    # ÖBB zbytek prodá jen do Arnhemu, dál NS
    tail = [Leg("Köln Hbf", "Arnhem Centraal", at(20, 30), at(22), Mode.TRAIN, "ÖBB", "IC 2"),
            Leg("Arnhem Centraal", "Rotterdam Centraal", at(22, 10), at(23), Mode.TRAIN, "NS", "IC 3")]
    obb_tail = _partial(tail, "Köln Hbf", "Arnhem Centraal", eur="20", czk="500")
    ns = make_journey(provider="NS", origin="Arnhem Centraal", destination="Rotterdam Centraal",
                      dep=at(22, 10), arr=at(23), price=300, line="IC 3")

    async def search(q, allow_browser=False, only=None):
        if q.origin == "Köln Hbf":
            return [obb_tail]
        if q.origin == "Arnhem Centraal":
            return [ns]
        return []

    found = await ObbPartialHack().find(query, [journey], search)
    assert len(found) == 1 and found[0].price == Decimal(1880 + 500 + 300)
    assert [s.provider for s in found[0].segments] == ["ÖBB", "ÖBB", "NS"]


@pytest.mark.asyncio
async def test_obb_partial_swaps_expensive_piece_for_a_fitting_cheaper_train():
    query = SearchQuery(origin="Bratislava hl.st.", destination="Rotterdam", departure=at(10))
    legs = [Leg("Bratislava hl.st.", "Praha hl.n.", at(12), at(16, 23), Mode.TRAIN, "ZSSK", "EC 278"),
            Leg("Praha hl.n.", "Rotterdam", at(18), at(23, 59), Mode.TRAIN, "ES", "ES 452")]
    journey = _partial(legs, "Bratislava hl.st.", "Praha hl.n.", eur="89.2", czk="2174")
    regiojet = make_journey(provider="RegioJet", origin="Bratislava hl.st.", destination="Praha hl.n.",
                            dep=at(12, 26), arr=at(16, 58), price=399, line="RJ 1048")
    sleeper = make_journey(provider="EuropeanSleeper", origin="Praha hl.n.", destination="Rotterdam",
                           dep=at(18), arr=at(23, 59), price=2277, line="ES 452")

    async def search(q, allow_browser=False, only=None):
        return [regiojet] if q.origin == "Bratislava hl.st." else [sleeper]

    found = await ObbPartialHack().find(query, [journey], search)
    assert found[0].price == Decimal(399 + 2277)
    assert [s.provider for s in found[0].segments] == ["RegioJet", "EuropeanSleeper"]


def test_carrier_groups_keep_city_transit_apart():
    legs = [Leg("Trnava", "Bratislava hl.st.", at(11), at(11, 40), Mode.TRAIN,
                "Železničná spoločnosť Slovensko, a.s.", "R 1"),
            Leg("Bratislava hl.st.", "Praha hl.n.", at(12), at(16), Mode.TRAIN, "RegioJet,a.s.", "RJ 1"),
            Leg("Praha hl.n.", "Florenc", at(16, 10), at(16, 15), Mode.METRO,
                "Dopravní podnik hl.m. Prahy, a.s.", "C"),
            Leg("Florenc", "Rotterdam", at(17), at(23), Mode.BUS, "RegioJet", "RJ 2"),
            Leg("Rotterdam", "Rotterdam C", at(23, 10), at(23, 30), Mode.TRAIN, "", "IC 3")]
    groups = _carrier_groups(legs)
    assert [[leg.line for leg in g] for g in groups] == [["R 1"], ["RJ 1"], ["C"], ["RJ 2", "IC 3"]]


def test_plan_ending_early_is_completed_to_destination():
    legs = [make_journey(line=f"T{i}").legs[0] for i in range(4)]
    assert _cover_plan([{"kind": "buy", "from": [0, 0], "to": [2, 1]}], legs) == [
        {"kind": "buy", "from": [0, 0], "to": [3, 1]}]
    ids = [{"kind": "ids", "from": [0, 0], "to": [0, 1], "price": "20", "system": "X", "name": "x"}]
    assert _cover_plan(ids, legs)[-1] == {"kind": "buy", "from": [1, 0], "to": [3, 1]}


def test_zssk_browser_only_near_slovakia():
    run = make_journey(origin="Trnava", destination="Rotterdam").legs
    assert "ZSSK" not in sellers_for("Trnava", "Rotterdam", run, {"SK", "NL"})[1]
    run = make_journey(origin="Trnava", destination="Wien").legs
    assert "ZSSK" in sellers_for("Trnava", "Wien", run, {"SK", "AT"})[1]


def test_flixbus_picks_city_by_position():
    from vyhledavac import stations

    found = [{"name": "Berlín", "location": {"lat": 52.52, "lon": 13.4}},
             {"name": "Kolín", "location": {"lat": 50.9359, "lon": 6.9587}}]
    assert _nearest(found, stations.place("Köln Hbf")) == 1


def test_transitous_trnava_is_slovak():
    results = [{"type": "STOP", "name": "Trnava", "id": "cz-JDF-merged_jdf:stop:13000"},
               {"type": "STOP", "name": "Trnava", "id": "sk-zsr_5613676"}]
    assert transitous.pick_place("Trnava", results) == "sk-zsr_5613676"
    assert transitous.pick_place("Domanín", [{"type": "STOP", "name": "Domanín", "id": "cz-1"},
                                             {"type": "STOP", "name": "Domanín", "id": "sk-2"}]) == "cz-1"
