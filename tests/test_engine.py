"""Engine – hlavně izolace chyb a agregace."""

import asyncio

import pytest

from vyhledavac.engine import SearchEngine
from vyhledavac.errors import BlockedError, TransientProviderError
from vyhledavac.models import SearchQuery
from vyhledavac.providers.base import Provider
from tests.conftest import at, make_journey


class FakeProvider(Provider):
    name = "Fake"

    def __init__(self, journeys=(), error=None, delay=0.0, **kw):
        super().__init__(**kw)
        self._journeys = list(journeys)
        self._error = error
        self._delay = delay

    async def search(self, query):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return list(self._journeys)


def q():
    return SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))


@pytest.mark.asyncio
async def test_one_failing_provider_does_not_break_others():
    good = FakeProvider([make_journey(price=100)])
    good.name = "Dobrý"
    bad = FakeProvider(error=TransientProviderError("Zlý", "spadlo to"))
    bad.name = "Zlý"

    engine = SearchEngine([good, bad])
    result = await engine.search(q(), enable_hacks=False)

    assert len(result.journeys) == 1
    assert [r.provider for r in result.failed_providers] == ["Zlý"]


@pytest.mark.asyncio
async def test_blocked_provider_is_flagged():
    blocked = FakeProvider(error=BlockedError("Blok", "cloudflare"))
    blocked.name = "Blok"
    result = await SearchEngine([blocked]).search(q(), enable_hacks=False)
    assert result.blocked_providers == ["Blok"]


@pytest.mark.asyncio
async def test_unexpected_exception_is_contained():
    class Exploding(FakeProvider):
        async def search(self, query):
            raise ZeroDivisionError("ups")

    provider = Exploding()
    provider.name = "Bum"
    result = await SearchEngine([provider]).search(q(), enable_hacks=False)
    assert not result.journeys
    assert "ZeroDivisionError" in result.failed_providers[0].error


@pytest.mark.asyncio
async def test_slow_provider_hits_timeout_without_blocking_rest():
    slow = FakeProvider([make_journey(price=1)], delay=5)
    slow.name = "Pomalý"
    fast = FakeProvider([make_journey(price=100)])
    fast.name = "Rychlý"

    engine = SearchEngine([slow, fast], provider_timeout=0.2)
    result = await engine.search(q(), enable_hacks=False)

    assert len(result.journeys) == 1
    assert "časový limit" in result.failed_providers[0].error


@pytest.mark.asyncio
async def test_partial_callback_fires_per_provider():
    seen = []
    p1 = FakeProvider([make_journey(price=100)]); p1.name = "A"
    p2 = FakeProvider([make_journey(price=200)]); p2.name = "B"
    engine = SearchEngine([p1, p2])
    await engine.search(q(), on_partial=seen.append, enable_hacks=False)
    assert {r.provider for r in seen} == {"A", "B"}


@pytest.mark.asyncio
async def test_insane_journeys_are_dropped():
    """Příjezd před odjezdem je chyba parsování, ne spoj."""
    broken = make_journey(dep=at(12), arr=at(8))
    provider = FakeProvider([broken])
    result = await SearchEngine([provider]).search(q(), enable_hacks=False)
    assert result.journeys == []


@pytest.mark.asyncio
async def test_leg_search_is_cached_across_hacks():
    calls = {"n": 0}

    class Counting(FakeProvider):
        async def search(self, query):
            calls["n"] += 1
            return [make_journey(price=50)]

    provider = Counting()
    provider.name = "Counting"
    engine = SearchEngine([provider])
    sub = q().leg_query("Praha", "Břeclav")
    await engine._leg_search(sub)
    await engine._leg_search(sub)
    assert calls["n"] == 1


def test_build_reports_unknown_provider_without_crashing():
    engine = SearchEngine.build(["RegioJet", "NeexistujiciDopravce"])
    assert [p.name for p in engine.providers] == ["RegioJet"]


def test_registry_lists_builtin_plugins():
    from vyhledavac.registry import FILTERS, HACKS, PROVIDERS, load_plugins

    load_plugins()
    assert {"RegioJet", "FlixBus", "ÖBB", "ČD"} <= set(PROVIDERS.names())
    assert {"split_ticket", "hidden_city"} <= set(HACKS.names())
    assert "pareto" in FILTERS.names()


@pytest.mark.asyncio
async def test_limit_keeps_the_cheapest_not_an_arbitrary_slice():
    """Řazení musí proběhnout dřív než ořez počtu výsledků."""
    from vyhledavac.filters.base import FilterPipeline

    journeys = [make_journey(dep=at(8 + i), arr=at(12 + i), price=500 - i * 50)
                for i in range(5)]           # nejdražší je první
    provider = FakeProvider(journeys)
    provider.name = "Many"

    engine = SearchEngine([provider],
                          filters=FilterPipeline.from_spec([("limit", {"count": 2})]))
    result = await engine.search(q(), sort_by="price", enable_hacks=False)

    prices = [j.price for j in result.journeys]
    assert prices == sorted(prices)
    assert prices[0] == 300      # skutečně nejlevnější z pětice


@pytest.mark.asyncio
async def test_foreign_provider_gets_local_city_names():
    seen = {}

    class Recorder(FakeProvider):
        async def search(self, query):
            seen[self.name] = (query.origin, query.destination)
            return []

    foreign, czech = Recorder(), Recorder()
    foreign.name, czech.name = "Cizí", "Český"
    czech.czech_names = True
    query = SearchQuery(origin="Praha", destination="Lublaň", departure=at(8))
    await SearchEngine([foreign, czech]).search(query, enable_hacks=False)
    assert seen == {"Cizí": ("Praha", "Ljubljana"), "Český": ("Praha", "Lublaň")}
