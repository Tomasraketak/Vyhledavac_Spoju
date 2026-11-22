"""Testy oprav chyb, výkonnostních optimalizací a cenového kalendáře."""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import json
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal

import httpx
import pytest

from vyhledavac import cache as cache_mod
from vyhledavac import calendar_scan, cli, geo, stations, transfer
from vyhledavac.cache import Cache, MemoryCache
from vyhledavac.engine import SearchEngine
from vyhledavac.filters.builtin import Deduplicate, PriceBackfill
from vyhledavac.http import HttpClient, RetryPolicy, _parse_retry_after
from vyhledavac.models import (
    DEFAULT_TZ,
    DiscountCard,
    Mode,
    Passenger,
    ProviderResult,
    SearchQuery,
)
from vyhledavac.normalize import normalize
from vyhledavac.providers._oebb_shop import OebbShop
from vyhledavac.providers.base import Provider
from vyhledavac.providers.obb import ObbProvider
from vyhledavac.web import server as web

from conftest import at, make_journey


# ---- HTTP: rotace identity a Retry-After ------------------------------------
@pytest.mark.asyncio
async def test_rotate_identity_changes_profile_and_clears_chrome_headers():
    async with HttpClient(retry=RetryPolicy(attempts=2)) as client:
        first_ua = client._client.headers["User-Agent"]
        seen_uas = {first_ua}
        for _ in range(10):
            client.rotate_identity()
            ua = client._client.headers["User-Agent"]
            seen_uas.add(ua)
            if "Firefox" in ua:
                assert "sec-ch-ua" not in client._client.headers
                assert "sec-ch-ua-mobile" not in client._client.headers
                assert "sec-ch-ua-platform" not in client._client.headers
        assert len(seen_uas) >= 2


@pytest.mark.asyncio
async def test_http_retry_uses_rotated_headers_and_no_429_double_sleep(monkeypatch):
    seen_uas: list[str] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_uas.append(request.headers.get("User-Agent", ""))
        if len(seen_uas) == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with HttpClient(retry=RetryPolicy(attempts=3, base_delay=0.5)) as client:
        client._client = httpx.AsyncClient(transport=transport, headers=client._client.headers)
        data = await client.get_json("https://example.test/api")

    assert data == {"ok": True}
    assert len(seen_uas) == 2
    assert seen_uas[0] != seen_uas[1]
    # Přesně jedno čekání z Retry-After – žádné zdvojené čekání s backoffem.
    # (HostLimiter může vložit krátkou pauzu < 0.5 s mezi 1. a 2. požadavkem.)
    retry_sleeps = [s for s in sleeps if s >= 1.0]
    assert retry_sleeps == [1.5]


def test_parse_retry_after_numeric_and_http_date():
    assert _parse_retry_after("3.5") == 3.5
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)
    header = email.utils.format_datetime(future, usegmt=True)
    parsed = _parse_retry_after(header)
    assert parsed is not None and 0.0 <= parsed <= 6.5
    assert _parse_retry_after("invalid") is None


# ---- Filtry: nemutují vstupní objekty Journey -------------------------------
def test_deduplicate_and_price_backfill_do_not_mutate_inputs():
    q = SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))
    j_priced = make_journey(provider="RegioJet", dep=at(8), arr=at(12), price=299, line="RJ 1031")
    j_unpriced = make_journey(provider="Plánovač", dep=at(8), arr=at(12), price=None, line="RJ 1031")
    orig_fares_p = list(j_priced.fares)
    orig_notes_p = list(j_priced.notes)
    orig_fares_u = list(j_unpriced.fares)
    orig_notes_u = list(j_unpriced.notes)

    deduped = Deduplicate().apply([j_priced, j_unpriced], q)
    assert len(deduped) == 1
    assert j_priced.fares == orig_fares_p and j_priced.notes == orig_notes_p
    assert j_unpriced.fares == orig_fares_u and j_unpriced.notes == orig_notes_u

    # PriceBackfill s jinými odjezdy stejného čísla vlaku (jinak by je Deduplicate sloučil).
    donor = make_journey(provider="ÖBB", dep=at(8, 5), arr=at(12, 5), price=450, line="EC 275")
    target = make_journey(provider="Plánovač", dep=at(8, 0), arr=at(12, 0), price=None, line="EC 275")
    orig_t_fares = list(target.fares)
    orig_t_notes = list(target.notes)

    filled = PriceBackfill().apply([donor, target], q)
    filled_target = next(j for j in filled if j.provider == "Plánovač")
    assert filled_target.price == Decimal("450")
    assert target.fares == orig_t_fares and target.notes == orig_t_notes


# ---- Cache: iter_namespace v MemoryCache, mazání expirovaných, limit RAM ----
def test_memory_cache_iter_namespace_and_expiry():
    cache = MemoryCache()
    cache.set("ns", "a", {"x": 1}, ttl=60)
    cache.set("ns", "b", {"x": 2}, ttl=-1)
    cache.set("other", "c", {"x": 3}, ttl=60)
    rows = dict(cache.iter_namespace("ns"))
    assert rows == {"a": {"x": 1}}


def test_sqlite_cache_purges_expired_and_bounds_memory(tmp_path, monkeypatch):
    db_path = tmp_path / "cache.sqlite"
    c1 = Cache(db_path)
    c1.set("ns", "old", "expired", ttl=-5)
    c1.set("ns", "fresh", "alive", ttl=300)
    c1.close()

    # Nové otevření smaže expirované záznamy.
    c2 = Cache(db_path)
    assert c2.get("ns", "old") is None
    assert c2.get("ns", "fresh") == "alive"

    monkeypatch.setattr(cache_mod, "_MAX_MEMORY_ITEMS", 5)
    for i in range(12):
        c2.set("ns", f"k{i}", i, ttl=60)
    assert len(c2._memory) <= 5
    # Vše je stále uložené v SQLite.
    assert c2.get("ns", "k0") == 0
    c2.close()


# ---- Stanice a geo: indexované hledání vrací totéž co lineární průchod ------
def test_station_suggest_prefix_index_matches_linear_scan():
    db = stations.database()
    if not db.places:
        pytest.skip("Chybí databáze stanic")
    from vyhledavac.stations import _all_words

    def linear_suggest(query: str, limit: int = 10):
        q = normalize(query)
        if not q:
            return []
        words = q.split()
        w0 = words[0]
        starts = []
        others = []
        for place in db.places:
            best = 2
            for key in place.keys:
                if key.startswith(q):
                    best = 0
                    break
                if best > 1 and w0 in key and _all_words(words, key):
                    best = 1
            if best == 0:
                starts.append(place)
                if len(starts) >= limit:
                    break
            elif best == 1 and len(others) < limit:
                others.append(place)
        return (starts + others)[:limit]

    for q in ("hl brno", "domanin", "praha hl", "wien", "a", "xyzzy_neexistuje", "České Bud"):
        assert db.suggest(q, limit=10) == linear_suggest(q, limit=10), f"neshoda pro {q!r}"


def test_geo_country_and_state_precedence():
    assert geo.country_of("Praha") == "CZ"
    assert geo.country_of("Flughafen Wien") == "AT"
    assert geo.state_of("Flughafen Wien") == "N"
    assert geo.state_of("Wien Hbf") == "W"
    assert geo.state_of("Salzburg Hbf") == "S"
    assert geo.state_of("Graz Hbf") == "ST"
    assert geo.country_of("München Hbf") == "DE"
    assert geo.country_of("Gdańsk Główny") == "PL"


# ---- Transfer: bezpečné použití napříč více asyncio.run() -------------------
def test_transfer_http_client_survives_multiple_event_loops():
    async def grab():
        return transfer._get_client()

    c1 = asyncio.run(grab())
    c2 = asyncio.run(grab())
    assert c1 is not c2


# ---- Engine: chytré přeskakování pro přímé hledání + _leg_cache klíč --------
class _DummyProvider(Provider):
    def __init__(self, name: str, countries: tuple[str, ...], *, international_only: bool = False):
        super().__init__()
        self.name = name
        self.countries = countries
        self.international_only = international_only
        self.calls: list[SearchQuery] = []

    async def search(self, query: SearchQuery):
        self.calls.append(query)
        price = 100 if query.lead.age < 26 else 250
        return [make_journey(provider=self.name, origin=query.origin, destination=query.destination,
                             dep=query.departure, arr=query.departure + dt.timedelta(hours=2), price=price)]


def test_engine_route_skips_irrelevant_providers_but_keeps_hacks_working():
    q = SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))
    p_mav = _DummyProvider("MÁV", ("HU",), international_only=True)
    p_cp = _DummyProvider("CP", ("PT",), international_only=True)
    p_mhd = _DummyProvider("MHD", ("CZ",))
    p_bileto = _DummyProvider("Bileto", ("CZ",))
    p_rj = _DummyProvider("RegioJet", ("CZ", "SK", "AT", "HU", "PL", "DE"))
    p_obb = _DummyProvider("ÖBB", ("AT", "CZ", "DE", "SK"))

    engine = SearchEngine([p_mav, p_cp, p_mhd, p_bileto, p_rj, p_obb])
    skips = engine._route_skips(q)
    assert "MÁV" in skips
    assert "CP" in skips
    assert "MHD" in skips
    assert "Bileto" in skips
    assert "RegioJet" not in skips
    assert "ÖBB" not in skips


@pytest.mark.asyncio
async def test_engine_leg_search_cache_respects_passengers_and_modes():
    prov = _DummyProvider("RegioJet", ("CZ", "AT"))
    engine = SearchEngine([prov])
    engine._skipped = set()

    q_adult = SearchQuery(origin="Praha", destination="Brno", departure=at(8),
                          passengers=(Passenger(age=30),))
    q_student = SearchQuery(origin="Praha", destination="Brno", departure=at(8),
                            passengers=(Passenger(age=20, cards=(DiscountCard.ISIC,)),))
    q_train = SearchQuery(origin="Praha", destination="Brno", departure=at(8),
                          passengers=(Passenger(age=30),), modes=frozenset({Mode.TRAIN}))

    r1 = await engine._leg_search(q_adult, only=["RegioJet"])
    r2 = await engine._leg_search(q_adult, only=["RegioJet"])
    r3 = await engine._leg_search(q_student, only=["RegioJet"])
    r4 = await engine._leg_search(q_train, only=["RegioJet"])

    assert len(prov.calls) == 3  # r2 z cache, r3 a r4 nové klíče
    assert r1[0].price == Decimal("250")
    assert r2[0].price == Decimal("250")
    assert r3[0].price == Decimal("100")
    assert r4[0].price == Decimal("250")


# ---- ÖBB: cache stanic a znovupoužití relace --------------------------------
@pytest.mark.asyncio
async def test_oebb_shop_caches_station_lookups():
    calls = 0

    class _FakeTransport:
        async def get(self, url, params, headers):
            nonlocal calls
            if "/init" in url:
                return {"accessToken": "tok", "sessionId": "sid"}
            calls += 1
            return [{"name": "Wien Hbf", "number": 1290401}]

        async def post(self, url, body, headers):
            return {}

        async def close(self):
            pass

    cache = MemoryCache()
    shop = OebbShop("ÖBB", _FakeTransport(), cache=cache)
    s1 = await shop.station("Vídeň")
    s2 = await shop.station("Vídeň")
    assert s1 == {"name": "Wien Hbf", "number": 1290401}
    assert s2 == s1
    assert calls == 1


@pytest.mark.asyncio
async def test_obb_provider_reuses_direct_shop_and_closes_on_aclose():
    closed = []

    class _FakeShop:
        async def close(self):
            closed.append(True)

    prov = ObbProvider(cache=MemoryCache())
    prov._direct_shop = _FakeShop()  # type: ignore[assignment]
    await prov.aclose()
    assert closed == [True]
    assert prov._direct_shop is None


# ---- Cenový kalendář (calendar_scan) ----------------------------------------
def test_calendar_date_window_never_goes_into_past():
    today = dt.date(2026, 10, 10)
    # Střed dnes -> posune začátek na dnes, vrátí 7 dní.
    w = calendar_scan.date_window(today, days=7, today=today)
    assert len(w) == 7
    assert w[0] == today
    assert w[-1] == dt.date(2026, 10, 16)

    # Střed v budoucnosti -> vycentrovaný (±3 dny).
    center = dt.date(2026, 10, 20)
    w2 = calendar_scan.date_window(center, days=7, today=today)
    assert w2[0] == dt.date(2026, 10, 17)
    assert w2[3] == center
    assert w2[-1] == dt.date(2026, 10, 23)

    # Omezení na MAX_DAYS.
    assert len(calendar_scan.date_window(center, days=99, today=today)) == calendar_scan.MAX_DAYS


def test_select_fast_providers_excludes_browser_and_non_priced():
    chosen = calendar_scan.select_fast_providers(
        ["RegioJet", "FlixBus", "ÖBB", "ČD", "ZSSK", "IDS", "Plánovač", "MHD", "Transitous"]
    )
    assert "RegioJet" in chosen
    assert "FlixBus" in chosen
    assert "ÖBB" in chosen
    assert "ČD" not in chosen
    assert "ZSSK" not in chosen
    assert "IDS" not in chosen
    assert "Plánovač" not in chosen
    assert "MHD" not in chosen


@pytest.mark.asyncio
async def test_scan_calendar_finds_cheapest_per_day_and_streams_updates():
    class _PriceByDayProvider(Provider):
        name = "RegioJet"
        countries = ("CZ", "AT")

        async def search(self, query: SearchQuery):
            day = query.date.day
            # Den 11 (okolní): ranní spoj v 06:30 za 320 Kč (okolní dny hledají od 05:00).
            # Den 12 (střed, dotaz od 10:00): spoj v 11:00 za 199 Kč vlakem a 350 Kč busem.
            # Den 13: jen autobus (600 Kč), ale dotaz chce trains_only -> prázdné.
            if day == 11:
                dep = dt.datetime(2026, 10, day, 6, 30, tzinfo=DEFAULT_TZ)
                return [make_journey(provider="RegioJet", dep=dep, arr=dep + dt.timedelta(hours=4),
                                     price=320, mode=Mode.TRAIN)]
            if day == 12:
                dep = dt.datetime(2026, 10, day, 11, 0, tzinfo=DEFAULT_TZ)
                return [
                    make_journey(provider="RegioJet", dep=dep, arr=dep + dt.timedelta(hours=4),
                                 price=199, mode=Mode.TRAIN),
                    make_journey(provider="RegioJet", dep=dep, arr=dep + dt.timedelta(hours=4),
                                 price=350, mode=Mode.BUS),
                ]
            dep = dt.datetime(2026, 10, day, 8, 0, tzinfo=DEFAULT_TZ)
            return [make_journey(provider="RegioJet", dep=dep, arr=dep + dt.timedelta(hours=4),
                                 price=600, mode=Mode.BUS)]

    events: list[calendar_scan.DayPrice] = []
    q = SearchQuery(origin="Praha", destination="Vídeň",
                    departure=dt.datetime(2026, 10, 12, 10, 0, tzinfo=DEFAULT_TZ),
                    modes=frozenset({Mode.TRAIN}))
    days = await calendar_scan.scan_calendar(
        q,
        providers=[_PriceByDayProvider()],
        days=3,
        today=dt.date(2026, 10, 10),
        on_day=events.append,
    )
    assert [d.date.day for d in days] == [11, 12, 13]
    by_day = {d.date.day: d for d in days}
    assert by_day[11].min_price == Decimal("320")
    assert by_day[12].min_price == Decimal("199")
    # Den 13 měl jen autobus, ale dotaz byl trains_only -> žádná cena.
    assert by_day[13].min_price is None and by_day[13].status == "empty"
    assert any(e.status == "running" for e in events)
    assert any(e.status == "ok" and e.date.day == 12 for e in events)


# ---- Web server: trainsOnly, _skip_detail, cancellation, /api/calendar ------
def test_web_parse_query_trains_only_and_skip_details():
    q = web.parse_query({"from": "Praha", "to": "Brno", "date": "2026-10-15", "time": "08:00", "trainsOnly": True})
    assert q.trains_only is True
    assert q.modes == frozenset({Mode.TRAIN})

    assert web._skip_detail("jen MHD po jednom městě") == "jen MHD"
    assert web._skip_detail("jen cesty po Česku") == "jen Česko"
    assert web._skip_detail("mimo oblast dopravce") == "mimo oblast"
    assert web._skip_detail("jen zahraničí") == "jen zahraničí"


def test_web_cancelled_search_ignores_late_partials():
    q = SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))
    s = web.SearchSession(q, ["RegioJet"], [])
    s.finish(cancelled=True)
    v_after_cancel = s.version
    s.on_partial(ProviderResult(provider="RegioJet", journeys=[make_journey(provider="RegioJet", price=200)]))
    assert s.version == v_after_cancel
    assert json.loads(s.snapshot)["journeys"] == []


def test_web_calendar_api_endpoints_stream_and_cancel(monkeypatch):
    async def fake_scan(query, providers, *, days=7, on_day=None, **kwargs):
        out = []
        for i, d in enumerate(calendar_scan.date_window(query.date, days)):
            entry = calendar_scan.DayPrice(
                date=d,
                status="ok",
                min_price=Decimal(250 + i * 50),
                provider="RegioJet",
                departure=dt.datetime(d.year, d.month, d.day, 8, 15, tzinfo=DEFAULT_TZ),
                duration_minutes=240,
                transfers=0,
                journey_count=3,
                priced_count=3,
            )
            if on_day:
                on_day(entry)
            out.append(entry)
        return out

    monkeypatch.setattr(calendar_scan, "scan_calendar", fake_scan)

    class _MiniApp(web.WebApp):
        def __init__(self):
            from vyhledavac.registry import load_plugins
            load_plugins()
            self.runner = web.AsyncRunner()
            self.sessions = {}
            self.calendars = {}
            self.lock = threading.Lock()
            self.last_seen = time.monotonic()

    app = _MiniApp()
    server = web.make_server(app, 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/calendar",
            data=json.dumps({"from": "Praha", "to": "Vídeň", "date": "2026-10-15", "time": "08:00",
                             "providers": ["RegioJet", "FlixBus"], "days": 5}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        cid = created["id"]

        events = []
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/calendar/{cid}/events", timeout=5) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        assert events and events[-1]["done"] is True
        assert len(events[-1]["days"]) == 5
        assert events[-1]["days"][0]["price"] == 250.0

        # Cancel na už dokončenou relaci vrátí 200 ok.
        cancel_req = urllib.request.Request(f"http://127.0.0.1:{port}/api/calendar/{cid}/cancel", method="POST")
        with urllib.request.urlopen(cancel_req, timeout=5) as resp:
            assert resp.status == 200

        # Špatný Content-Length nesmí shodit server.
        bad_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/calendar",
            data=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(bad_req, timeout=5)
        assert exc.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        app.runner.stop()


# ---- CLI: --flex ------------------------------------------------------------
def test_cli_flex_parsing_and_rendering(capsys):
    parser = cli.build_parser()
    args_default = parser.parse_args(["Praha", "Vídeň", "--flex"])
    assert args_default.flex == 7
    args_custom = parser.parse_args(["Praha", "Vídeň", "--flex", "5", "--json"])
    assert args_custom.flex == 5 and args_custom.json is True

    q = SearchQuery(origin="Praha", destination="Vídeň",
                    departure=dt.datetime(2026, 10, 12, 8, 0, tzinfo=DEFAULT_TZ))
    sample = [
        calendar_scan.DayPrice(date=dt.date(2026, 10, 11), status="ok", min_price=Decimal("450"),
                               provider="ÖBB", departure=dt.datetime(2026, 10, 11, 9, 0, tzinfo=DEFAULT_TZ),
                               duration_minutes=245, transfers=0, journey_count=4, priced_count=4),
        calendar_scan.DayPrice(date=dt.date(2026, 10, 12), status="ok", min_price=Decimal("390"),
                               provider="RegioJet", departure=dt.datetime(2026, 10, 12, 8, 15, tzinfo=DEFAULT_TZ),
                               duration_minutes=240, transfers=0, journey_count=5, priced_count=5),
        calendar_scan.DayPrice(date=dt.date(2026, 10, 13), status="ok", min_price=Decimal("249"),
                               provider="RegioJet", departure=dt.datetime(2026, 10, 13, 6, 30, tzinfo=DEFAULT_TZ),
                               duration_minutes=240, transfers=0, journey_count=5, priced_count=5),
    ]

    cli.render_calendar(sample, q, as_json=False)
    out = capsys.readouterr().out
    assert "CENOVÝ KALENDÁŘ: Praha → Vídeň" in out
    assert "249 Kč" in out
    assert "ušetříte 141 Kč" in out

    cli.render_calendar(sample, q, as_json=True)
    js = json.loads(capsys.readouterr().out)
    assert js["origin"] == "Praha" and len(js["days"]) == 3
    assert js["days"][2]["price"] == 249.0
