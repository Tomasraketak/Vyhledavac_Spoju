"""Mezinárodní trasy z benchmarku: špatná místa od dopravců se zahazují."""

import pytest

from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import SearchQuery
from vyhledavac.providers.base import _right_place
from vyhledavac.providers.cd import _plausible
from vyhledavac.providers.oneticket import OneTicketProvider

from conftest import at, make_journey


def test_cd_result_from_remembered_origin_is_dropped():
    assert not _plausible("Praha hl.n.", "Ostrava")
    assert _plausible("Č. Budějovice", "České Budějovice")
    assert _plausible("Ústí n.L. hl.n.", "Ústí nad Labem")
    assert _plausible("Krakow Glowny", "Krakov")


@pytest.mark.asyncio
async def test_oneticket_does_not_turn_munich_into_mnichovice():
    query = SearchQuery(origin="Praha", destination="Mnichov", departure=at(8))
    with pytest.raises(StationNotFoundError):
        await OneTicketProvider().search(query)


def test_foreign_city_far_away_is_wrong_place():
    query = SearchQuery(origin="Praha", destination="Mnichov", departure=at(8))
    assert not _right_place(make_journey(origin="Praha hl.n.", destination="Mnichovice"), query)
    assert _right_place(make_journey(origin="Praha hl.n.", destination="München Hbf"), query)
