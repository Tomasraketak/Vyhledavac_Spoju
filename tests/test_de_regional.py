"""Odhad ceny německých regionálních vlaků podle ceníku (Deutschlandtarif, NRW)."""

from decimal import Decimal

import pytest

from vyhledavac import de_regional
from vyhledavac.de_regional import estimate, is_regional, table_price
from vyhledavac.models import Leg, Mode, Passenger

from conftest import at


def _leg(a, b, line, carrier="DB Regio AG NRW"):
    return Leg(a, b, at(10), at(11), Mode.TRAIN, carrier, line)


def test_price_table_is_official_list():
    assert table_price(1) == Decimal("2.00")
    assert table_price(10) == Decimal("4.60")
    assert table_price(100) == Decimal("31.90")
    assert table_price(2000) == Decimal("277.70")
    assert table_price(5000) == Decimal("277.70")          # nad 2000 km strop tabulky


def test_regional_vs_long_distance():
    assert is_regional(_leg("Köln Hbf", "Aachen Hbf", "RE1 (Zug-Nr. 26808)"))
    assert is_regional(_leg("Münster", "Enschede", "Zug RB64", "ÖBB"))
    assert is_regional(_leg("Köln", "Venlo", "020060", "Eurobahn"))
    assert not is_regional(_leg("München Hbf", "Köln Hbf", "ICE 610", "DB Fernverkehr AG"))
    assert not is_regional(_leg("Wien", "Salzburg", "RJX 62", "ÖBB"))


@pytest.fixture
def located(monkeypatch):
    points = {"Köln Hbf": (50.943, 6.959), "Aachen Hbf": (50.768, 6.091), "Duisburg Hbf": (51.43, 6.776),
              "Venlo": (51.364, 6.171), "München Hbf": (48.140, 11.558), "Nürnberg Hbf": (49.446, 11.082),
              "Rotterdam Centraal": (51.925, 4.469)}

    async def locate(name):
        return points.get(name)

    import vyhledavac.transfer as transfer
    monkeypatch.setattr(transfer, "locate", locate)


@pytest.mark.asyncio
async def test_nrw_takes_cheaper_eezy(located):
    price, label = await estimate([_leg("Köln Hbf", "Aachen Hbf", "RE1")], [Passenger(age=30)])
    assert label.startswith("eezy.nrw") and price < table_price(70)


@pytest.mark.asyncio
async def test_cross_border_to_venlo_and_children(located):
    run = [_leg("Duisburg Hbf", "Venlo", "RE13", "eurobahn")]
    adult, label = await estimate(run, [Passenger(age=30)])
    assert label.startswith("Deutschlandtarif")
    family, _ = await estimate(run, [Passenger(age=30), Passenger(age=10), Passenger(age=4)])
    assert family == adult + (adult / 2 * 20).to_integral_value(rounding="ROUND_FLOOR") / 20


@pytest.mark.asyncio
async def test_no_estimate_deep_abroad_or_for_ice(located):
    assert await estimate([_leg("Duisburg Hbf", "Rotterdam Centraal", "RE19")], [Passenger()]) is None
    assert await estimate([_leg("München Hbf", "Nürnberg Hbf", "ICE 1")], [Passenger()]) is None


@pytest.mark.asyncio
async def test_regional_journey_is_marked_as_estimate(located):
    class Rate:
        def to_czk(self, amount, currency):
            return amount * 25

    journey = await de_regional.regional_journey([_leg("München Hbf", "Nürnberg Hbf", "RE 4010")],
                                                 [Passenger()], Rate())
    assert journey.raw["estimate"] and journey.price == journey.fares[0].amount * 25
    assert "odhad" in journey.provider and journey.deep_link.startswith("https://int.bahn.de/")
