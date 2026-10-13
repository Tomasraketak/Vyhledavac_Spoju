"""TCDD (Turecké dráhy) – jízdní řád z IDOS, cena za captchou e-shopu TCDD (říjen 2026)."""

import asyncio
import datetime as dt

import pytest

from vyhledavac import geo, idos, stations
from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, SearchQuery
from vyhledavac.normalize import strip_accents
from vyhledavac.providers import tcdd


def test_anatolian_cities_known():
    for name in ("Ankara", "Izmir", "İzmir", "Konya", "Eskisehir", "Kapikule"):
        assert geo.country_of(name) == "TR", name
    assert stations.place("Ankara").lat == pytest.approx(39.93, abs=0.01)
    assert str(geo.zone_of("Ankara")) == "Europe/Istanbul"
    assert stations.provider_name("Segedín") == "Szeged"
    assert strip_accents("Горна Оряховица") == "Gorna Oryahovitsa"


def test_tcdd_only_turkey():
    provider = tcdd.TcddProvider(cache=MemoryCache())
    q = SearchQuery("Praha", "Brno", dt.datetime(2026, 10, 15, 6, tzinfo=DEFAULT_TZ))
    with pytest.raises(StationNotFoundError):
        asyncio.run(provider.search(q))


def test_tcdd_journey_from_idos():
    t = dt.datetime(2026, 10, 15, 7, 35, tzinfo=DEFAULT_TZ)
    leg = idos.IdosLeg(line="X2 81005 YHT ANKARA - ISTANBUL", kind="vysokorychlostní vlak",
                       carrier="TCDD Turkish Republic State Railways", origin="Ankara", destination="Istanbul",
                       departure=t, arrival=t + dt.timedelta(hours=3, minutes=44))
    journey = tcdd.to_journey("TCDD", idos.IdosConnection([leg]))
    assert journey.fare_status is FareStatus.UNKNOWN
    assert journey.deep_link == tcdd.SHOP
    assert journey.legs[0].line == "X2 81005 YHT ANKARA - ISTANBUL"
