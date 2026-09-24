"""Časové pásmo bez databáze pásem (Windows bez balíčku tzdata)."""

import datetime as dt

from vyhledavac.models import CentralEuropeanTime

CET = CentralEuropeanTime()


def test_summer_is_utc_plus_two():
    assert dt.datetime(2026, 9, 30, 8, 0, tzinfo=CET).utcoffset() == dt.timedelta(hours=2)


def test_winter_is_utc_plus_one():
    assert dt.datetime(2026, 12, 1, 8, 0, tzinfo=CET).utcoffset() == dt.timedelta(hours=1)


def test_switch_dates_2026():
    # 2026: letní čas od 29. 3. 02:00 do 25. 10. 03:00 místního času.
    assert dt.datetime(2026, 3, 29, 1, 30, tzinfo=CET).utcoffset() == dt.timedelta(hours=1)
    assert dt.datetime(2026, 3, 29, 3, 30, tzinfo=CET).utcoffset() == dt.timedelta(hours=2)
    assert dt.datetime(2026, 10, 25, 1, 30, tzinfo=CET).utcoffset() == dt.timedelta(hours=2)
    assert dt.datetime(2026, 10, 25, 4, 0, tzinfo=CET).utcoffset() == dt.timedelta(hours=1)


def test_matches_real_zone():
    from zoneinfo import ZoneInfo

    prague = ZoneInfo("Europe/Prague")
    moment = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc)
    for hours in range(0, 365 * 24, 7):
        utc = moment + dt.timedelta(hours=hours)
        assert utc.astimezone(CET).utcoffset() == utc.astimezone(prague).utcoffset(), utc
