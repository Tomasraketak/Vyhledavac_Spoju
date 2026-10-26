"""Ceny pro skupinu u dopravců, kteří cenu umějí jen za jednoho cestujícího."""

from decimal import Decimal

import pytest

from vyhledavac.models import DiscountCard, Fare, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery
from vyhledavac.providers.base import Provider

from conftest import at

GROUP = (Passenger(76), Passenger(78), Passenger(50), Passenger(48), Passenger(20, cards=(DiscountCard.ISIC,)))


def _journey(price):
    leg = Leg("Wien Hbf", "Salzburg Hbf", at(8), at(10, 30), Mode.TRAIN, "ÖBB", "RJX 1")
    return Journey(provider="X", legs=[leg], fare_status=FareStatus.PRICED,
                   fares=[Fare(amount=Decimal(price), currency="EUR", amount_czk=Decimal(price) * 25)])


def _provider(name, **attrs):
    cls = type(name, (Provider,), {"name": name, "search": lambda self, q: None, **attrs})
    return cls()


def test_adult_price_is_scaled_to_group():
    """ÖBB počítá vždy jednoho dospělého: pět dospělých/seniorů = pětinásobek."""
    from vyhledavac.providers.obb import ObbProvider

    obb = ObbProvider()
    query = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8), passengers=GROUP)
    assert obb.group_factor(query) == 5
    family = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8),
                         passengers=(Passenger(40), Passenger(38), Passenger(10), Passenger(4)))
    assert obb.group_factor(family) == Decimal("2.5")       # dítě 10 let za polovic, 4 roky zdarma


def test_lead_price_only_for_identical_passengers():
    zssk = _provider("Z", price_basis="lead")
    same = SearchQuery(origin="Bratislava", destination="Košice", departure=at(8),
                       passengers=(Passenger(30), Passenger(30)))
    mixed = SearchQuery(origin="Bratislava", destination="Košice", departure=at(8), passengers=GROUP)
    assert zssk.group_factor(same) == 2
    assert zssk.group_factor(mixed) is None


def test_multi_passenger_provider_untouched():
    provider = _provider("M", multi_passenger=True)
    query = SearchQuery(origin="Praha", destination="Brno", departure=at(8), passengers=GROUP)
    assert provider.group_factor(query) == 1


@pytest.mark.asyncio
async def test_run_scales_prices_and_drops_unknown_group_price():
    query = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8), passengers=GROUP)

    async def search(self, q):
        return [_journey(30)]

    adult = _provider("A", price_basis="adult", search=search)
    result = await adult.run(query)
    fare = result.journeys[0].fares[0]
    assert fare.amount == 150 and fare.amount_czk == 3750
    assert "skupinu" in fare.tariff and any("skupinu" in n for n in result.notes)

    lead = _provider("L", price_basis="lead", search=search)
    result = await lead.run(query)
    assert result.journeys[0].price is None
    assert result.journeys[0].fare_status is FareStatus.UNKNOWN


def test_sbb_counts_group_itself():
    from vyhledavac.providers.sbb import SbbProvider, group_price

    assert SbbProvider.multi_passenger
    total, _ = group_price(Decimal(20), GROUP, saver=False)
    assert total == Decimal(200)            # 5 dospělých × 2 × cena s Halbtaxem


@pytest.mark.asyncio
async def test_partial_obb_ticket_is_scaled_too():
    """ÖBB prodá jen část trasy (raw["partial"]) – i ta cena musí být za skupinu."""
    query = SearchQuery(origin="Wien", destination="Salzburg", departure=at(8), passengers=GROUP)

    async def search(self, q):
        journey = _journey(30)
        journey.fares = []
        journey.fare_status = FareStatus.NOT_BOOKABLE
        journey.raw["partial"] = {"from": "München Hbf", "to": "Zürich HB", "eur": "60.00", "czk": "1500.00"}
        return [journey]

    adult = _provider("A", price_basis="adult", search=search)
    result = await adult.run(query)
    part = result.journeys[0].raw["partial"]
    assert Decimal(part["czk"]) == 7500 and Decimal(part["eur"]) == 300

    lead = _provider("L", price_basis="lead", search=search)
    result = await lead.run(query)
    assert "partial" not in result.journeys[0].raw
