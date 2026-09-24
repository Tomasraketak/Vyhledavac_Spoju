"""Logika zjišťování cen ZSSK přes nákupní trychtýř (bez prohlížeče)."""

from decimal import Decimal

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.models import FareStatus, Passenger, SearchQuery
from vyhledavac.providers.zssk import ZsskProvider, _parse_eur

from conftest import at, make_journey


class _Converter:
    def to_czk(self, amount, currency):
        return amount * 25


def journeys(n):
    out = []
    for i in range(n):
        j = make_journey(provider="ZSSK", dep=at(8 + i), arr=at(13 + i))
        j.journey_id = str(i)
        out.append(j)
    return out


def provider(**options):
    p = ZsskProvider(cache=MemoryCache(), converter=_Converter(), options=options)

    async def ensure_list(session, page, query):
        pass

    p._ensure_list = ensure_list
    return p


def q():
    return SearchQuery(origin="Bratislava", destination="Košice", departure=at(8),
                       passengers=(Passenger(age=40),))


@pytest.mark.asyncio
async def test_funnel_prices_first_n_and_says_so():
    p = provider(price_limit=2)
    calls = []

    async def funnel(page, index, query):
        calls.append(index)
        return Decimal("19.44")

    p._price_via_funnel = funnel
    js = journeys(4)
    from vyhledavac.providers import base
    token = base._NOTES.set([])
    try:
        await p._fill_missing_prices(None, None, js, q())
        notes = base._NOTES.get()
    finally:
        base._NOTES.reset(token)

    assert calls == [0, 1]
    assert js[0].price == Decimal("19.44") * 25 and js[0].fare_status is FareStatus.PRICED
    assert js[2].price is None
    assert any("prvních 2" in n for n in notes)


@pytest.mark.asyncio
async def test_one_broken_connection_does_not_stop_the_rest():
    p = provider()

    async def funnel(page, index, query):
        if index == 1:
            raise TimeoutError("modál se nezavřel")
        return Decimal("10")

    p._price_via_funnel = funnel
    js = journeys(3)
    await p._fill_missing_prices(None, None, js, q())
    assert [j.price is not None for j in js] == [True, False, True]


@pytest.mark.asyncio
async def test_gives_up_after_two_failures_in_row():
    p = provider()
    calls = []

    async def funnel(page, index, query):
        calls.append(index)
        raise TimeoutError("web nereaguje")

    p._price_via_funnel = funnel
    await p._fill_missing_prices(None, None, journeys(6), q())
    assert calls == [0, 1]


def test_zero_euro_is_a_real_price():
    assert _parse_eur("Celková suma 0,00 €") == Decimal("0.00")
    assert _parse_eur("19,44 €") == Decimal("19.44")
    assert _parse_eur("") is None
