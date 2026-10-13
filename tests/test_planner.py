"""Plánovač tras po Evropě (Transitous) a trasování úvah hacku."""

from __future__ import annotations

import datetime as dt

from vyhledavac import trace
from vyhledavac.hacks import idos_complete
from vyhledavac.models import DEFAULT_TZ, Mode, SearchQuery
from vyhledavac.providers.planner import PlannerProvider


def leg(mode, frm, to, start, end, agency="", short="", trip="", stops=()):
    return {"mode": mode, "from": {"name": frm[0], "lat": frm[1], "lon": frm[2]},
            "to": {"name": to[0], "lat": to[1], "lon": to[2]},
            "startTime": start, "endTime": end, "agencyName": agency, "routeShortName": short,
            "tripShortName": trip, "intermediateStops": list(stops)}


CENTER = ("Kraków", 50.0614, 19.9366)
GLOWNY = ("Kraków Główny", 50.0677, 19.9476)
WAW = ("Warszawa Centralna", 52.2289, 21.0032)
LICHTENBERG = ("S+U Lichtenberg Bhf (Berlin)", 52.5099, 13.4969)
BERLIN_HBF = ("S+U Berlin Hauptbahnhof", 52.5251, 13.3694)
HAMBURG = ("Hamburg Hbf", 53.5530, 10.0067)


def itinerary():
    return {"legs": [
        leg("TRAM", CENTER, GLOWNY, "2026-10-15T04:00:00Z", "2026-10-15T04:10:00Z", "MPK", "18"),
        leg("WALK", GLOWNY, GLOWNY, "2026-10-15T04:10:00Z", "2026-10-15T04:15:00Z"),
        leg("HIGHSPEED_RAIL", GLOWNY, WAW, "2026-10-15T04:20:00Z", "2026-10-15T06:40:00Z", "PKP Intercity", "EIP",
            "3508"),
        leg("RAIL", WAW, LICHTENBERG, "2026-10-15T07:05:00Z", "2026-10-15T12:03:00Z", "PKP Intercity", "EC", "95"),
        leg("SUBURBAN", LICHTENBERG, BERLIN_HBF, "2026-10-15T12:10:00Z", "2026-10-15T12:26:00Z", "S-Bahn Berlin",
            "S7", "7100"),
        leg("HIGHSPEED_RAIL", BERLIN_HBF, HAMBURG, "2026-10-15T12:40:00Z", "2026-10-15T14:30:00Z",
            "DB Fernverkehr AG", "ICE", "176"),
    ]}


def test_planner_trims_city_transport_and_marks_local_legs():
    query = SearchQuery(origin="Krakov", destination="Hamburk",
                        departure=dt.datetime(2026, 10, 15, 6, tzinfo=DEFAULT_TZ))
    journey = PlannerProvider().to_journey(itinerary(), query)
    rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK]
    assert rides[0].origin == "Kraków Główny"                      # tramvaj k nádraží vypuštěná
    assert [leg.carrier for leg in rides] == ["PKP Intercity", "PKP Intercity", "S-Bahn Berlin",
                                              "DB Fernverkehr AG"]
    (plan,) = journey.raw["plans"]
    assert [seg["kind"] for seg in plan] == ["buy", "local", "buy"]   # S-Bahn po Berlíně = MHD
    assert plan[0]["from"] == [0, 0] and plan[0]["to"][0] == 1 and plan[-1]["from"] == [3, 0]


def test_city_inside_station_names():
    assert idos_complete._query_name("S+U Berlin Hauptbahnhof", True) == "Berlín"
    assert idos_complete._query_name("Malmö central train station FlixBus stop", True) == "Malmö"
    assert idos_complete._query_name("Warszawa Zachodnia", True) == "Varšava"


def test_trace_collects_nested_lines():
    lines: list[str] = []
    with trace.collect(lines):
        trace.trace("▶ spoj")
        with trace.nested():
            trace.trace("? A → B")
    trace.trace("mimo sběr")
    assert lines == ["▶ spoj", "  ? A → B"]
