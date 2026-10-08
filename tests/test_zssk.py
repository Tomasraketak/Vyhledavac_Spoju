"""Logika zjišťování cen ZSSK přes nákupní trychtýř (bez prohlížeče)."""

import asyncio
from decimal import Decimal

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.models import FareStatus, Passenger, SearchQuery
from vyhledavac.providers import base
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


class _Session:
    """Falešná prohlížečová relace – souběžné kontexty jsou jen jména."""

    def __init__(self):
        self.isolated = 0

    async def new_isolated_page(self):
        self.isolated += 1
        return f"page-{self.isolated}"


def provider(**options):
    p = ZsskProvider(cache=MemoryCache(), converter=_Converter(), options=options)

    async def ensure_list(session, page, query):
        pass

    async def open_list(session, page, query):
        pass

    p._ensure_list = ensure_list
    p._open_list = open_list
    return p


def q():
    return SearchQuery(origin="Bratislava", destination="Košice", departure=at(8),
                       passengers=(Passenger(age=40),))


def index(journey):
    return int(journey.journey_id)


@pytest.mark.asyncio
async def test_funnel_prices_first_n_and_says_so():
    p = provider(price_limit=2, price_workers=1)
    calls = []

    async def funnel(page, journey, query):
        calls.append(index(journey))
        return Decimal("19.44")

    p._price_via_funnel = funnel
    js = journeys(4)
    token = base._NOTES.set([])
    try:
        await p._fill_missing_prices(_Session(), "page-0", js, q())
        notes = base._NOTES.get()
    finally:
        base._NOTES.reset(token)

    assert calls == [0, 1]
    assert js[0].price == Decimal("19.44") * 25 and js[0].fare_status is FareStatus.PRICED
    assert js[2].price is None
    assert any("prvních 2" in n for n in notes)


@pytest.mark.asyncio
async def test_one_broken_connection_does_not_stop_the_rest():
    p = provider(price_workers=1)

    async def funnel(page, journey, query):
        if index(journey) == 1:
            raise TimeoutError("modál se nezavřel")
        return Decimal("10")

    p._price_via_funnel = funnel
    js = journeys(3)
    await p._fill_missing_prices(_Session(), "page-0", js, q())
    assert [j.price is not None for j in js] == [True, False, True]


@pytest.mark.asyncio
async def test_gives_up_after_two_failures_in_row():
    p = provider(price_workers=1)
    calls = []

    async def funnel(page, journey, query):
        calls.append(index(journey))
        raise TimeoutError("web nereaguje")

    p._price_via_funnel = funnel
    await p._fill_missing_prices(_Session(), "page-0", journeys(6), q())
    assert calls == [0, 1]


@pytest.mark.asyncio
async def test_each_price_is_published_as_soon_as_it_is_known():
    """Na poslední spoj se nečeká – každá cena jde ven hned."""
    p = provider(price_workers=1)
    published = []

    async def funnel(page, journey, query):
        return Decimal(str(10 + index(journey)))

    p._price_via_funnel = funnel
    js = journeys(3)
    token = base._PUBLISH.set(lambda snap: published.append(
        [j.price is not None for j in snap]))
    try:
        await p._fill_missing_prices(_Session(), "page-0", js, q())
    finally:
        base._PUBLISH.reset(token)

    assert published == [[True, False, False], [True, True, False], [True, True, True]]


@pytest.mark.asyncio
async def test_prices_run_concurrently_in_isolated_contexts():
    p = provider(price_workers=3)
    session = _Session()
    running, peak, pages = 0, 0, set()

    async def funnel(page, journey, query):
        nonlocal running, peak
        pages.add(page)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return Decimal("9.60")

    p._price_via_funnel = funnel
    js = journeys(6)
    await p._fill_missing_prices(session, "page-0", js, q())

    assert all(j.price is not None for j in js)
    assert peak == 3 and session.isolated == 2
    assert pages == {"page-0", "page-1", "page-2"}


@pytest.mark.asyncio
async def test_prices_are_not_shared_between_connections():
    p = provider(price_workers=2)

    async def funnel(page, journey, query):
        return Decimal(str(5 + index(journey)))

    p._price_via_funnel = funnel
    js = journeys(4)
    await p._fill_missing_prices(_Session(), "page-0", js, q())
    assert [j.price for j in js] == [Decimal(str((5 + i) * 25)) for i in range(4)]


def test_zero_euro_is_a_real_price():
    assert _parse_eur("Celková suma 0,00 €") == Decimal("0.00")
    assert _parse_eur("19,44 €") == Decimal("19.44")
    assert _parse_eur("") is None
