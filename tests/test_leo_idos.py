"""Leo Express (GraphQL) a spoje IDS z IDOS."""

import datetime as dt
import gzip
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac import idos
from vyhledavac.cache import MemoryCache
from vyhledavac.models import DiscountCard, Mode, Passenger, SearchQuery
from vyhledavac.providers import ids_idos, leo
from vyhledavac.providers.ids_idos import IdsIdosProvider

from conftest import at

DATA = Path(__file__).parent / "data"

STATIONS = [
    {"id": "5457076", "name": "Praha hl.n.", "name_master": "Praha hl.n.", "type": "train",
     "parent_station": {"short": "PRAHA", "name": "Praha"}},
    {"id": "5100028", "name": "Krakov Główny", "name_master": "Kraków Główny", "type": "train",
     "parent_station": None},
    {"id": "64663", "name": "Krakov, aut. st.", "name_master": "Kraków, dw. aut.", "type": "bus",
     "parent_station": None},
]


def test_leo_only_where_it_runs():
    assert leo.pick_station("Praha", STATIONS) == ("PRAHA", "Praha")
    assert leo.pick_station("Praha hl.n.", STATIONS) == ("5457076", "Praha hl.n.")
    assert leo.pick_station("Kraków", STATIONS) == ("5100028", "Krakov Główny")   # vlak má přednost
    assert leo.pick_station("Brno", STATIONS) is None                          # Leo tam nejezdí


@pytest.mark.parametrize("age, isic, name", [(3, False, "child"), (12, False, "child6"),
                                             (20, True, "student"), (20, False, "adult"),
                                             (70, False, "senior")])
def test_leo_tariffs(age, isic, name):
    assert leo.tariff(Passenger(age=age, cards=(DiscountCard.ISIC,) if isic else ())) == name


def test_leo_cheapest_available_class():
    raw = {"classes": [{"id": "3", "name": "Economy"}, {"id": "1", "name": "Business"}],
           "class_info": [{"record_id": "1", "total": 779, "capacity": 8},
                          {"record_id": "3", "total": 309, "capacity": 0},
                          {"record_id": "4", "total": 499, "capacity": 13}]}
    assert leo.cheapest_class(raw) == (Decimal(499), "", 13)     # Economy vyprodaná


# ---- IDOS ---------------------------------------------------------------------------
def _page(name):
    return gzip.open(DATA / name, "rt", encoding="utf-8").read()


def test_idos_results_parse_legs_and_transfers():
    conns = idos.parse_results(_page("idos_results.html.gz"), dt.date(2026, 10, 20))
    assert len(conns) == 3
    bus = conns[1].legs[0]
    assert bus.line == "Bus 300" and bus.is_bus and bus.origin == "Třeboň,,aut.nádr."
    assert bus.departure.hour == 6 and bus.departure.minute == 5
    train = conns[2].legs
    assert [l.train_label for l in train] == ["R 730 Lužnice", "R 653 Rožmberk"]
    assert train[0].destination == "Veselí n.Lužnicí" and not train[0].is_bus


def test_idos_detail_line_system_and_route():
    page = _page("idos_detail.html.gz")
    assert idos.parse_line_info(page) == ("340300", {"IDESKA"})
    route = idos.parse_route(page)
    assert route[0] == "Třeboň,,aut.nádr." and route[-1] == "Jindřichův Hradec,,Pravdova"
    assert len(route) == 16


def test_ids_line_membership():
    assert ids_idos.line_systems("", {"IDESKA"}) == {"IDESKA"}
    assert ids_idos.line_systems("340300", set()) == {"IDESKA"}
    assert ids_idos.line_systems("320927", set()) == set()          # dálkový autobus mimo IDS


@pytest.mark.asyncio
async def test_ids_provider_skips_places_outside_one_system():
    from vyhledavac.errors import StationNotFoundError

    provider = IdsIdosProvider(cache=MemoryCache())
    with pytest.raises(StationNotFoundError):
        await provider.search(SearchQuery(origin="Plzeň", destination="Praha", departure=at(6)))


@pytest.mark.asyncio
async def test_ids_provider_prices_ideska_bus(monkeypatch):
    provider = IdsIdosProvider(cache=MemoryCache())
    pages = {"results": _page("idos_results.html.gz"), "detail": _page("idos_detail.html.gz"),
             "long": _page("idos_detail_long.html.gz")}

    class Client:
        async def get_text(self, url, **kwargs):
            if "/draha/" not in url:
                return pages["results"]
            return pages["long"] if "from=36746" in url else pages["detail"]

    provider._client = Client()
    query = SearchQuery(origin="Třeboň", destination="Jindřichův Hradec",
                        departure=dt.datetime(2026, 10, 20, 6, 0, tzinfo=at(6).tzinfo))
    journeys = await provider.search(query)
    buses = [j for j in journeys if j.legs[0].mode is Mode.BUS]
    assert buses and all(j.legs[0].line == "Bus 300" for j in buses)      # dálkový bus vyřazen
    assert buses[0].price > 0 and "IDESKA" in buses[0].fares[0].tariff
