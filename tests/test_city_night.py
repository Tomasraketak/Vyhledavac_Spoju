"""MHD po městě, noční vlak s návazností a místní časy stanic (říjen 2026)."""

import datetime as dt
from decimal import Decimal

from vyhledavac import city, geo, idos
from vyhledavac.hacks import night_train
from vyhledavac.ids import idsok
from vyhledavac.models import DEFAULT_TZ, Journey, Leg, Mode, Passenger, SearchQuery
from vyhledavac.providers import european_sleeper as es
from vyhledavac.providers._hafas import _parse_time
from vyhledavac.providers.base import price_scope


def test_local_time_of_station():
    # IDOS i MÁV píší místní čas: Bukurešť 16:00 = 15:00 v Praze
    t = dt.datetime(2026, 10, 15, 16, 0, tzinfo=DEFAULT_TZ)
    assert geo.local_time(t, "Bucuresti Nord Gr. A").astimezone(DEFAULT_TZ).hour == 15
    assert geo.local_time(t, "Budapest-Keleti pu") == t
    assert geo.local_time(t, "Sofia").astimezone(DEFAULT_TZ).hour == 15


def test_hafas_time_with_station_offset():
    t = _parse_time(dt.date(2026, 10, 15), "160000", DEFAULT_TZ, 180)
    assert t.astimezone(DEFAULT_TZ).hour == 15
    assert _parse_time(dt.date(2026, 10, 15), "160000", DEFAULT_TZ, 120).astimezone(DEFAULT_TZ).hour == 16


def test_price_scope_sparse_route():
    def j(hour):
        t = dt.datetime(2026, 10, 15, hour, tzinfo=DEFAULT_TZ)
        return Journey(provider="x", legs=[Leg(origin="a", destination="b", departure=t,
                                               arrival=t + dt.timedelta(hours=10), mode=Mode.TRAIN)])
    q = SearchQuery("Bukurešť", "Bratislava", dt.datetime(2026, 10, 15, 6, tzinfo=DEFAULT_TZ))
    # první tři vlaky, ať jedou kdykoli v okně hledání
    assert [x.departure.hour for x in price_scope([j(18), j(15), j(16), j(17)], q)] == [15, 16, 17]


def test_night_train_candidates_amsterdam_bratislava():
    pairs = night_train.candidates((52.374, 4.890), (48.148, 17.107))
    assert pairs[0] == ("8400058", "5457076")              # Amsterdam Centraal → Praha hl. n.
    # Praha → Brno noční vlak nedává smysl (nejede tam)
    assert night_train.candidates((50.08, 14.43), (49.19, 16.61)) == []
    assert all(s in es.COORDS for route in es.ROUTES for s in route)


def test_city_trip_detection():
    trip = city.trip("Praha, Anděl", "Praha, Letňany")
    assert (trip.city, trip.code, trip.origin_stop, trip.destination_stop) == ("Praha", "", "Anděl", "Letňany")
    assert city.trip("Wien Stephansplatz", "Wien Schönbrunn").code == "AT"
    assert city.trip("Bratislava, Hodžovo námestie", "Bratislava hl.st.").city == "Bratislava"
    assert city.trip("Budapest-Keleti", "Budapest-Nyugati").city == "Budapešť"
    assert city.trip("Praha", "Brno") is None
    assert city.trip("Praha", "Praha") is None
    assert city.trip("Bratislava hl.st.", "Wien Hbf") is None


def test_city_tickets():
    vienna = city.trip("Wien Stephansplatz", "Wien Schönbrunn")
    assert city.ticket_for(vienna, 3, 50)[1].price == Decimal("3.20")
    budapest = city.trip("Budapest Deák Ferenc tér", "Budapest-Keleti")
    assert city.ticket_for(budapest, 1, 20)[1].price == Decimal(500)       # jedna linka
    assert city.ticket_for(budapest, 2, 20)[1].price == Decimal(600)       # s přestupem 30 min
    assert city.ticket_for(budapest, 2, 70)[1].price == Decimal(850)
    bratislava = city.trip("Bratislava, Hodžovo námestie", "Bratislava hl.st.")
    assert city.ticket_for(bratislava, 1, 45)[1].price == Decimal("1.80")
    assert city.passenger_factor("Vídeň", [30, 10, 4]) == Decimal("1.5")
    assert city.passenger_factor("Budapešť", [30, 70, 10]) == Decimal(1)


def test_idos_pick_stop_in_town():
    items = [{"selectedText": "Praha, Andělova; okres Praha", "description": "adresa, okres Praha", "value": "5"},
             {"selectedText": "Anděl", "description": "zastávka (Praha), autobusy", "value": "301003"},
             {"selectedText": "Andělská Hora; okres Bruntál", "description": "obec, okres Bruntál", "value": "1"}]
    assert idos.pick_stop(items, "Praha", False)["value"] == "301003"
    assert idos.pick_stop(items[:1], "Praha", True) is None
    assert idos.params("Anděl", "Praha,,Letňany", dt.datetime(2026, 10, 15, 10, tzinfo=DEFAULT_TZ),
                       ("301003", ""))["fc"] == "301003"


def test_olomouc_city_ticket():
    adult, senior = Passenger(age=30), Passenger(age=70)
    assert idsok.city_fare([("71", "710"), ("71",)], adult).price == Decimal(25)
    assert idsok.city_fare([("71",)], senior).price == Decimal(0)
    assert idsok.city_fare([("71",), ("72",)], adult) is None
