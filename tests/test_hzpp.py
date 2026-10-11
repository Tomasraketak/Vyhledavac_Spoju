import datetime as dt
import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError, TransientProviderError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Passenger, SearchQuery
from vyhledavac.providers import hzpp

DATA = json.load(gzip.open(Path(__file__).parent / "data" / "hzpp_journeys.json.gz", "rt", encoding="utf-8"))
STATIONS = DATA["stations"]["response"]


def at(h, m=0, day=14):
    return dt.datetime(2026, 10, day, h, m, tzinfo=DEFAULT_TZ)


def test_pick_station():
    assert hzpp.pick_station("Zagreb", STATIONS)["name"] == "Zagreb Glavni kol."
    assert hzpp.pick_station("Záhřeb", STATIONS)["name"] == "Zagreb Glavni kol."
    assert hzpp.pick_station("Zagreb Klara", STATIONS)["id"] == 72922
    assert hzpp.pick_station("Split", STATIONS)["id"] == 76660
    assert hzpp.pick_station("Varazdin", STATIONS)["name"] == "Varaždin"
    assert hzpp.pick_station("Ljubljana", STATIONS) is None


def test_journey_with_changes_and_price():
    items = hzpp.parse_journeys(DATA["zagreb_rijeka_all_1"])
    provider = hzpp.HzppProvider(cache=MemoryCache())
    journeys = [provider.to_journey(i) for i in items]
    direct = journeys[0]
    assert direct.legs[0].line == "Pu 4000" and direct.departure == at(8, 30) and direct.transfers == 0
    assert direct.fares[0].amount == Decimal("7.97") and direct.fares[0].currency == "EUR"
    change = journeys[1]
    assert [(l.origin, l.destination) for l in change.legs] == [("Zagreb Glavni kol.", "Ogulin"),
                                                                ("Ogulin", "Rijeka")]


def test_price_is_for_whole_group():
    items = hzpp.parse_journeys(DATA["zagreb_split_direct_2"])        # countForBenefit=2
    journey = hzpp.HzppProvider(cache=MemoryCache()).to_journey(items[0], children=1)
    assert journey.legs[0].line == "IC 523"
    assert journey.fare_status is FareStatus.PRICED and journey.fares[0].amount == Decimal("30.12")
    assert "dětské" in journey.notes[0]


def test_passenger_count():
    q = SearchQuery(origin="Zagreb", destination="Split", departure=at(8),
                    passengers=(Passenger(age=40), Passenger(age=8), Passenger(age=4)))
    assert hzpp.passenger_count(q) == (2, 1)


def test_drop_dominated_keeps_cheaper_and_faster():
    provider = hzpp.HzppProvider(cache=MemoryCache())
    journeys = [provider.to_journey(i) for i in hzpp.parse_journeys(DATA["osijek_zagreb_all_3"])]
    kept = hzpp.drop_dominated(journeys)
    times = {(j.departure.strftime("%H:%M"), j.arrival.strftime("%H:%M")) for j in kept}
    assert ("07:12", "12:32") not in times          # 07:34 přijede stejně, je levnější
    assert ("15:26", "21:53") in times              # pozdější spoj je dražší – zůstává
    assert len(kept) < len(journeys)


@pytest.mark.asyncio
async def test_search_falls_back_to_direct_when_all_times_out(monkeypatch):
    provider = hzpp.HzppProvider(cache=MemoryCache())

    async def stations():
        return STATIONS

    async def day(origin, destination, when, count, changes):
        if changes == "ALL":
            raise TransientProviderError("HŽPP", "server vrátil 504")
        assert (origin["id"], destination["id"], count) == (72480, 76660, 2)
        return hzpp.parse_journeys(DATA["zagreb_split_direct_2"])

    monkeypatch.setattr(provider, "stations", stations)
    monkeypatch.setattr(provider, "_day", day)
    notes = []
    monkeypatch.setattr(provider, "note", notes.append)
    q = SearchQuery(origin="Zagreb", destination="Split", departure=at(6),
                    passengers=(Passenger(age=40), Passenger(age=35)))
    journeys = await provider.search(q)
    assert [j.legs[0].line for j in journeys] == ["IC 523"]
    assert notes and "přímé" in notes[0]


@pytest.mark.asyncio
async def test_skips_routes_outside_croatia_without_network():
    provider = hzpp.HzppProvider(cache=MemoryCache())
    for origin, destination in (("Praha", "Wien"), ("Zagreb", "Ljubljana"), ("Budapest", "Split")):
        with pytest.raises(StationNotFoundError):
            await provider.search(SearchQuery(origin=origin, destination=destination, departure=at(8)))
    assert provider._client is None and provider._slow is None
