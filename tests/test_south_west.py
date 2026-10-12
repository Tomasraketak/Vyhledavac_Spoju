"""BDŽ (Bulharsko), CP (Portugalsko), Srbijavoz (Srbsko), SBB (Švýcarsko) – přehrání
skutečných odpovědí (říjen 2026)."""

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from replay import install
from vyhledavac.cache import MemoryCache
from vyhledavac.errors import StationNotFoundError
from vyhledavac.models import DEFAULT_TZ, FareStatus, Mode, Passenger, SearchQuery
from vyhledavac.providers import bdz, cp, sbb, srbijavoz

DATA = Path(__file__).parent / "data"


def query(origin, destination, hour, **kwargs):
    return SearchQuery(origin=origin, destination=destination,
                       departure=dt.datetime(2026, 10, 15, hour, tzinfo=DEFAULT_TZ), **kwargs)


def run(cls, fixture, q, monkeypatch):
    install(monkeypatch, DATA / fixture)
    return asyncio.run(cls(cache=MemoryCache()).search(q))


def test_bdz_prices_from_timetable(monkeypatch):
    found = run(bdz.BdzProvider, "bdz_sofia_plovdiv.json.gz", query("Sofie", "Plovdiv", 6), monkeypatch)
    assert [j.legs[0].line for j in found] == ["BV 1611", "BV 8651"]
    first = found[0]
    assert first.departure.strftime("%H:%M") == "08:22" and len(first.legs[0].stops) > 2
    assert first.fare_status is FareStatus.PRICED and first.fares[0].currency == "EUR"
    assert first.fares[0].amount > 0 and first.notes


def test_bdz_price_table_and_group():
    html = ("Тарифи 2-ра класа 1-ва класа Редовна тарифа 5,62 € 7,06 € Дете 7-10 г. 2,81 € 3,53 € "
            "Групова тарифа от 3 до 6 пътника 4,81 € -- Цена за запазено място 0,51 € 0,51 €")
    table = bdz.parse_prices(html)
    assert table == {"full": Decimal("5.62"), "child": Decimal("2.81"), "group": Decimal("4.81"),
                     "seat": Decimal("0.51")}
    total, _ = bdz.group_price(table, (Passenger(age=40), Passenger(age=8), Passenger(age=5)), True)
    assert total == Decimal("5.62") + Decimal("2.81") + 2 * Decimal("0.51")
    group, tariff = bdz.group_price(table, tuple(Passenger(age=30) for _ in range(3)), False)
    assert group == 3 * Decimal("4.81") and "skupinová" in tariff


def test_cp_lisboa_porto(monkeypatch):
    found = run(cp.CpProvider, "cp_lisboa_porto.json.gz", query("Lisabon", "Porto", 8), monkeypatch)
    ic = next(j for j in found if j.legs[0].line == "IC 721")
    assert ic.fares[0].amount == Decimal("28.05") and ic.fares[0].currency == "EUR"
    assert ic.legs[0].stops[0].name == "Lisboa Santa Apolonia" and len(ic.legs[0].stops) > 5
    assert any(len(j.legs) == 2 for j in found)


def test_cp_child_half_price():
    total, tariff = cp.group_price(Decimal("28.05"), (Passenger(age=40), Passenger(age=8), Passenger(age=2)))
    assert total == Decimal("28.05") + Decimal("14.03") and "zdarma" in tariff


def test_srbijavoz_beograd_nis(monkeypatch):
    found = run(srbijavoz.SrbijavozProvider, "srbijavoz_beograd_nis.json.gz", query("Bělehrad", "Niš", 6),
                monkeypatch)
    assert [j.legs[0].line for j in found] == ["Re 2903", "Re 2905", "IR 691"]
    re = found[0]
    assert re.fares[0].amount == Decimal(1256) and re.fares[0].currency == "RSD"
    assert re.legs[0].stops[0].name == "Beograd Centar-PROKOP" and re.legs[0].stops[-1].name == "Nis"


def test_srbijavoz_child_discount():
    train = {"cenau": 2512.0, "cenao": 2341.0, "cenad": 171.0}
    total, _ = srbijavoz.group_price(train, (Passenger(age=40), Passenger(age=10)), 2)
    assert total == Decimal(2512 - 2341 / 2 / 2).quantize(Decimal("1"))


def test_sbb_prices_from_old_timetable(monkeypatch):
    found = run(sbb.SbbProvider, "sbb_zurich_bern.json.gz", query("Curych", "Bern", 8), monkeypatch)
    assert found and all(j.fare_status is FareStatus.PRICED for j in found)
    ic = next(j for j in found if j.legs[0].line == "IC 8")
    assert ic.legs[0].origin == "Zürich HB" and ic.legs[0].destination == "Bern"
    assert ic.fares[0].currency == "CHF" and ic.fares[0].amount == Decimal("53.00")   # 2 × 26.50 s Halbtaxem
    assert "Halbtax" in ic.notes[0]


def test_sbb_group_price():
    total, tariff = sbb.group_price(Decimal("26.50"), (Passenger(age=40), Passenger(age=10), Passenger(age=4)),
                                    saver=True)
    assert total == Decimal("79.50") and "úsporná" in tariff and "zdarma" in tariff


@pytest.mark.parametrize("cls", [bdz.BdzProvider, cp.CpProvider, srbijavoz.SrbijavozProvider, sbb.SbbProvider])
def test_not_asked_for_czech_trip(cls):
    with pytest.raises(StationNotFoundError):
        asyncio.run(cls(cache=MemoryCache()).search(query("Praha", "Brno", 8)))
