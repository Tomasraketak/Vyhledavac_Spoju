"""Webové rozhraní – serializace, průběh hledání a HTTP API.

Dopravci jsou falešní, nic nesahá na síť ani na prohlížeč.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.request
from decimal import Decimal

import pytest

from vyhledavac.models import Mode, ProviderResult, SearchQuery
from vyhledavac.web import server as web
from vyhledavac.web.serialize import journey_key, journey_to_dict

from conftest import at, make_journey


def query():
    return SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))


def snapshot(session):
    return json.loads(session.snapshot)


# ---- serializace ------------------------------------------------------------
def test_journey_serialization_keeps_what_the_page_needs():
    journey = make_journey(provider="RegioJet", price=299, stops=["Praha", "Brno", "Vídeň"])
    data = journey_to_dict(journey, journey_key(journey))
    assert data["price"] == 299.0 and data["priceLabel"] == "299 Kč"
    assert data["dep"].startswith("2026-10-01T08:00") and data["minutes"] == 240
    assert data["legs"][0]["mode"] == "train"
    assert [s["name"] for s in data["legs"][0]["stops"]] == ["Praha", "Brno", "Vídeň"]
    json.dumps(data)                                     # musí jít poslat


def test_journey_key_survives_price_update():
    """Rozbalený detail nesmí zmizet, když dopravce doplní cenu."""
    before = make_journey(provider="ZSSK")
    after = make_journey(provider="ZSSK", price=234)
    assert journey_key(before) == journey_key(after)


# ---- průběh hledání -----------------------------------------------------------
def session(providers=("RegioJet", "ZSSK", "OneTicket")):
    return web.SearchSession(query(), list(providers), [])


def test_progress_shows_timetable_before_prices():
    s = session()
    s.on_progress(ProviderResult(provider="ZSSK", partial=True,
                                 journeys=[make_journey(provider="ZSSK", dep=at(9), arr=at(13))]))
    data = snapshot(s)
    zssk = next(p for p in data["providers"] if p["name"] == "ZSSK")
    assert zssk["status"] == "running" and "ceny 0/1" in zssk["detail"]
    assert len(data["journeys"]) == 1 and data["journeys"][0]["price"] is None

    # Dorazila cena – stejný spoj, ne nový.
    s.on_progress(ProviderResult(provider="ZSSK", partial=True,
                                 journeys=[make_journey(provider="ZSSK", dep=at(9), arr=at(13), price=234)]))
    data = snapshot(s)
    assert len(data["journeys"]) == 1 and data["journeys"][0]["price"] == 234.0


def test_failures_go_to_the_problem_panel_not_away_with_results():
    s = session()
    s.on_partial(ProviderResult(provider="RegioJet", journeys=[make_journey(provider="RegioJet", price=240)],
                                elapsed=1.2))
    s.on_partial(ProviderResult(provider="ZSSK", error="blokováno ochranou (HTTP 403)", blocked=True))
    s.on_partial(ProviderResult(provider="OneTicket", error="OneTicket nezná stanici 'Vídeň'",
                                not_served=True))
    data = snapshot(s)
    status = {p["name"]: p for p in data["providers"]}
    assert status["RegioJet"]["status"] == "ok"
    assert status["ZSSK"]["status"] == "blocked"
    assert status["OneTicket"]["detail"] == "nejezdí tam"
    kinds = {p["provider"]: p for p in data["problems"]}
    assert kinds["ZSSK"]["kind"] == "blocked" and kinds["ZSSK"]["canDisable"]
    assert kinds["OneTicket"]["kind"] == "not_served" and not kinds["OneTicket"]["canDisable"]
    assert len(data["journeys"]) == 1                    # RegioJet zůstal


def test_dominated_journeys_are_flagged_not_dropped():
    s = session(("RegioJet",))
    cheap = make_journey(provider="RegioJet", dep=at(8), arr=at(12), price=200)
    worse = make_journey(provider="RegioJet", dep=at(8), arr=at(12, 30), price=400, line="EC 2")
    s.on_partial(ProviderResult(provider="RegioJet", journeys=[cheap, worse]))
    flags = {j["priceLabel"]: j["dominated"] for j in snapshot(s)["journeys"]}
    assert flags == {"200 Kč": False, "400 Kč": True}


def test_cancel_marks_running_providers():
    s = session()
    s.finish(cancelled=True)
    data = snapshot(s)
    assert data["done"] and data["cancelled"]
    assert {p["status"] for p in data["providers"]} == {"off"}


def test_isic_over_26_is_explained():
    from vyhledavac.models import DiscountCard, Passenger

    q = SearchQuery(origin="Praha", destination="Brno", departure=at(8),
                    passengers=(Passenger(age=30, cards=(DiscountCard.ISIC,)),))
    s = web.SearchSession(q, ["RegioJet"], [])
    assert any(p["provider"] == "ISIC" for p in snapshot(s)["problems"])


# ---- vstup ------------------------------------------------------------------
def test_parse_query():
    q = web.parse_query({"from": "Praha", "to": "Brno", "date": "2026-10-15", "time": "08:30",
                         "age": 22, "isic": True})
    assert q.departure == dt.datetime(2026, 10, 15, 8, 30, tzinfo=q.departure.tzinfo)
    assert q.lead.age == 22 and q.lead.cards


@pytest.mark.parametrize("body", [{"from": "", "to": "Brno"},
                                  {"from": "A", "to": "B", "date": "15.10.2026"},
                                  {"from": "A", "to": "B", "age": "abc"}])
def test_parse_query_rejects_bad_input(body):
    with pytest.raises(ValueError):
        web.parse_query(body)


# ---- HTTP -------------------------------------------------------------------
class _FakeApp(web.WebApp):
    """Bez asyncio vlákna a kurzů – stačí na kontrolu HTTP vrstvy."""

    def __init__(self):  # noqa: D107 - záměrně bez rodiče
        self.sessions = {}
        self.lock = threading.Lock()
        self.last_seen = 0.0

    def meta(self):
        return {"providers": [{"name": "RegioJet"}], "hacks": [], "eurRate": 25.0}

    def start_search(self, body):
        q = web.parse_query(body)
        s = web.SearchSession(q, ["RegioJet"], [])
        s.on_partial(ProviderResult(provider="RegioJet",
                                    journeys=[make_journey(provider="RegioJet", price=199)]))
        s.finish()
        self.sessions[s.id] = s
        return s


@pytest.fixture
def http():
    srv = web.make_server(_FakeApp(), port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read().decode("utf-8")


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_static_page_and_meta(http):
    status, kind, body = _get(http + "/")
    assert status == 200 and kind.startswith("text/html") and "Vyhledávač spojů" in body
    assert _get(http + "/app.js")[0] == 200
    assert json.loads(_get(http + "/api/meta")[2])["providers"][0]["name"] == "RegioJet"


def test_static_does_not_leave_its_folder(http):
    with pytest.raises(urllib.error.HTTPError):
        _get(http + "/../server.py")


def test_search_and_event_stream(http):
    status, body = _post(http + "/api/search", {"from": "Praha", "to": "Brno",
                                                "date": "2026-10-01", "time": "08:00"})
    assert status == 200
    _, kind, stream = _get(f"{http}/api/search/{body['id']}/events")
    assert kind.startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
    assert events[-1]["done"] and events[-1]["journeys"][0]["price"] == 199.0


def test_search_validation_error_is_readable(http):
    status, body = _post(http + "/api/search", {"from": "", "to": "Brno"})
    assert status == 400 and "odkud" in body["error"]
