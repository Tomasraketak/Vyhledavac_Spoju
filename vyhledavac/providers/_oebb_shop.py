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

* ``CurlTransport`` – přímé dotazy přes ``curl_cffi`` s TLS otiskem Chromu.
  Cloudflare před shopem odmítá TLS otisk Pythonu (httpx dostane 403
  bez ohledu na hlavičky), otisk skutečného prohlížeče pustí. Rychlé (~1 s).
* ``HttpTransport`` – přímé dotazy přes httpx; záloha, když ``curl_cffi``
  chybí (obvykle ho Cloudflare odmítne),
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

from ..errors import BlockedError, ProviderError, StationNotFoundError, TransientProviderError
from ..http import looks_blocked
from ..models import DEFAULT_TZ

log = logging.getLogger(__name__)

SHOP_HOST = "https://shop.oebbtickets.at"
SHOP_PAGE = f"{SHOP_HOST}/de/ticket"
# Vídeň má stejná pravidla jako Praha. Vlastní ZoneInfo("Europe/Vienna")
# na Windows bez balíčku tzdata shodilo import a ÖBB zmizelo z nabídky.
VIENNA = DEFAULT_TZ

#: Verze ``init`` endpointu. ÖBB je čas od času zvedá; starší obvykle
#: chvíli dožívají. Zkoušíme od nejznámější. Neexistující verze vrací 404.
INIT_VERSIONS = ("v4", "v5")
#: Anonymní token platí 5 minut (``exp - iat`` = 300 s). Obnovujeme ho
#: s rezervou, ať nevyprší uprostřed hledání.
TOKEN_TTL = 4 * 60

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


class CurlTransport:
    """Přímé dotazy s TLS otiskem skutečného Chromu (``curl_cffi``)."""

    IMPERSONATE = "chrome"

    def __init__(self, provider: str, *, timeout: float = 20.0, attempts: int = 2) -> None:
        self.provider = provider
        self.timeout = timeout
        self.attempts = attempts
        self._session = None

    @staticmethod
    def available() -> bool:
        try:
            import curl_cffi.requests  # noqa: F401
            return True
        except Exception:
            return False

    def _ensure(self):
        if self._session is None:
            from curl_cffi.requests import AsyncSession

            self._session = AsyncSession(impersonate=self.IMPERSONATE, timeout=self.timeout)
        return self._session

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        session = self._ensure()
        last: ProviderError | None = None
        for attempt in range(self.attempts):
            if attempt:
                await asyncio.sleep(0.8)
            try:
                response = await session.request(method, url, referer=SHOP_PAGE, **kwargs)
            except Exception as exc:  # curl_cffi má vlastní hierarchii výjimek
                last = TransientProviderError(self.provider, f"síť: {exc!r}"[:200])
                continue
            if response.status_code >= 500:
                last = TransientProviderError(self.provider, f"shop ÖBB vrátil {response.status_code}")
                continue
            return _decode(self.provider, response.status_code, response.text)
        raise last or TransientProviderError(self.provider, "vyčerpány pokusy")

    async def get(self, url, params, headers):
        return await self._request("GET", url, params=params, headers=headers)

    async def post(self, url, body, headers):
        return await self._request("POST", url, json=body, headers=headers)

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None


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
    walk: bool = False


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
    section_count: int = 0
    #: Cena platí jen pro část trasy (``reducedScope``): (odkud, kam) podle shopu.
    scope: tuple[str, str] | None = None


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
            # pěší přesun mezi nástupišti/nádražími: kategorie „W“ (Fußweg)
            walk=str(name).strip().upper() == "W" or "fußweg" in str(category.get("name") or "").lower()
            or str(sec.get("type") or "").lower() in ("walk", "footpath", "transfer"),
        ))
    return ShopOffer(connection_id=str(raw["id"]), departure=departure,
                     arrival=arrival, sections=sections,
                     section_count=len(raw.get("sections") or []))


def apply_offer(offer: ShopOffer, raw: dict[str, Any]) -> None:
    """Promítne položku z ``/prices`` do ``ShopOffer``.

    Cena platí bez ``offerError``. Starší verze shopu posílala navíc
    ``availabilityState`` a cena platila jen s ``"available"``; současná
    (říjen 2026) to pole vůbec nemá – posílá jen ``price`` a ``offerError``.
    Dřív se proto zahodila každá cena.
    """
    offer.has_offer = True
    state = str(raw.get("availabilityState") or "").lower()
    price = _decimal(raw.get("price"))
    if raw.get("offerError") or (state and state != "available") or price is None:
        offer.sold_out = "sold" in state or "ausgebucht" in state
        return
    offer.price = price
    offer.first_class = _decimal(raw.get("firstClass"))
    # „Ticket nur für Teilstrecke“: cena jen za úseky sectionIdx od–do
    for part in raw.get("reducedScope") or []:
        start, end = part.get("from") or {}, part.get("to") or {}
        whole = (start.get("sectionIdx", 0) == 0
                 and end.get("sectionIdx", -1) >= max(offer.section_count - 1, 0))
        if not whole:
            offer.scope = (str(start.get("name") or ""), str(end.get("name") or ""))
            break


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
        # 404 znamená jen "tahle verze už/ještě neexistuje". Jinou chybu si
        # pamatujeme a nahlásíme ji – dřív ji přepsala 404 z poslední
        # zkoušené verze a uživatel viděl nic neříkající "HTTP 404: ".
        real: ProviderError | None = None
        last: ProviderError | None = None
        for version in INIT_VERSIONS:
            try:
                data = await self.transport.get(f"{self.host}/api/domain/{version}/init",
                                                params, dict(BASE_HEADERS))
            except BlockedError:
                raise
            except TransientProviderError as exc:
                last = exc
                if "HTTP 404" not in exc.message and real is None:
                    real = exc
                continue
            token = (data or {}).get("accessToken")
            if not token:
                real = real or TransientProviderError(self.provider, "shop nevydal anonymní token")
                continue
            headers = {"AccessToken": str(token)}
            if data.get("sessionId"):
                headers["SessionId"] = str(data["sessionId"])
            if data.get("supportId"):
                headers["x-ts-supportid"] = f"WEB_{data['supportId']}"
            return headers
        raise real or last or TransientProviderError(self.provider, "shop nevydal anonymní token")

    async def station(self, name: str) -> dict[str, Any]:
        data = await self.transport.get(f"{self.host}/api/hafas/v1/stations",
                                        [("name", name)], await self._headers())
        stations = [s for s in (data or []) if isinstance(s, dict) and s.get("number")]
        if not stations:
            raise StationNotFoundError(self.provider, f"shop ÖBB nezná stanici {name!r}")
        from .. import stations as places
        from ..normalize import best_match

        stations = [s for s in stations if not places.far_from(name, *_shop_coords(s))]
        idx = best_match(name, [str(s.get("name") or s.get("meta") or "") for s in stations],
                         threshold=0.3)
        if idx is None:
            # žádné podobné jméno („Vídeň“ × „Wien Hbf“): první návrh, jen když leží
            # ve stejné zemi – „Brašov“ shop nezná a nabídne „Braunau/Inn“
            from .. import geo

            first = str(stations[0].get("name") or stations[0].get("meta") or "") if stations else ""
            asked, got = geo.country_of(name), geo.country_of(first)
            if not stations or (asked and got and asked != got):
                raise StationNotFoundError(self.provider, f"shop ÖBB nezná stanici {name!r}")
            idx = 0
        return stations[idx]

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
                # Žádné "datetimeArrival": "" – prázdný řetězec shop bere jako
                # zadaný příjezd, datum odjezdu ignoruje a vrací spoje od "teď".
                body = {**base, "datetimeDeparture": when_utc, "passengers": [_adult()],
                        "from": start, "to": end, "count": 5,
                        "entryPointId": "timetable"}
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


def _shop_coords(station: dict[str, Any]) -> tuple[float | None, float | None]:
    """Souřadnice stanice ze shopu, pokud je posílá (``latitude``/``longitude``)."""
    try:
        lat, lon = station.get("latitude"), station.get("longitude")
        if lat is None or lon is None:
            return None, None
        lat, lon = float(lat), float(lon)
        return (lat / 1e6, lon / 1e6) if abs(lat) > 1000 else (lat, lon)
    except (TypeError, ValueError):
        return None, None


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
