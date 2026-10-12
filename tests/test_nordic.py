"""Vy (Norsko), DSB (Dánsko), SJ (Švédsko) – přehrání skutečných odpovědí API (říjen 2026)."""

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from replay import install
from vyhledavac import geo
from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.normalize import normalize
from vyhledavac.providers import dsb, sj, vy

DATA = Path(__file__).parent / "data"


def query(origin, destination, **kwargs):
    return SearchQuery(origin=origin, destination=destination,
                       departure=dt.datetime(2026, 10, 15, 7, tzinfo=DEFAULT_TZ), **kwargs)


def run(provider_cls, fixture, q, monkeypatch):
    install(monkeypatch, DATA / fixture)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    provider = provider_cls(cache=MemoryCache())
    return asyncio.run(provider.search(q))


async def _no_sleep(*_args, **_kwargs):
    return None


def test_vy_oslo_bergen(monkeypatch):
    found = run(vy.VyProvider, "vy_oslo_bergen.json.gz", query("Oslo", "Bergen"), monkeypatch)
    assert [j.departure.strftime("%H:%M") for j in found] == ["08:25", "08:30", "12:03", "16:23"]
    train = found[0]
    assert train.legs[0].line == "F4" and train.legs[0].mode is Mode.TRAIN
    assert train.legs[0].stops[0].name.startswith("Oslo") and len(train.legs[0].stops) > 2
    assert train.fare_status is FareStatus.PRICED and train.fares[0].currency == "NOK"
    assert train.fares[0].amount > 0
    assert found[1].legs[0].mode is Mode.BUS


def test_dsb_kobenhavn_odense(monkeypatch):
    found = run(dsb.DsbProvider, "dsb_kobenhavn_odense.json.gz", query("København", "Odense"), monkeypatch)
    assert len(found) >= 20
    first = found[0]
    assert first.departure.strftime("%H:%M") == "07:22" and first.legs[0].line == "ICL 625"
    assert first.fare_status is FareStatus.PRICED and first.fares[0].currency == "DKK"
    assert all(j.legs[0].mode is Mode.TRAIN for j in found[:5])


def test_sj_stockholm_goteborg(monkeypatch):
    found = run(sj.SjProvider, "sj_stockholm_goteborg.json.gz", query("Stockholm", "Göteborg"), monkeypatch)
    assert len(found) >= 30
    first = found[0]
    assert first.departure.strftime("%H:%M") == "07:14" and first.legs[0].line == "SJ Regional 163"
    assert first.fare_status is FareStatus.PRICED and first.fares[0].currency == "SEK"
    assert first.fares[0].amount > 0


@pytest.mark.parametrize("cls", [vy.VyProvider, dsb.DsbProvider, sj.SjProvider])
@pytest.mark.parametrize("origin,destination", [("Praha", "Brno"), ("Praha", "Wien")])
def test_not_asked_outside_scandinavia(cls, origin, destination):
    with pytest.raises(StationNotFoundError):
        asyncio.run(cls(cache=MemoryCache()).search(query(origin, destination)))


def test_geo_and_nordic_letters():
    assert normalize("København H") == "kobenhavn h" and normalize("Tønsberg") == "tonsberg"
    assert normalize("Łódź") == "lodz" and normalize("Straße") == "strasse"
    assert [geo.country_of(n) for n in ("København H", "Oslo S", "Malmö C", "Aarhus H")] == \
        ["DK", "NO", "SE", "DK"]


def test_vy_offer_passengers():
    q = query("Oslo", "Bergen", passengers=(Passenger(age=40), Passenger(age=70), Passenger(age=3)))
    body = vy.offer_body(["x"], q)
    assert [p["age"] for p in body["passengers"]] == [40, 70, 3]
    assert body["passengers"][1]["categories"] == [{"type": "senior-citizen"}]
