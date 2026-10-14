"""Málaga → Řím, Považská Bystrica → Amsterdam: jména stanic, kdo se ptá, učení prohlížeče."""

import datetime as dt

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.hacks.idos_complete import IdosCompleteHack, sellers_for
from vyhledavac.models import SearchQuery
from vyhledavac.providers.trenitalia import pick_location

from conftest import make_journey

FIRENZE = [{"id": 830006998, "name": "Firenze ( Tutte Le Stazioni )", "multistation": True},
           {"id": 830006421, "name": "Firenze S. M. Novella"},
           {"id": 835118008, "name": "FIRENZE"}]


def test_trenitalia_city_means_all_stations_not_bus_stop():
    assert pick_location("Firenze", FIRENZE)["id"] == 830006998
    assert pick_location("FIRENZE S.MARIA NOVELLA", FIRENZE, "FIRENZE S.MARIA NOVELLA")["id"] == 830006421


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["ČD", "ZSSK"])
async def test_czech_and_slovak_browser_sellers_skip_unrelated_routes(name):
    from vyhledavac.registry import PROVIDERS, load_plugins

    load_plugins()
    provider = PROVIDERS.get(name)()
    query = SearchQuery(origin="Málaga", destination="Řím", departure=dt.datetime(2026, 10, 20, 6, 36))
    with pytest.raises(StationNotFoundError):
        await provider.search(query)


def test_domestic_browser_answer_without_price_is_not_learned():
    hack = IdosCompleteHack()
    hack._learn_unpriced(("SK", "SK"))
    hack._learn_unpriced(("CZ", "CZ"))
    assert not hack._browser_unpriced(("SK", "SK")) and not hack._browser_unpriced(("CZ", "CZ"))
    hack._learn_unpriced(("SK", "NL"))
    assert hack._browser_unpriced(("SK", "NL"))


def test_cd_not_asked_for_transit_to_far_countries():
    run = make_journey(origin="Považská Bystrica", destination="Amsterdam").legs
    assert "ČD" not in sellers_for("Považská Bystrica", "Amsterodam", run, {"SK", "CZ", "NL"})[1]
