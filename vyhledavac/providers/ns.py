"""NS – Nizozemské dráhy (plánovač na ns.nl).

Plánovač na www.ns.nl/reisplanner volá API ``gateway.apiportal.ns.nl``
s klíčem ``Ocp-Apim-Subscription-Key``, který stránka sama vypisuje
v ``window.journeyplannerConfiguration.nsAppInternalProductKey``. Klíč se
proto pokaždé (jednou za den) přečte ze stránky – když ho NS vymění, program
si vezme nový. Volání:

* ``GET nsapp-stations/v3?q=…`` – stanice (i zahraniční: „Berlin Hbf“,
  země v ``country``; kód stanice ``id.code`` – ASD, UT, BERHBL);
* ``GET reisinformatie-api/api/v3/trips?fromStation&toStation&dateTime`` –
  spoje s mezizastávkami a cenou ``productFare`` (jednotlivá jízdenka
  2. třídy bez slevy, OV-chipkaart / e-ticket).

Na co si dát pozor:

* **Cena je jen po Nizozemsku.** U mezinárodních spojů (Amsterdam → Berlín)
  NS vrátí jízdní řád bez ceny – jízdenku prodává NS International / DB.
* Cena v API je za jednoho dospělého. Pro skupinu ji sčítáme: od 12 let plné
  jízdné, děti 4–11 let s dospělým jezdí s jízdenkou Railrunner (2,50 €),
  do 4 let zdarma.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Nizozemsku.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

GATEWAY = "https://gateway.apiportal.ns.nl"
PLANNER = "https://www.ns.nl/reisplanner/"
KEY_TTL = 24 * 3600
PLACE_TTL = 30 * 24 * 3600
#: Klíč z konfigurace ns.nl (říjen 2026) – jen záloha, když se stránku nepodaří načíst.
FALLBACK_KEY = "3833ed4cbc5d43bd9241420caf04365c"
RAILRUNNER = Decimal("2.50")


@PROVIDERS.register()
class NsProvider(HttpProvider):
    name = "NS"
    label = "NS – Nizozemské dráhy (jen cesty z/do Nizozemska; ceny po Nizozemsku)"
    multi_passenger = True
    currency = "EUR"
    countries = ("NL", "DE", "BE")
    international_only = True
    referer = PLANNER
    min_interval = 0.3

    async def api_key(self) -> str:
        cached = self.cache.get(self.name, "key")
        if cached:
            return cached
        try:
            page = await self.client.get_text(PLANNER, referer="https://www.ns.nl/")
            key = parse_key(page) or FALLBACK_KEY
        except Exception as exc:  # noqa: BLE001 - záložní klíč
            log.debug("NS: konfiguraci plánovače nelze načíst: %s", exc)
            return FALLBACK_KEY
        self.cache.set(self.name, "key", key, ttl=KEY_TTL)
        return key

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await self.client.get_json(f"{GATEWAY}/{path}", params=params, referer=PLANNER,
                                          headers={"Ocp-Apim-Subscription-Key": await self.api_key(),
                                                   "Origin": "https://www.ns.nl"})

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self._get("nsapp-stations/v3", {"q": place, "limit": 10})
        items = [s for s in (data or {}).get("payload") or [] if (s.get("id") or {}).get("code")]
        if not items:
            raise StationNotFoundError(self.name, f"stanici {place!r} NS nezná")
        names = [str((s.get("names") or {}).get("long") or "") for s in items]
        wanted = normalize(place)
        exact = [s for s, n in zip(items, names) if normalize(n) == wanted
                 or wanted in (normalize(x) for x in (s.get("names") or {}).get("synonyms") or [])]
        if exact:
            chosen = exact[0]
        else:
            idx = best_match(place, names)
            chosen = items[idx if idx is not None else 0]
        out = {"code": chosen["id"]["code"], "name": (chosen.get("names") or {}).get("long") or place,
               "country": chosen.get("country") or ""}
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "NL" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Nizozemsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if "NL" not in (origin["country"], destination["country"]):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Nizozemsku")
        when = query.departure.astimezone(DEFAULT_TZ).isoformat(timespec="seconds")
        data = await self._get("reisinformatie-api/api/v3/trips", {
            "fromStation": origin["code"], "toStation": destination["code"], "dateTime": when})
        journeys = []
        for trip in (data or {}).get("trips") or []:
            journey = to_journey(trip, query)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                if journey.raw.get("eur"):
                    journey.fares = [self.make_fare(Decimal(journey.raw["eur"]), "EUR", label="NS",
                                                    tariff=journey.raw["tariff"])]
                    journey.fare_status = FareStatus.PRICED
                journeys.append(journey)
        return journeys


def parse_key(page: str) -> str | None:
    m = re.search(r'nsAppInternalProductKey:\s*"([0-9a-f]{32})"', page)
    return m.group(1) if m else None


def group_price(adult: Decimal, query: SearchQuery) -> tuple[Decimal, str]:
    total, parts = Decimal(0), []
    for p in query.passengers:
        if p.age >= 12:
            total += adult
        elif p.age >= 4:
            total += RAILRUNNER
            parts.append("Railrunner")
    return total, ("jednotlivá jízdenka 2. třídy" + (" + Railrunner" if parts else ""))


def to_journey(trip: dict[str, Any], query: SearchQuery) -> Journey | None:
    legs = []
    for raw in trip.get("legs") or []:
        origin, destination = raw.get("origin") or {}, raw.get("destination") or {}
        dep = _time(origin.get("plannedDateTime"))
        arr = _time(destination.get("plannedDateTime"))
        if dep is None or arr is None:
            continue
        product = raw.get("product") or {}
        walk = raw.get("travelType") == "WALK" or product.get("type") == "WALK"
        stops = []
        for s in raw.get("stops") or []:
            if s.get("passing"):
                continue
            stops.append(Stop(name=str(s.get("name") or ""),
                              arrival=_time(s.get("plannedArrivalDateTime")),
                              departure=_time(s.get("plannedDepartureDateTime"))))
        stops = [s for s in stops if s.name] or [Stop(name=str(origin.get("name") or ""), departure=dep),
                                                 Stop(name=str(destination.get("name") or ""), arrival=arr)]
        kind = str(product.get("type") or "TRAIN").upper()
        mode = Mode.WALK if walk else Mode.BUS if kind == "BUS" else Mode.TRAM if kind in ("TRAM", "METRO") \
            else Mode.TRAIN
        legs.append(Leg(origin=str(origin.get("name") or ""), destination=str(destination.get("name") or ""),
                        departure=dep, arrival=arr, mode=mode,
                        carrier=str(product.get("operatorName") or "NS"),
                        line=str(raw.get("name") or product.get("displayName") or ""), stops=stops))
    if not legs:
        return None
    share = trip.get("shareUrl")
    link = share.get("uri") if isinstance(share, dict) else None
    journey = Journey(provider="NS", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=link or PLANNER)
    fare = trip.get("productFare") or {}
    cents = fare.get("buyableTicketPriceInCents") or fare.get("priceInCents")
    if cents:
        total, tariff = group_price(Decimal(int(cents)) / 100, query)
        journey.raw = {"eur": str(total), "tariff": tariff}
    else:
        journey.notes.append("NS prodává jízdenky jen po Nizozemsku – mezinárodní jízdenku kupte "
                             "u NS International nebo DB.")
    return journey


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
