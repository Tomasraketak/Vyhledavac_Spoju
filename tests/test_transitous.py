"""Transitous: spojení bez IDOS (přehrání skutečných odpovědí, Třeboň → Domanín, Vrchy, říjen 2026)."""

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

from replay import install
from vyhledavac import transitous
from vyhledavac.cache import MemoryCache
from vyhledavac.errors import RateLimitedError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, SearchQuery
from vyhledavac.providers.ids_idos import IdsIdosProvider
from vyhledavac.providers.transitous_jr import TransitousProvider

FIXTURE = Path(__file__).parent / "data" / "transitous_trebon_domanin.json.gz"
QUERY = SearchQuery(origin="Třeboň", destination="Domanín, Vrchy",
                    departure=dt.datetime(2026, 10, 15, 6, tzinfo=DEFAULT_TZ), window=dt.timedelta(hours=4))


def test_timetable_provider(monkeypatch):
    install(monkeypatch, FIXTURE)
    found = asyncio.run(TransitousProvider(cache=MemoryCache()).search(QUERY))
    assert found and all(j.fare_status is FareStatus.UNKNOWN for j in found)
    direct = next(j for j in found if j.departure.strftime("%H:%M") == "06:30")
    leg = direct.legs[0]
    assert leg.mode is Mode.BUS and leg.line == "Bus 340330 5"
    assert leg.carrier == "ČSAD AUTOBUSY České Budějovice a.s."
    assert leg.destination == "Domanín, Vrchy" and len(leg.stops) >= 2
    # žádné duplicity ze sloučených zdrojů dat
    keys = [(j.departure, j.arrival, j.legs[0].line) for j in found]
    assert len(keys) == len(set(keys))


def test_ids_falls_back_to_transitous(monkeypatch):
    install(monkeypatch, FIXTURE)
    provider = IdsIdosProvider(cache=MemoryCache())

    async def blocked(query):
        raise RateLimitedError("IDS", "rate limit (429)")

    monkeypatch.setattr(provider, "_idos", blocked)
    result = asyncio.run(provider.run(QUERY))
    assert any("Transitous" in n for n in result.notes)
    direct = next(j for j in result.journeys if j.departure.strftime("%H:%M") == "06:30")
    assert direct.fare_status is FareStatus.PRICED and direct.price == Decimal(21)
    assert "Transitous" in direct.notes[0]


def test_line_and_kind():
    bus = {"mode": "BUS", "routeType": 3, "routeShortName": "100", "tripShortName": "340100 7"}
    assert transitous.kind_of(bus) == "regionální autobus" and transitous.line_of(bus, "regionální autobus") == \
        "Bus 340100 7"
    train = {"mode": "REGIONAL_RAIL", "routeType": 103, "routeShortName": "R19", "tripShortName": "R 865"}
    assert transitous.kind_of(train) == "vlak" and transitous.line_of(train, "vlak") == "R19 (R 865)"
    tram = {"mode": "TRAM", "routeType": 0, "routeShortName": "2"}
    assert transitous.line_of(tram, transitous.kind_of(tram)) == "Tram 2"


def test_pick_place():
    results = [{"type": "PLACE", "name": "Vrchy", "lat": 49.0, "lon": 14.7},
               {"type": "STOP", "name": "Domanín,Vrchy", "id": "cz-JDF-merged_jdf:stop:33720"}]
    assert transitous.pick_place("Domanín, Vrchy", results) == "cz-JDF-merged_jdf:stop:33720"
    town = [{"type": "PLACE", "name": "Třeboň", "lat": 49.0, "lon": 14.77},
            {"type": "STOP", "name": "Třeboň,Branná", "id": "cz-x"}]
    assert transitous.pick_place("Třeboň", town) == "49.0,14.77"
