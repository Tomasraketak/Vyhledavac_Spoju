"""Filtry a jejich skládání."""

import datetime as dt
from decimal import Decimal

from vyhledavac.filters.base import FilterPipeline
from vyhledavac.models import FareStatus, Mode, SearchQuery
from vyhledavac.registry import load_plugins
from tests.conftest import at, make_journey

load_plugins()


def q(**kw):
    defaults = dict(origin="Praha", destination="Vídeň", departure=at(8))
    defaults.update(kw)
    return SearchQuery(**defaults)


def test_departure_window_excludes_previous_day():
    """Spoj z předchozího dne nesmí projít – původní modulo 1440 ho pustilo."""
    pipeline = FilterPipeline.from_spec([("departure_window", {"hours": 6})])
    late_yesterday = make_journey(dep=at(23, 0, day=1), arr=at(2, 0, day=2))
    on_time = make_journey(dep=at(10), arr=at(14))
    out = pipeline.apply([late_yesterday, on_time], q())
    assert out == [on_time]


def test_max_price_keeps_unknown_by_default():
    pipeline = FilterPipeline.from_spec([("max_price", {"limit": 300})])
    cheap = make_journey(price=200)
    dear = make_journey(price=900)
    unknown = make_journey(price=None)
    out = pipeline.apply([cheap, dear, unknown], q())
    assert cheap in out and unknown in out and dear not in out


def test_max_price_can_drop_unknown():
    pipeline = FilterPipeline.from_spec([("max_price", {"limit": 300, "keep_unknown": False})])
    out = pipeline.apply([make_journey(price=None)], q())
    assert out == []


def test_pareto_drops_dominated():
    """Dražší spoj, který není ani rychlejší, nemá smysl nabízet."""
    good = make_journey(dep=at(8), arr=at(12), price=300)
    dominated = make_journey(dep=at(8), arr=at(13), price=500)
    pipeline = FilterPipeline.from_spec(["pareto"])
    out = pipeline.apply([good, dominated], q())
    assert out == [good]


def test_pareto_keeps_faster_but_pricier():
    slow_cheap = make_journey(dep=at(8), arr=at(14), price=300)
    fast_dear = make_journey(dep=at(8), arr=at(11), price=500)
    out = FilterPipeline.from_spec(["pareto"]).apply([slow_cheap, fast_dear], q())
    assert len(out) == 2


def test_deduplicate_keeps_cheaper_but_richer_route():
    """Levnější vyhraje, ale převezme podrobnou trasu od dražšího."""
    cheap = make_journey(provider="RegioJet", price=200, stops=["Praha", "Vídeň"])
    detailed = make_journey(provider="ÖBB", price=None,
                            stops=["Praha", "Brno", "Břeclav", "Vídeň"])
    out = FilterPipeline.from_spec(["deduplicate"]).apply([cheap, detailed], q())
    assert len(out) == 1
    assert out[0].price == Decimal("200")
    assert len(out[0].legs[0].stops) == 4


def test_min_transfer_time_rejects_two_minute_change():
    j = make_journey(dep=at(8), arr=at(12))
    second = make_journey(dep=at(12, 2), arr=at(14))
    j.legs = j.legs + second.legs
    out = FilterPipeline.from_spec([("min_transfer_time", {"minutes": 5})]).apply([j], q())
    assert out == []


def test_only_modes_allows_walking_transfer():
    j = make_journey(mode=Mode.TRAIN)
    walk = make_journey(mode=Mode.WALK)
    j.legs = j.legs + walk.legs
    out = FilterPipeline.from_spec([("only_modes", {"modes": ["train"]})]).apply([j], q())
    assert out == [j]


def test_drop_sold_out():
    ok = make_journey(price=100)
    gone = make_journey(price=None)
    gone.fare_status = FareStatus.SOLD_OUT
    out = FilterPipeline.from_spec(["drop_sold_out"]).apply([ok, gone], q())
    assert out == [ok]


def test_broken_filter_does_not_lose_results():
    """Rozbitý filtr se přeskočí, výsledky zůstanou."""
    from vyhledavac.filters.base import Filter

    class Boom(Filter):
        name = "boom"

        def apply(self, journeys, query):
            raise RuntimeError("nefunguju")

    j = make_journey(price=100)
    out = FilterPipeline([Boom()]).apply([j], q())
    assert out == [j]


def test_unknown_filter_is_skipped_not_fatal():
    pipeline = FilterPipeline.from_spec(["neexistujici_filtr", "deduplicate"])
    assert len(pipeline) == 1


# ---- doplňování cen napříč dopravci -----------------------------------------
def test_price_backfill_prices_a_schedule_only_journey():
    """ÖBB zná vlak, RegioJet ho prodává – cena se doplní."""
    schedule = make_journey(provider="ÖBB", price=None, line="RJ 1035",
                            dep=at(13), arr=at(17))
    seller = make_journey(provider="RegioJet", price=319, line="RJ 1035",
                          dep=at(13), arr=at(17))
    out = FilterPipeline.from_spec(["price_backfill"]).apply([schedule, seller], q())
    backfilled = next(j for j in out if j.provider == "ÖBB")
    assert backfilled.price == Decimal("319")
    assert any("prodává RegioJet" in n for n in backfilled.notes)


def test_price_backfill_requires_matching_arrival():
    """Stejné číslo, ale jiný vlak (jiný příjezd) – cenu nepřebírat."""
    schedule = make_journey(provider="ÖBB", price=None, line="RJ 55",
                            dep=at(8), arr=at(12))
    other = make_journey(provider="RegioJet", price=200, line="RJ 55",
                         dep=at(8), arr=at(15))
    out = FilterPipeline.from_spec(["price_backfill"]).apply([schedule, other], q())
    assert next(j for j in out if j.provider == "ÖBB").price is None


def test_price_backfill_needs_a_train_number():
    schedule = make_journey(provider="ÖBB", price=None, line="vlak",
                            dep=at(8), arr=at(12))
    seller = make_journey(provider="RegioJet", price=200, line="autobus",
                          dep=at(8), arr=at(12))
    out = FilterPipeline.from_spec(["price_backfill"]).apply([schedule, seller], q())
    assert next(j for j in out if j.provider == "ÖBB").price is None
