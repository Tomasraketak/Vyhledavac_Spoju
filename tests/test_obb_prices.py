"""Ceny ÖBB z e-shopu a odolnost vůči jeho selhání.

Shop se tu nahrazuje falešným transportem. Tvary odpovědí odpovídají
open-source klientovi ``oebb`` (juliuste/oebb).
"""

import asyncio
import datetime as dt
from decimal import Decimal

import pytest

from vyhledavac.cache import MemoryCache
from vyhledavac.errors import BlockedError, TransientProviderError
from vyhledavac.models import FareStatus, SearchQuery
from vyhledavac.providers import _oebb_shop as shop_mod
from vyhledavac.providers import obb as obb_mod
from vyhledavac.providers._oebb_shop import OebbShop, apply_offer, parse_connection, parse_time
from vyhledavac.providers.obb import ObbProvider, merge_offers

from conftest import at, make_journey


def conn(cid, dep, arr, line=("RJX", "165")):
    return {
        "id": cid,
        "from": {"name": "Wien Hbf", "departure": dep},
        "to": {"name": "Salzburg Hbf", "arrival": arr},
        "sections": [{
            "from": {"name": "Wien Hbf", "departure": dep},
            "to": {"name": "Salzburg Hbf", "arrival": arr},
            "category": {"name": line[0], "shortName": line[0], "number": line[1], "train": True},
        }],
    }


class FakeTransport:
    """Odpovídá jako shop ÖBB. ``fail_on`` nasimuluje selhání konkrétního kroku."""

    def __init__(self, fail_on=None, error=BlockedError):
        self.calls = []
        self.fail_on = fail_on
        self.error = error

    def _check(self, url):
        self.calls.append(url)
        if self.fail_on and self.fail_on in url:
            raise self.error("ÖBB", "simulace")

    async def get(self, url, params, headers):
        self._check(url)
        if "/init" in url:
            return {"accessToken": "anon-token", "sessionId": "s1", "supportId": "42"}
        if "/stations" in url:
            name = dict(params)["name"]
            return [{"number": 1290401 if "Wien" in name else 8100002, "name": name}]
        if "/prices" in url:
            assert headers["AccessToken"] == "anon-token"
            ids = [v for k, v in params if k == "connectionIds[]"]
            offers = {
                "c1": {"connectionId": "c1", "price": 19.9, "firstClass": 39.9,
                       "availabilityState": "available"},
                "c2": {"connectionId": "c2", "price": 0, "availabilityState": "soldOut"},
                # Současný tvar odpovědi: bez availabilityState.
                "c3": {"connectionId": "c3", "price": 29.9, "offerError": False,
                       "firstClass": False},
            }
            return {"offers": [offers[i] for i in ids if i in offers]}
        raise AssertionError(url)

    async def post(self, url, body, headers):
        self._check(url)
        if "/travelActions" in url:
            return {"travelActions": [{"id": "ta-1", "entrypoint": {"id": "timetable"}}]}
        if "/timetable" in url and "Scroll" not in url:
            assert body["travelActionId"] == "ta-1"
            # Prázdný příjezd by shop přiměl ignorovat datum odjezdu.
            assert "datetimeArrival" not in body
            self.timetable_body = body
            return {"connections": [
                conn("c1", "2026-10-01T08:28:00.000", "2026-10-01T10:53:00.000"),
                conn("c2", "2026-10-01T08:57:00.000", "2026-10-01T11:49:00.000"),
            ]}
        if "timetableScroll" in url:
            return {"connections": [
                conn("c3", "2026-10-01T09:28:00.000", "2026-10-01T11:53:00.000"),
            ]}
        raise AssertionError(url)

    async def close(self):
        pass


def query():
    return SearchQuery(origin="Wien", destination="Salzburg", departure=at(8))


# ---- parsování ------------------------------------------------------------
def test_naive_shop_time_is_vienna_local():
    parsed = parse_time("2026-10-01T08:28:00.000")
    assert (parsed.hour, parsed.utcoffset()) == (8, dt.timedelta(hours=2))


def test_utc_shop_time_is_converted():
    assert parse_time("2026-10-01T06:28:00.000Z").hour == 8


def test_only_available_offer_counts_as_price():
    offer = parse_connection(conn("x", "2026-10-01T08:00:00", "2026-10-01T09:00:00"))
    apply_offer(offer, {"price": 0, "availabilityState": "soldOut"})
    assert offer.price is None and offer.sold_out

    offer = parse_connection(conn("y", "2026-10-01T08:00:00", "2026-10-01T09:00:00"))
    apply_offer(offer, {"price": 12.5, "availabilityState": "available", "offerError": "x"})
    assert offer.price is None and not offer.sold_out


def test_current_offer_shape_without_availability_state():
    """Shop od podzimu 2026 posílá jen price/offerError/firstClass."""
    offer = parse_connection(conn("z", "2026-10-01T08:00:00", "2026-10-01T09:00:00"))
    apply_offer(offer, {"price": 38.3, "offerError": False, "firstClass": False,
                        "specialNote": {"de": "Sparschiene"}})
    assert offer.price == Decimal("38.3")
    assert offer.first_class is None          # False není cena 1. třídy

    offer = parse_connection(conn("w", "2026-10-01T08:00:00", "2026-10-01T09:00:00"))
    apply_offer(offer, {"price": 38.3, "offerError": True})
    assert offer.price is None


# ---- klient shopu -----------------------------------------------------------
@pytest.mark.asyncio
async def test_shop_flow_without_api_key():
    transport = FakeTransport()
    offers = await OebbShop("ÖBB", transport).search("Wien", "Salzburg", at(8), pages=2)

    assert [o.connection_id for o in offers] == ["c1", "c2", "c3"]
    assert offers[0].price == Decimal("19.9")
    assert offers[0].first_class == Decimal("39.9")
    assert offers[1].price is None and offers[1].sold_out
    # Token se získá jednou a pak se používá – žádný klíč.
    assert sum("/init" in c for c in transport.calls) == 1


@pytest.mark.asyncio
async def test_init_falls_back_to_newer_version():
    class NewInit(FakeTransport):
        async def get(self, url, params, headers):
            if "/domain/v4/init" in url:
                self.calls.append(url)
                raise TransientProviderError("ÖBB", "HTTP 404: not found")
            return await super().get(url, params, headers)

    transport = NewInit()
    await OebbShop("ÖBB", transport).search("Wien", "Salzburg", at(8), pages=1)
    assert any("/domain/v5/init" in c for c in transport.calls)


@pytest.mark.asyncio
async def test_init_error_is_not_masked_by_missing_versions():
    """Selže-li v4 jinak než 404, uživatel má vidět tu chybu, ne 404 z v5."""
    class Broken(FakeTransport):
        async def get(self, url, params, headers):
            if "/domain/v4/init" in url:
                raise TransientProviderError("ÖBB", "síť: ConnectTimeout()")
            if "/init" in url:
                raise TransientProviderError("ÖBB", "HTTP 404: ")
            return await super().get(url, params, headers)

    with pytest.raises(TransientProviderError) as info:
        await OebbShop("ÖBB", Broken()).search("Wien", "Salzburg", at(8), pages=1)
    assert "ConnectTimeout" in info.value.message


@pytest.mark.asyncio
async def test_timetable_request_carries_route_and_date():
    transport = FakeTransport()
    await OebbShop("ÖBB", transport).search("Wien", "Salzburg", at(8), pages=1)
    body = transport.timetable_body
    assert body["datetimeDeparture"] == "2026-10-01T06:00:00.000Z"
    assert body["from"]["number"] == 1290401 and body["to"]["number"] == 8100002


# ---- dopravce: spojení jízdního řádu a cen ---------------------------------
def hafas_journeys():
    return [
        make_journey(provider="ÖBB", origin="Wien Hbf", destination="Salzburg Hbf",
                     dep=at(8, 28), arr=at(10, 53), line="RJX 165"),
        make_journey(provider="ÖBB", origin="Wien Hbf", destination="Salzburg Hbf",
                     dep=at(8, 57), arr=at(11, 49), line="WB 911"),
    ]


class _Converter:
    def to_czk(self, amount, currency):
        return amount * 25 if currency == "EUR" else amount


def provider(**options):
    # Testy nesmí sahat na síť přes curl_cffi – přímý transport je httpx,
    # který si testy nahrazují falešným.
    options.setdefault("impersonate", False)
    return ObbProvider(cache=MemoryCache(), converter=_Converter(), options=options)


def test_merge_attaches_prices_and_adds_shop_only_connections():
    offers = asyncio.run(OebbShop("ÖBB", FakeTransport()).search("Wien", "Salzburg", at(8)))
    journeys = merge_offers(hafas_journeys(), offers, provider(), query())

    by_dep = {j.departure.strftime("%H:%M"): j for j in journeys}
    assert by_dep["08:28"].price == Decimal("19.9") * 25          # EUR → CZK
    assert by_dep["08:28"].legs[0].stops                          # mezizastávky z HAFASu zůstaly
    assert by_dep["08:57"].fare_status is FareStatus.SOLD_OUT
    assert by_dep["09:28"].price is not None                      # jen ze shopu


@pytest.mark.asyncio
async def test_prices_blocked_keeps_timetable_and_notes_it(monkeypatch):
    p = provider(browser_fallback=False)

    async def timetable(q):
        return hafas_journeys()

    monkeypatch.setattr(p, "_timetable", timetable)
    monkeypatch.setattr(shop_mod, "HttpTransport", lambda client: FakeTransport(fail_on="/init"))
    monkeypatch.setattr(obb_mod, "HttpTransport", lambda client: FakeTransport(fail_on="/init"))

    result = await p.run(query())
    assert result.ok                                   # výpadek cen není chyba dopravce
    assert len(result.journeys) == 2
    assert all(j.fare_status is FareStatus.UNKNOWN for j in result.journeys)
    assert result.notes and "jízdní řád" in result.notes[0]


@pytest.mark.asyncio
async def test_blocked_http_switches_to_browser(monkeypatch):
    p = provider()
    browser = FakeTransport()

    async def timetable(q):
        return hafas_journeys()

    monkeypatch.setattr(p, "_timetable", timetable)
    monkeypatch.setattr(obb_mod, "HttpTransport", lambda client: FakeTransport(fail_on="/init"))
    monkeypatch.setattr(obb_mod, "BrowserTransport", lambda *a, **k: browser)
    monkeypatch.setattr(obb_mod, "playwright_available", lambda: True)

    result = await p.run(query())
    assert browser.calls, "po blokaci se měl použít prohlížeč"
    assert any(j.price is not None for j in result.journeys)
    assert not result.notes


@pytest.mark.asyncio
async def test_slow_prices_do_not_cost_the_timetable(monkeypatch):
    p = provider()
    monkeypatch.setattr(obb_mod, "PRICE_BUDGET", 0.05)

    async def timetable(q):
        return hafas_journeys()

    async def slow(q):
        await asyncio.sleep(5)

    monkeypatch.setattr(p, "_timetable", timetable)
    monkeypatch.setattr(p, "_shop_offers", slow)

    result = await p.run(query())
    assert result.ok and len(result.journeys) == 2
    assert "včas" in result.notes[0]


@pytest.mark.asyncio
async def test_timetable_down_falls_back_to_shop(monkeypatch):
    p = provider()

    async def broken(q):
        raise TransientProviderError("ÖBB", "HAFAS nedostupný")

    monkeypatch.setattr(p, "_timetable", broken)
    monkeypatch.setattr(obb_mod, "HttpTransport", lambda client: FakeTransport())

    result = await p.run(query())
    assert result.ok
    assert len(result.journeys) == 3
    assert any(j.price is not None for j in result.journeys)
    assert "e-shopu" in result.notes[0]


@pytest.mark.asyncio
async def test_both_down_is_a_real_error(monkeypatch):
    p = provider(browser_fallback=False)

    async def broken(q):
        raise TransientProviderError("ÖBB", "HAFAS nedostupný")

    monkeypatch.setattr(p, "_timetable", broken)
    monkeypatch.setattr(obb_mod, "HttpTransport", lambda client: FakeTransport(fail_on="/init"))

    result = await p.run(query())
    assert not result.ok
