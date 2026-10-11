"""DB – Deutsche Bahn (int.bahn.de, e-shop pro celou Evropu).

Web int.bahn.de volá JSON API ``/web/api`` (stejné jako knihovna
db-vendo-client, profil „dbweb“):

* ``GET /web/api/reiseloesung/orte?suchbegriff=…&typ=ALL&limit=10`` –
  stanice; ``id`` je řetězec HAFAS („A=1@O=Praha hl.n.@…@L=5400014@“),
  který se posílá dál beze změny;
* ``POST /web/api/angebote/fahrplan`` – spoje se zastávkami a nejlevnější
  cenou (``angebotsPreis``) pro skupinu cestujících (``reisende``: typ
  ERWACHSENER, JUGENDLICHER, KIND, KLEINKIND, SENIOR + věk).

Na co si dát pozor:

* **Ochrana Akamai.** API odmítá dotazy z IP adres datových center
  („OPS_BLOCKED“, HTTP 403) a dotazy bez platné cookie ``_abck``, kterou
  nastaví JavaScript webu. Proto: nejdřív obyčejný HTTP dotaz (z domácího
  připojení obvykle projde), při 403 – je-li nainstalovaný Playwright –
  totéž ``fetch()`` uvnitř stránky int.bahn.de v prohlížeči. Při vývoji
  (z datacentra) prošlo vyhledání stanic, dotaz na spoje ne – parsování
  odpovědi je podle formátu db-vendo-client, živě neověřené.
* Ceny jsou „Flexpreis/Sparpreis“ bez BahnCard; u mezinárodních spojů DB
  prodá jen trasy, kde má tarif (obvykle přes Německo).

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Německu nebo
v zemi, kam se z Česka jezdí přes Německo (NL, BE, FR, CH, DK, LU).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import BlockedError, StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

BASE = "https://int.bahn.de"
API = f"{BASE}/web/api"
START = f"{BASE}/de/buchung/start"
PLACE_TTL = 30 * 24 * 3600
#: Země, kde má smysl ptát se DB (cíl nebo začátek cesty).
COUNTRIES = {"DE", "NL", "BE", "FR", "CH", "DK", "LU"}
PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"]


@PROVIDERS.register()
class DbProvider(HttpProvider):
    name = "DB"
    label = "DB – Deutsche Bahn (cesty přes Německo; Akamai může blokovat)"
    multi_passenger = True
    currency = "EUR"
    countries = ("DE", "NL", "BE", "FR", "CH", "DK", "LU", "AT", "CZ", "PL")
    international_only = True
    hack_legs = False        # pomalý a blokovaný – na dílčí úseky hacků ne
    referer = START
    min_interval = 0.6

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._browser = None
        self._page = None

    # ---- přenos: HTTP, při blokaci fetch v prohlížeči -----------------------------
    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
                    params: dict[str, Any] | None = None) -> Any:
        url = f"{API}/{path}"
        headers = {"Accept": "application/json", "Origin": BASE}
        if self._page is None:
            try:
                if method == "GET":
                    return await self.client.get_json(url, params=params, referer=START, headers=headers)
                return await self.client.post_json(url, body, referer=START, headers=headers)
            except BlockedError as exc:              # 403 Akamai „OPS_BLOCKED“
                log.info("DB: API blokuje přímé dotazy (%s), zkusím prohlížeč", exc)
        return await self._browser_call(method, path, body, params)

    async def _browser_call(self, method: str, path: str, body: dict[str, Any] | None,
                            params: dict[str, Any] | None) -> Any:
        from ..browser import BrowserSession, playwright_available

        if not playwright_available():
            raise BlockedError(self.name, "int.bahn.de blokuje dotazy (Akamai) a prohlížeč Playwright "
                                          "není nainstalovaný")
        if self._page is None:
            self._browser = BrowserSession(self.name, locale="de-DE", timezone="Europe/Berlin")
            self._page = await self._browser.new_page()
            await self._browser.goto(self._page, START)
            await self._page.wait_for_timeout(3000)
        from urllib.parse import urlencode

        url = f"/web/api/{path}" + (f"?{urlencode(params)}" if params else "")
        status, text = await self._page.evaluate(
            """async ([url, method, body]) => {
                const r = await fetch(url, {method, headers: {'Accept': 'application/json',
                    'Content-Type': 'application/json; charset=utf-8'}, body: body});
                return [r.status, await r.text()];
            }""", [url, method, json.dumps(body) if body is not None else None])
        if status == 403:
            raise BlockedError(self.name, "int.bahn.de odmítá dotaz (Akamai „OPS_BLOCKED“) – z této IP "
                                          "adresy DB hledat nejde")
        if status >= 400:
            raise TransientProviderError(self.name, f"HTTP {status}: {text[:120]}")
        return json.loads(text)

    async def aclose(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = self._page = None
        await super().aclose()

    # ---- hledání -----------------------------------------------------------------
    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self._call("GET", "reiseloesung/orte", params={"suchbegriff": place, "typ": "ALL",
                                                                     "limit": 10})
        items = [p for p in data or [] if p.get("id") and p.get("type") == "ST"] or \
                [p for p in data or [] if p.get("id")]
        if not items:
            raise StationNotFoundError(self.name, f"stanici {place!r} DB nezná")
        chosen = pick_place(place, items)
        self.cache.set(self.name, key, chosen, ttl=PLACE_TTL)
        return chosen

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and not COUNTRIES & set(known):
            raise StationNotFoundError(self.name, "cesta nevede do Německa ani přes něj")
        origin = await self.resolve(query.origin)
        destination = await self.resolve(query.destination)
        when = query.departure.astimezone(DEFAULT_TZ).strftime("%Y-%m-%dT%H:%M:%S")
        data = await self._call("POST", "angebote/fahrplan", body={
            "abfahrtsHalt": origin["id"], "ankunftsHalt": destination["id"],
            "anfrageZeitpunkt": when, "ankunftSuche": "ABFAHRT", "klasse": "KLASSE_2",
            "produktgattungen": PRODUCTS, "reisende": travellers(query.passengers),
            "schnelleVerbindungen": True, "sitzplatzOnly": False, "bikeCarriage": False,
            "reservierungsKontingenteVorhanden": False, "nurDeutschlandTicketVerbindungen": False,
            "deutschlandTicketVorhanden": False})
        journeys = []
        for raw in (data or {}).get("verbindungen") or []:
            journey = self.to_journey(raw)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def to_journey(self, raw: dict[str, Any]) -> Journey | None:
        legs = parse_legs(raw)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=f"{BASE}/de/buchung/fahrplan/suche")
        price = raw.get("angebotsPreis") or raw.get("abPreis") or {}
        if price.get("betrag") is not None:
            journey.fares = [self.make_fare(Decimal(str(price["betrag"])), str(price.get("waehrung") or "EUR"),
                                            label="DB", tariff=str(raw.get("angebotsPreisKlasse") or ""))]
            journey.fare_status = FareStatus.PRICED
        return journey


def travellers(passengers: tuple[Passenger, ...]) -> list[dict[str, Any]]:
    """Cestující pro DB: typ podle věku a věk (děti ho DB vyžadují)."""
    out = []
    for p in passengers:
        if p.age < 6:
            kind = "KLEINKIND"
        elif p.age < 15:
            kind = "KIND"
        elif p.age < 27:
            kind = "JUGENDLICHER"
        elif p.age >= 65:
            kind = "SENIOR"
        else:
            kind = "ERWACHSENER"
        out.append({"typ": kind, "alter": [str(p.age)], "anzahl": 1,
                    "ermaessigungen": [{"art": "KEINE_ERMAESSIGUNG", "klasse": "KLASSENLOS"}]})
    return out


def pick_place(place: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = normalize(place)
    for item in items:
        if normalize(str(item.get("name") or "")) == wanted:
            return item
    idx = best_match(place, [str(i.get("name") or "") for i in items])
    return items[idx if idx is not None else 0]


def parse_legs(raw: dict[str, Any]) -> list[Leg]:
    legs = []
    for section in raw.get("verbindungsAbschnitte") or []:
        dep, arr = _time(section.get("abfahrtsZeitpunkt")), _time(section.get("ankunftsZeitpunkt"))
        if dep is None or arr is None:
            continue
        vehicle = section.get("verkehrsmittel") or {}
        kind = str(vehicle.get("typ") or "").upper()
        product = str(vehicle.get("produktGattung") or "").upper()
        walk = kind in ("WALK", "FUSSWEG", "TRANSFER") or (not vehicle.get("name") and not product)
        mode = Mode.WALK if walk else Mode.BUS if product == "BUS" else \
            Mode.TRAM if product in ("TRAM", "UBAHN") else Mode.TRAIN
        stops = [Stop(name=str(h.get("name") or ""), arrival=_time(h.get("ankunftsZeitpunkt")),
                      departure=_time(h.get("abfahrtsZeitpunkt")))
                 for h in section.get("halte") or []]
        stops = [s for s in stops if s.name]
        origin = str(section.get("abfahrtsOrt") or (stops[0].name if stops else ""))
        destination = str(section.get("ankunftsOrt") or (stops[-1].name if stops else ""))
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr, mode=mode,
                        carrier="DB", line=str(vehicle.get("name") or vehicle.get("mittelText") or ""),
                        stops=stops or [Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    return legs


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # DB píše místní čas bez zóny (střední Evropa)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
