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
    p = provider(price_workers=3, price_limit=6)
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
    p = provider(price_workers=2, price_limit=4)

    async def funnel(page, journey, query):
        return Decimal(str(5 + index(journey)))

    p._price_via_funnel = funnel
    js = journeys(4)
    await p._fill_missing_prices(_Session(), "page-0", js, q())
    assert [j.price for j in js] == [Decimal(str((5 + i) * 25)) for i in range(4)]


@pytest.mark.asyncio
async def test_unsellable_connection_is_not_retried():
    from vyhledavac.providers.zssk import _NotSellable

    p = provider(price_workers=1)
    calls = []

    async def funnel(page, journey, query):
        calls.append(index(journey))
        if index(journey) == 1:
            raise _NotSellable("ZSSK", "lůžkový vůz")
        return Decimal("9.60")

    p._price_via_funnel = funnel
    js = journeys(3)
    await p._fill_missing_prices(_Session(), "page-0", js, q())
    assert calls == [0, 1, 2]                       # bez druhého pokusu
    assert js[1].fare_status is FareStatus.NOT_BOOKABLE
    assert js[0].price is not None and js[2].price is not None


# Skutečný řádek seznamu ZSSK (Praha → … , říjen 2026).
NIGHT_TRAIN = ("22:16\tPraha hl.n.\t\nEN 443\n\n04:48\tVrútky\n06:05\tVrútky\t\nR 931\n"
               "/ Os 7307\n\n08:20\tZvolen os.st.\n08:36\tZvolen os.st.\t\nOs 6211\n\n"
               "08:50\tVígľaš\n10 hod. 34 min.\n592 km\n Upozornenie!  IDS Východ")


def test_list_row_becomes_legs_with_transfers():
    from vyhledavac.providers.zssk import _legs_from_text

    legs = _legs_from_text(NIGHT_TRAIN, q())
    assert [(l.origin, l.destination, l.line) for l in legs] == [
        ("Praha hl.n.", "Vrútky", "EN 443"),
        ("Vrútky", "Zvolen os.st.", "R 931 / Os 7307"),
        ("Zvolen os.st.", "Vígľaš", "Os 6211"),
    ]
    assert legs[0].departure.day == 1 and legs[0].arrival.day == 2      # přes půlnoc
    assert legs[-1].arrival.hour == 8 and legs[-1].arrival.minute == 50


def test_night_departure_after_midnight_is_next_day():
    from vyhledavac.providers.zssk import _legs_from_text

    text = "00:36\tPraha hl.n.\t\nNJ 457\n/ EN 477\n\n06:02\tBratislava hl.st.\n12 hod."
    legs = _legs_from_text(text, q())          # dotaz na 1. 10. 8:00
    assert legs[0].departure.day == 2 and legs[0].line == "NJ 457 / EN 477"


def test_station_suggestion_must_match_the_query():
    """Na 'Vídeň' ZSSK jako první nabídne Vígľaš – ten se brát nesmí."""
    from vyhledavac.normalize import aliases, best_match

    assert best_match("Vídeň", ["VÍGĽAŠ", "Vígľašská Huta-Kalinka"]) is None
    assert "Wien" in aliases("Vídeň") and "Vieden" in aliases("Vídeň")
    assert best_match("Vídeň", ["Wien Hbf", "Wiener Neustadt"]) == 0
    assert best_match("Bratislava", ["BRATISLAVA", "Bratislava hl.st."]) == 0


@pytest.mark.asyncio
async def test_route_not_sold_online_stops_early():
    from vyhledavac.providers.zssk import _NotSellable

    p = provider(price_workers=2, price_limit=4)
    calls = []

    async def funnel(page, journey, query):
        calls.append(index(journey))
        raise _NotSellable("ZSSK", "neprodává")

    p._price_via_funnel = funnel
    token = base._NOTES.set([])
    try:
        await p._fill_missing_prices(_Session(), "page-0", journeys(6), q())
        notes = base._NOTES.get()
    finally:
        base._NOTES.reset(token)
    assert len(calls) <= 3
    assert any("neprodává" in n for n in notes)


def test_zero_euro_is_a_real_price():
    assert _parse_eur("Celková suma 0,00 €") == Decimal("0.00")
    assert _parse_eur("19,44 €") == Decimal("19.44")
    assert _parse_eur("") is None
