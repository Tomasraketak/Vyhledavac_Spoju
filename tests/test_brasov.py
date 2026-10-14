"""Třeboň → Brašov (říjen 2026): cizí města podle jména, jednotní dopravci, IDS za regionální bus."""

import datetime as dt
from decimal import Decimal

from vyhledavac import geo, stations
from vyhledavac.hacks.idos_complete import _bare_route, ids_fare
from vyhledavac.models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery
from vyhledavac.normalize import carrier_name
from vyhledavac.providers import transitous_jr
from vyhledavac.providers._hafas import _coords


def test_austrian_slash_names_are_austrian():
    # samotné „Neumarkt“ je v databázi italská Egna, „Braunau“ tam není vůbec
    assert geo.country_of("Neumarkt/Wallersee Bahnhof") == "AT"
    assert geo.country_of("Braunau/Inn Bahnhof") == "AT"
    assert geo.country_of("Linz/Donau Hbf") == "AT"


def test_station_far_from_city():
    # HAFAS: Braunau/Inn je od Brašova ~1000 km – takovou stanici nebrat
    assert stations.far_from("Brašov", *_coords({"crd": {"x": 13035887, "y": 48250922}}))
    assert not stations.far_from("Brašov", *_coords({"crd": {"x": 25613711, "y": 45661516}}))
    assert not stations.far_from("Brašov", None, None)


def test_carrier_names_unified():
    assert {carrier_name(n) for n in ("Ceske Drahy", "ČD", "České dráhy, a.s.")} == {"ČD"}
    assert {carrier_name(n) for n in ("OEBB Personenverkehr AG Kundenservice", "ÖBB")} == {"ÖBB"}
    assert {carrier_name(n) for n in ("FlixBus-eu", "FlixBus")} == {"FlixBus"}
    assert carrier_name("IDS JMK (Data from: KORDIS JMK, DPMB)") == "IDS JMK"
    assert carrier_name("ČSAD AUTOBUSY České Budějovice a.s.") == "ČSAD AUTOBUSY České Budějovice a.s."


def _at(h, m=0):
    return dt.datetime(2026, 10, 15, h, m, tzinfo=DEFAULT_TZ)


def test_regional_bus_priced_by_ids_tariff():
    q = SearchQuery("Třeboň", "Brašov", _at(5))
    bus = Leg("Třeboň,Táboritská", "České Budějovice,aut.nádr.", _at(5, 12), _at(5, 52), Mode.BUS,
              "ČSAD AUTOBUSY České Budějovice a.s.", "Bus 340100 101")
    fare = ids_fare(q, [bus])
    assert fare is not None and fare.provider == "IDESKA" and fare.price == Decimal("49")
    ec = Leg("České Budějovice", "Linz Hbf", _at(8, 5), _at(10, 6), Mode.TRAIN, "České dráhy, a.s.", "EC 331")
    assert ids_fare(q, [ec]) is None                 # EC: IDS neplatí, ani do ciziny


def test_unpriced_provider_route_goes_to_hack():
    legs = [Leg("Třeboň", "Veselí n.Lužnicí", _at(10, 58), _at(11, 23), Mode.TRAIN, "ČD", "Os 8712"),
            Leg("Veselí n.Lužnicí", "Brasov", _at(11, 28), _at(20, 57), Mode.TRAIN, "ČD", "IC 473")]
    assert _bare_route(Journey(provider="ČD", legs=legs, fare_status=FareStatus.UNKNOWN))
    assert not _bare_route(Journey(provider="Plánovač", legs=legs, fare_status=FareStatus.UNKNOWN))
    assert not _bare_route(Journey(provider="ČD", legs=legs[:1], fare_status=FareStatus.UNKNOWN))


def test_transitous_ends_at_station_in_foreign_city():
    legs = [Leg("Budapest Airport", "Stadionul Municipal", _at(10), _at(20), Mode.BUS, "FlixBus-eu", "FlixBus N3906"),
            Leg("Stadionul Municipal", "Căprioara", _at(20, 5), _at(20, 28), Mode.BUS, "RATBV S.A.", "Bus 23")]
    journey = transitous_jr._trim_foreign_ends(Journey(provider="Transitous", legs=legs),
                                               SearchQuery("Třeboň", "Brašov", _at(5)))
    assert [leg.destination for leg in journey.legs] == ["Stadionul Municipal"]


def test_unpriced_twin_of_priced_hack_hidden():
    from vyhledavac.filters.builtin import Deduplicate

    legs = [Leg("Třeboň", "Brasov", _at(8, 7), _at(11, 3) + dt.timedelta(days=1), Mode.TRAIN, "ČD", "R 726")]
    bare = Journey(provider="IDS", legs=list(legs), fare_status=FareStatus.UNKNOWN)
    piece = Journey(provider="MÁV", legs=list(legs), fare_status=FareStatus.PRICED)
    from vyhledavac.models import Fare
    piece.fares = [Fare(amount=Decimal(1830), currency="CZK", amount_czk=Decimal(1830))]
    hack = Journey(provider="IDOS + MÁV", legs=list(legs), segments=[piece], fares=list(piece.fares),
                   fare_status=FareStatus.PRICED)
    kept = Deduplicate().apply([bare, hack], SearchQuery("Třeboň", "Brašov", _at(5)))
    assert kept == [hack]


def test_flixbus_transfer_legs():
    """FlixBus posílá přestupy v ``legs`` – spoj Třeboň → Brašov má 2 přestupy, ne přímý."""
    from vyhledavac.cache import MemoryCache
    from vyhledavac.providers.flixbus import FlixBusProvider

    def node(date, station):
        return {"date": date, "station_id": station, "city_id": "c-" + station}
    raw = {"departure": node("2026-10-16T13:40:00+02:00", "t"), "arrival": node("2026-10-17T18:30:00+03:00", "b"),
           "legs": [{"departure": node("2026-10-16T13:40:00+02:00", "t"), "arrival": node("2026-10-16T16:55:00+02:00", "brno"),
                     "means_of_transport": "bus"},
                    {"departure": node("2026-10-16T21:50:00+02:00", "brno"), "arrival": node("2026-10-17T00:05:00+02:00", "wien"),
                     "means_of_transport": "bus"},
                    {"departure": node("2026-10-17T01:25:00+02:00", "wien"), "arrival": node("2026-10-17T18:30:00+03:00", "b"),
                     "means_of_transport": "bus"}]}
    names = {"t": "Třeboň (aut. nádr.)", "brno": "Brno (aut. nádr. Hotel Grand)", "wien": "Vídeň, hlavní nádraží",
             "b": "Brașov (Autogara Internationala Bartolomeu)"}
    stations_ = {k: {"name": v} for k, v in names.items()}
    p = FlixBusProvider(cache=MemoryCache())
    legs = p._build_legs(raw, stations_, {}, _at(13, 40), _at(17, 30), "Třeboň", "Brašov")
    journey = Journey(provider="FlixBus", legs=legs)
    assert journey.transfers == 2
    assert [leg.destination for leg in legs] == [names["brno"], names["wien"], names["b"]]
