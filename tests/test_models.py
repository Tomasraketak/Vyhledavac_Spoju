"""Datový model – hlavně situace, na kterých padal původní kód."""

import datetime as dt
from decimal import Decimal

from vyhledavac.models import FareStatus, sort_journeys
from tests.conftest import at, make_journey


def test_duration_across_midnight():
    """Noční spoj 23:30 -> 05:10 trvá 5h40m, ne -18h ani 18h."""
    j = make_journey(dep=at(23, 30, day=1), arr=at(5, 10, day=2))
    assert j.duration == dt.timedelta(hours=5, minutes=40)


def test_price_is_none_not_infinity():
    """Chybějící cena je None – ne 'nekonečně drahý spoj'."""
    j = make_journey(price=None)
    assert j.price is None
    assert j.price_label == "Cena neznámá"


def test_sold_out_label():
    j = make_journey(price=None)
    j.fare_status = FareStatus.SOLD_OUT
    assert j.price_label == "Vyprodáno"


def test_cheapest_fare_prefers_bookable():
    from vyhledavac.models import Fare

    j = make_journey(price=500)
    j.fares.append(Fare(amount=Decimal("100"), currency="CZK",
                        amount_czk=Decimal("100"), bookable=False))
    assert j.price == Decimal("500")


def test_sort_by_price_puts_unknown_last():
    cheap = make_journey(price=100, dep=at(9))
    unknown = make_journey(price=None, dep=at(7))
    expensive = make_journey(price=900, dep=at(10))
    ordered = sort_journeys([unknown, expensive, cheap], by="price")
    assert [j.price for j in ordered] == [Decimal("100"), Decimal("900"), None]


def test_transfers_counted_from_legs():
    j = make_journey()
    assert j.transfers == 0
    j.legs = j.legs * 3
    assert j.transfers == 2


def test_serves_matches_without_accents():
    j = make_journey(stops=["Praha hl.n.", "Břeclav", "Wien Hbf"])
    assert j.serves("Breclav")
    assert not j.serves("Ostrava")


def test_query_normalizes_naive_datetime():
    from vyhledavac.models import SearchQuery

    q = SearchQuery(origin="A", destination="B", departure=dt.datetime(2026, 10, 1, 8, 0))
    assert q.departure.tzinfo is not None


# ---- cenový přehled ---------------------------------------------------------
def _result(journeys):
    from vyhledavac.models import SearchQuery, SearchResult

    query = SearchQuery(origin="Praha", destination="Brno", departure=at(8))
    return SearchResult(query=query, journeys=list(journeys))


def test_price_coverage_reports_unknown_prices():
    res = _result([make_journey(price=100), make_journey(price=None)])
    assert res.price_coverage == 0.5
    assert len(res.unpriced) == 1


def test_cheapest_and_fastest():
    slow_cheap = make_journey(dep=at(8), arr=at(14), price=100)
    fast_dear = make_journey(dep=at(8), arr=at(10), price=900)
    res = _result([slow_cheap, fast_dear])
    assert res.cheapest is slow_cheap
    assert res.fastest is fast_dear
    assert res.savings_vs_fastest() == Decimal("800")


def test_best_value_avoids_both_extremes():
    cheap_slow = make_journey(dep=at(8), arr=at(16), price=100)
    middle = make_journey(dep=at(8), arr=at(11), price=300)
    fast_dear = make_journey(dep=at(8), arr=at(10), price=2000)
    res = _result([cheap_slow, middle, fast_dear])
    assert res.best_value is middle


def test_provider_prices_sorted_cheapest_first():
    res = _result([
        make_journey(provider="FlixBus", price=300),
        make_journey(provider="RegioJet", price=150),
        make_journey(provider="RegioJet", price=400),
    ])
    assert list(res.provider_prices()) == ["RegioJet", "FlixBus"]
    assert res.provider_prices()["RegioJet"] == Decimal("150")


def test_zero_price_is_a_real_price_not_missing():
    """Slovenská 100% sleva dává cenu 0 – to není chybějící cena."""
    free = make_journey(price=0)
    assert free.price == Decimal("0")
    assert _result([free]).price_coverage == 1.0

