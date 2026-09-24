"""Ceny ÖBB z jejich e-shopu – bez API klíče.

HAFAS (``fahrplan.oebb.at``) vrací jízdní řád, ale tarify má ÖBB vypnuté:
``trfReq`` projde, jenže ``trfRes`` je prázdný. Ceny zná jen e-shop
``shop.oebbtickets.at`` a ten si je bere z vlastního JSON API.

To API nepotřebuje klíč. Každý návštěvník webu dostane **anonymní token**
a s ním se ptá stejně jako prohlížeč:

1. ``GET  /api/domain/v4/init`` → ``accessToken`` + ``sessionId``
2. ``GET  /api/hafas/v1/stations?name=…`` → stanice (``number``, ``name``)
3. ``POST /api/offer/v2/travelActions`` → ``travelActionId`` (vstup "timetable")
4. ``POST /api/hafas/v4/timetable`` → spoje s ``id``
   (další stránka: ``POST /api/hafas/v1/timetableScroll``)
5. ``GET  /api/offer/v1/prices?connectionIds[]=…`` → ``offers`` s cenou v EUR

Postup odpovídá open-source klientovi ``oebb`` (npm, juliuste/oebb).

**Blokace.** ÖBB před shop předřadilo ochranu, která některé adresy odmítne
stránkou *„Zugriff vorübergehend eingeschränkt“* – spolehlivě IP adresy
serveroven, domácí připojení obvykle projde. Proto jsou tu dvě cesty
(„transporty“) se stejnou logikou:

* ``HttpTransport`` – přímé dotazy, rychlé (~1 s),
* ``BrowserTransport`` – načte shop ve skutečném prohlížeči a tytéž dotazy
  pošle ``fetch()``-em zevnitř stránky. Ochrana pak vidí běžný prohlížeč
  s cookies, které si sám vyřídil. To je v jádru to, co dělal původní
  skript, jen bez klikání do formuláře a čtení HTML.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ..errors import BlockedError, ProviderError, StationNotFoundError, TransientProviderError
from ..http import looks_blocked

log = logging.getLogger(__name__)

SHOP_HOST = "https://shop.oebbtickets.at"
SHOP_PAGE = f"{SHOP_HOST}/de/ticket"
VIENNA = ZoneInfo("Europe/Vienna")

#: Verze ``init`` endpointu. ÖBB je čas od času zvedá; starší obvykle
#: chvíli dožívají. Zkoušíme od nejznámější.
INIT_VERSIONS = ("v4", "v5", "v6")
#: Anonymní token vydrží déle, ale nechceme riskovat 401 uprostřed hledání.
TOKEN_TTL = 15 * 60

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Channel": "inet",
    "Lang": "de",
}


# ---------------------------------------------------------------------------
#  Transporty
# ---------------------------------------------------------------------------
class Transport(Protocol):
    """Jak se dostat k API. Logika dotazů je pro oba transporty stejná."""

    async def get(self, url: str, params: list[tuple[str, str]], headers: dict[str, str]) -> Any: ...
    async def post(self, url: str, body: Any, headers: dict[str, str]) -> Any: ...
    async def close(self) -> None: ...


class HttpTransport:
    """Přímé dotazy přes sdílený ``HttpClient`` (retry, rate-limit, identity)."""

    def __init__(self, client) -> None:
        self.client = client

    async def get(self, url, params, headers):
        return await self.client.get_json(url, params=params, headers=headers, referer=SHOP_PAGE)

    async def post(self, url, body, headers):
        return await self.client.post_json(url, body, headers=headers, referer=SHOP_PAGE)

    async def close(self) -> None:
        """Klient patří dopravci, ten ho i zavře."""


_FETCH_JS = """
async ([url, method, headers, body]) => {
    const response = await fetch(url, {
        method,
        headers,
        body: body === null ? undefined : JSON.stringify(body),
        credentials: "include",
    });
    return {status: response.status, text: await response.text()};
}
"""


class BrowserTransport:
    """Dotazy ``fetch()``-em zevnitř načtené stránky shopu."""

    def __init__(self, provider: str, *, headless: bool = True, engine: str = "chromium") -> None:
        self.provider = provider
        self.headless = headless
        self.engine = engine
        self._session = None
        self._page = None
        # Travel hacky se ptají na víc úseků najednou – prohlížeč smí
        # vzniknout jen jednou.
        self._lock = asyncio.Lock()

    async def _ensure_page(self):
        async with self._lock:
            if self._page is None:
                self._page = await self._open_page()
            return self._page

    async def _open_page(self):
        from ..browser import BrowserSession

        self._session = BrowserSession(
            self.provider, headless=self.headless, engine=self.engine,
            locale="de-AT", timezone="Europe/Vienna",
        )
        page = await self._session.new_page()
        await self._session.goto(page, SHOP_PAGE)   # vyhodí BlockedError na ochranné stránce
        # Aplikace si při startu sama vyřídí ochranné cookies; dáme jí chvíli.
        await page.wait_for_timeout(2500)
        await self._session.assert_not_blocked(page)
        return page

    async def _fetch(self, method: str, url: str, headers: dict[str, str], body: Any) -> Any:
        page = await self._ensure_page()
        try:
            result = await page.evaluate(_FETCH_JS, [url, method, headers, body])
        except Exception as exc:
            raise TransientProviderError(self.provider, f"fetch v prohlížeči selhal: {exc}") from exc
        return _decode(self.provider, int(result["status"]), result["text"])

    async def get(self, url, params, headers):
        from urllib.parse import urlencode

        full = f"{url}?{urlencode(params)}" if params else url
        return await self._fetch("GET", full, headers, None)

    async def post(self, url, body, headers):
        return await self._fetch("POST", url, {**headers, "Content-Type": "application/json"}, body)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = self._page = None


def _decode(provider: str, status: int, text: str) -> Any:
    if looks_blocked(status, text[:4000]):
        raise BlockedError(provider, f"shop ÖBB blokuje (HTTP {status})")
    if status >= 500:
        raise TransientProviderError(provider, f"shop ÖBB vrátil {status}")
    if status >= 400:
        raise TransientProviderError(provider, f"HTTP {status}: {text[:200]}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise TransientProviderError(provider, "shop ÖBB nevrátil JSON") from exc


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ShopSection:
    origin: str
    destination: str
    departure: dt.datetime
    arrival: dt.datetime
    line: str
    is_train: bool


@dataclass(slots=True)
class ShopOffer:
    """Cena jednoho spoje z shopu. ``price`` je ``None``, když se nedá koupit."""

    connection_id: str
    departure: dt.datetime
    arrival: dt.datetime
    sections: list[ShopSection]
    price: Decimal | None = None
    first_class: Decimal | None = None
    sold_out: bool = False
    has_offer: bool = False


def parse_time(value: Any) -> dt.datetime | None:
    """Shop posílá místní vídeňský čas, někdy bez zóny, někdy se ``Z``."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VIENNA)
    return parsed.astimezone(VIENNA)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount >= 0 else None


def parse_connection(raw: dict[str, Any]) -> ShopOffer | None:
    departure = parse_time((raw.get("from") or {}).get("departure"))
    arrival = parse_time((raw.get("to") or {}).get("arrival"))
    if departure is None or arrival is None or not raw.get("id"):
        return None
    sections = []
    for sec in raw.get("sections") or []:
        s_dep = parse_time((sec.get("from") or {}).get("departure"))
        s_arr = parse_time((sec.get("to") or {}).get("arrival"))
        if s_dep is None or s_arr is None:
            continue
        category = sec.get("category") or {}
        name = category.get("shortName") or category.get("name") or ""
        line = " ".join(str(p) for p in (name, category.get("number")) if p).strip()
        sections.append(ShopSection(
            origin=str((sec.get("from") or {}).get("name") or ""),
            destination=str((sec.get("to") or {}).get("name") or ""),
            departure=s_dep, arrival=s_arr, line=line,
            is_train=bool(category.get("train", True)),
        ))
    return ShopOffer(connection_id=str(raw["id"]), departure=departure,
                     arrival=arrival, sections=sections)


def apply_offer(offer: ShopOffer, raw: dict[str, Any]) -> None:
    """Promítne položku z ``/prices`` do ``ShopOffer``.

    Cena platí jen s ``availabilityState == "available"`` a bez
    ``offerError`` – stejné pravidlo používá i web.
    """
    offer.has_offer = True
    state = str(raw.get("availabilityState") or "").lower()
    price = _decimal(raw.get("price"))
    if raw.get("offerError") or state != "available" or price is None:
        offer.sold_out = "sold" in state or "ausgebucht" in state
        return
    offer.price = price
    offer.first_class = _decimal(raw.get("firstClass"))


# ---------------------------------------------------------------------------
#  Klient
# ---------------------------------------------------------------------------
class OebbShop:
    """Ptá se shopu ÖBB na spoje a ceny. Transport lze vyměnit."""

    def __init__(self, provider: str, transport: Transport, *, host: str = SHOP_HOST) -> None:
        self.provider = provider
        self.transport = transport
        self.host = host
        self._auth: dict[str, str] | None = None
        self._auth_at = 0.0
        self._auth_lock = asyncio.Lock()

    async def _headers(self) -> dict[str, str]:
        async with self._auth_lock:
            if self._auth is None or time.monotonic() - self._auth_at > TOKEN_TTL:
                self._auth = await self._init()
                self._auth_at = time.monotonic()
            return {**BASE_HEADERS, **self._auth}

    async def _init(self) -> dict[str, str]:
        params = [
            ("userId", f"anonym-{uuid.uuid4()}"),
            ("cookieDate", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")),
            ("isFirstVisit", "true"),
        ]
        last: ProviderError | None = None
        for version in INIT_VERSIONS:
            try:
                data = await self.transport.get(f"{self.host}/api/domain/{version}/init",
                                                params, dict(BASE_HEADERS))
            except BlockedError:
                raise
            except TransientProviderError as exc:
                last = exc
                continue
            token = (data or {}).get("accessToken")
            if not token:
                last = TransientProviderError(self.provider, "shop nevydal anonymní token")
                continue
            headers = {"AccessToken": str(token)}
            if data.get("sessionId"):
                headers["SessionId"] = str(data["sessionId"])
            if data.get("supportId"):
                headers["x-ts-supportid"] = f"WEB_{data['supportId']}"
            return headers
        raise last or TransientProviderError(self.provider, "shop nevydal anonymní token")

    async def station(self, name: str) -> dict[str, Any]:
        data = await self.transport.get(f"{self.host}/api/hafas/v1/stations",
                                        [("name", name)], await self._headers())
        stations = [s for s in (data or []) if isinstance(s, dict) and s.get("number")]
        if not stations:
            raise StationNotFoundError(self.provider, f"shop ÖBB nezná stanici {name!r}")
        from ..normalize import best_match

        idx = best_match(name, [str(s.get("name") or s.get("meta") or "") for s in stations],
                         threshold=0.3)
        return stations[idx if idx is not None else 0]

    async def search(self, origin: str, destination: str, when: dt.datetime,
                     *, pages: int = 2) -> list[ShopOffer]:
        """Spoje od ``when`` i s cenami. ``pages`` × cca 5 spojů."""
        headers = await self._headers()
        start, end = await self.station(origin), await self.station(destination)
        when_utc = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        actions = await self.transport.post(
            f"{self.host}/api/offer/v2/travelActions",
            {"datetime": when_utc, "from": start, "to": end}, headers)
        action_id = next(
            (a.get("id") for a in (actions or {}).get("travelActions", [])
             if (a.get("entrypoint") or {}).get("id") == "timetable"),
            None,
        )
        if not action_id:
            raise TransientProviderError(self.provider, "shop ÖBB nenabídl jízdenku pro tuto trasu")

        base = {
            "travelActionId": action_id,
            "sortType": "DEPARTURE",
            "filter": {"bikes": False, "changeTime": 0, "direct": False,
                       "regionaltrains": False, "trains": False, "wheelchair": False},
        }
        offers: dict[str, ShopOffer] = {}
        last_id: str | None = None
        for page in range(max(1, pages)):
            if page == 0:
                body = {**base, "datetimeDeparture": when_utc, "datetimeArrival": "",
                        "passengers": [_adult()]}
                url = f"{self.host}/api/hafas/v4/timetable"
            else:
                body = {**base, "connectionId": last_id, "count": 5, "direction": "after"}
                url = f"{self.host}/api/hafas/v1/timetableScroll"
            data = await self.transport.post(url, body, headers)
            batch = [c for c in (parse_connection(r) for r in (data or {}).get("connections", [])) if c]
            fresh = [c for c in batch if c.connection_id not in offers]
            if not fresh:
                break
            for conn in fresh:
                offers[conn.connection_id] = conn
            await self._prices(fresh, headers)
            last_id = fresh[-1].connection_id
        return sorted(offers.values(), key=lambda o: o.departure)

    async def _prices(self, conns: list[ShopOffer], headers: dict[str, str]) -> None:
        params = [("connectionIds[]", c.connection_id) for c in conns]
        params += [("sortType", "DEPARTURE"), ("bestPriceId", "undefined")]
        data = await self.transport.get(f"{self.host}/api/offer/v1/prices", params, headers)
        by_id = {c.connection_id: c for c in conns}
        for raw in (data or {}).get("offers", []) or []:
            conn = by_id.get(str(raw.get("connectionId")))
            if conn is not None:
                apply_offer(conn, raw)

    async def close(self) -> None:
        await self.transport.close()


def _adult() -> dict[str, Any]:
    """Jeden dospělý bez slevové karty – tvar, který posílá web."""
    return {
        "type": "ADULT",
        "id": int(time.time()),
        "me": False,
        "remembered": False,
        "position": 0,
        "challengedFlags": {"hasAssistanceDog": False, "hasAttendant": False,
                            "hasHandicappedPass": False, "hasWheelchair": False, "id": 0},
    }
