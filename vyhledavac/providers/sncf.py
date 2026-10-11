"""SNCF – francouzské vlaky (TGV INOUI, OUIGO, TER, Intercités, Lyria) přes srovnávač Virail.

Přímo od SNCF to nejde: sncf-connect.com i Trainline (thetrainline.com)
chrání hledání spojů DataDome – z datacentra vrací captchu „t=bv“
(zablokovaný návštěvník) i skutečnému prohlížeči. Srovnávač **virail.com**
ale vlaky SNCF ukazuje i s cenami (bere je od agregátorů Kombo a Omio)
a jeho API žádnou captchu nemá:

* ``GET /api/legacy-autocomplete/v2/search?q=…&locale_code=en_us`` – města
  (``k`` = „a.<geonames id>“, ``c`` = země, ``stations`` = nádraží města);
* ``GET /api/v1/search/transport?departure&arrival&date&adult_pax&youth_pax
  &senior_pax&transports=train&currency=EUR`` – hledání běží na jejich
  serveru; stejný dotaz se opakuje (Virail si ho pamatuje podle parametrů),
  dokud ``status`` není ``success``. Mezitím ``result`` přibývá.

Každý výsledek má ``segments`` (vlak, nádraží, čas s časovou zónou, cena)
a ``metadata.sources`` (od koho je cena, ``primary`` = ta zobrazená).

Na co si dát pozor:

* **Rate limit.** Virail dovolí jen pár hledání za několik minut – při
  častém dotazování vrací ``{"status": "error", "code": 429}``. Proto se ptáme
  jen tehdy, když aspoň jeden konec cesty leží ve Francii, dotazujeme se
  nejvýš jednou za 6 s a výsledek se na 20 minut ukládá do cache.
* **Cena je od agregátoru**, ne od SNCF: Kombo / Omio ji zjistily nedávno
  (``scraped``), ale může být mírně neaktuální. Spoj proto nese poznámku
  a odkaz vede na nákup u agregátoru.
* **Skupina neověřena.** Cestující se posílají (``adult_pax``, věky mládeže
  0–25 v ``youth_pax`` jako „8.0,12.0“, seniorů 60+ v ``senior_pax``), ale
  kvůli rate limitu se nepodařilo ověřit, že vrácená cena je za celou
  skupinu. U více cestujících se proto cena ukáže jen v poznámce
  (``raw["indicative_eur"]``), ne jako cena spoje.
* Virail nevrací čísla vlaků ani mezizastávky – úsek má jen typ vlaku
  („inOui“, „TER“, „Ouigo“) a první a poslední zastávku.
* Hledá se mezi **městy** (Paříž = všechna nádraží), konkrétní nádraží
  v dotazu se nepoužije.
* Vrací i vlaky jiných dopravců po Francii (Frecciarossa Paříž – Lyon,
  Eurostar) – necháváme je, i to je cena, kterou jinde nenajdeme.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import RateLimitedError, StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

BASE = "https://www.virail.com"
PLACE_TTL = 30 * 24 * 3600
RESULT_TTL = 20 * 60
POLL_EVERY = 6.0
#: Jak dlouho nejvýš čekat na dokončení hledání na straně Virailu (s).
POLL_LIMIT = 60.0
YOUTH_MAX, SENIOR_MIN = 25, 60
#: Úseky, které nejsou vlak (autobus TER se počítá jako autobus).
BUS_WORDS = ("autocar", "bus", "car ")


@PROVIDERS.register()
class SncfProvider(HttpProvider):
    name = "SNCF"
    label = "SNCF – vlaky ve Francii (TGV INOUI, OUIGO, TER) přes srovnávač Virail"
    multi_passenger = True
    currency = "EUR"
    countries = ("FR", "BE", "CH", "LU", "DE", "IT", "ES")
    international_only = True
    hack_legs = False          # pár hledání za pár minut – na dílčí úseky hacků ne
    referer = BASE + "/"
    min_interval = POLL_EVERY
    min_timeout = POLL_LIMIT + 30

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self.client.get_json(f"{BASE}/api/legacy-autocomplete/v2/search",
                                          params={"q": place, "locale_code": "en_us", "limit": 12},
                                          referer=self.referer)
        items = [r for r in (data or {}).get("results") or [] if r.get("k")]
        if not items:
            raise StationNotFoundError(self.name, f"místo {place!r} Virail nezná")
        chosen = pick_city(place, items)
        out = {"id": chosen["k"], "name": chosen.get("n") or place, "country": chosen.get("c") or ""}
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "FR" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Francii")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if "FR" not in (origin["country"], destination["country"]):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Francii")
        params = {"departure": origin["id"], "arrival": destination["id"],
                  "date": query.departure.astimezone(DEFAULT_TZ).date().isoformat(),
                  **pax_params(query.passengers), "tld": "com", "locale": "en_us",
                  "currency": "EUR", "transports": "train"}
        key = "search:" + "|".join(f"{k}={v}" for k, v in sorted(params.items()))
        data = self.cache.get(self.name, key)
        if not data:
            data = await self._poll(params, query)
            if data.get("status") == "success":
                self.cache.set(self.name, key, data, ttl=RESULT_TTL)
        return self.journeys(data, query)

    async def _poll(self, params: dict[str, str], query: SearchQuery) -> dict[str, Any]:
        started = time.monotonic()
        data: dict[str, Any] = {}
        while True:
            try:
                fresh = await self.client.get_json(f"{BASE}/api/v1/search/transport", params=params,
                                                   referer=self.referer) or {}
            except RateLimitedError:
                fresh = {"status": "error", "code": 429}
            if fresh.get("code") == 429 and data.get("result"):
                self.note("Virail přestal odpovídat (rate limit) – výsledky můžou být neúplné.")
                return data
            data = fresh
            status = data.get("status")
            if status == "error":
                if data.get("code") == 429 or data.get("message") == "rate_limited":
                    raise RateLimitedError(self.name, "Virail omezuje počet hledání z této IP adresy – "
                                                      "zkuste to za chvíli", None)
                raise TransientProviderError(self.name, f"Virail: {data.get('message') or 'chyba hledání'}")
            if status != "processing" or time.monotonic() - started > POLL_LIMIT:
                if status == "processing":
                    self.note("Virail nedokončil hledání včas – výsledky můžou být neúplné.")
                return data
            self.publish(self.journeys(data, query))
            await asyncio.sleep(POLL_EVERY)

    def journeys(self, data: dict[str, Any], query: SearchQuery) -> list[Journey]:
        out = []
        for item in data.get("result") or []:
            journey = to_journey(item)
            if journey is None or not query.departure <= journey.departure <= query.window_end:
                continue
            price = item_price(item)
            source = primary_source(item)
            if price is not None and len(query.passengers) > 1:
                # u skupiny nevíme jistě, zda agregátor počítal se všemi cestujícími
                journey.raw = {"indicative_eur": str(price)}
                journey.notes.append(f"Virail ({source}) uvádí {price:.2f} € – u skupiny není jisté, že jde "
                                     "o cenu za všechny cestující; ověřte u prodejce.")
            elif price is not None:
                journey.fares = [self.make_fare(price, str(item_currency(item)), label=f"Virail ({source})",
                                                tariff=", ".join(dict.fromkeys(leg.line for leg in journey.legs)))]
                journey.fare_status = FareStatus.PRICED
                journey.notes.append(f"Cenu zjistil agregátor {source} přes srovnávač Virail – před nákupem "
                                     "ověřte u SNCF nebo u prodejce.")
            out.append(journey)
        return out


def pax_params(passengers: tuple[Passenger, ...]) -> dict[str, str]:
    """Cestující pro Virail: dospělí počtem, mládež (do 25) a senioři (60+) věky."""
    adults = sum(1 for p in passengers if YOUTH_MAX < p.age < SENIOR_MIN)
    youth = [p.age for p in passengers if p.age <= YOUTH_MAX]
    seniors = [p.age for p in passengers if p.age >= SENIOR_MIN]
    if adults == 0 and not seniors and youth:        # Virail chce aspoň jednoho dospělého
        youth.sort()
        youth.pop()
        adults = 1
    return {"adult_pax": str(max(adults, 0)),
            "youth_pax": ",".join(f"{a}.0" for a in youth) or "_",
            "senior_pax": ",".join(f"{a}.0" for a in seniors) or "_"}


def pick_city(place: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Město z našeptávače: přesná shoda jména, pak shoda prvního slova, pak nejpodobnější."""
    wanted = normalize(place)
    first = normalize(place.replace(",", " ").replace("-", " ").split()[0]) if place.strip() else wanted
    with_stations = [i for i in items if i.get("stations")] or items
    for target in (wanted, first):
        for item in with_stations:
            if normalize(str(item.get("n") or "")) == target:
                return item
    idx = best_match(place, [str(i.get("n") or "") for i in with_stations])
    return with_stations[idx if idx is not None else 0]


def to_journey(item: dict[str, Any]) -> Journey | None:
    legs = []
    for seg in item.get("segments") or []:
        dep, arr = _time(seg.get("departure_datetime")), _time(seg.get("arrival_datetime"))
        if dep is None or arr is None:
            return None
        carrier = str(seg.get("carrier") or "").strip()
        transport = str(seg.get("transport") or "")
        bus = transport == "bus" or any(w in carrier.lower() + " " for w in BUS_WORDS)
        origin, destination = _station(seg.get("departure_name")), _station(seg.get("arrival_name"))
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN, carrier=carrier or "SNCF",
                        line=carrier or ("bus" if bus else "vlak"),
                        stops=[Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    if not legs or not any(leg.mode is Mode.TRAIN for leg in legs):
        return None
    link = next((((s.get("deeplink") or {}).get("url")) for s in item.get("segments") or []
                 if (s.get("deeplink") or {}).get("url")), None)
    return Journey(provider="SNCF", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=link or BASE)


def primary_source(item: dict[str, Any]) -> str:
    sources = (item.get("metadata") or {}).get("sources") or []
    best = next((s for s in sources if s.get("primary")), sources[0] if sources else {})
    return str(best.get("datasource") or item.get("datasource") or "Virail")


def item_price(item: dict[str, Any]) -> Decimal | None:
    sources = (item.get("metadata") or {}).get("sources") or []
    best = next((s for s in sources if s.get("primary")), None)
    if best is not None and best.get("price"):
        return Decimal(str(best["price"]))
    prices = [s.get("price") for s in item.get("segments") or []]
    if not prices or prices[0] in (None, 0):
        return None
    return sum((Decimal(str(p)) for p in prices if p), Decimal(0))


def item_currency(item: dict[str, Any]) -> str:
    for seg in item.get("segments") or []:
        if seg.get("currency"):
            return str(seg["currency"])
    return "EUR"


def _station(name: Any) -> str:
    """„Lyon, Lyon-Part-Dieu“ → „Lyon-Part-Dieu“ (Kombo píše před nádraží město)."""
    text = str(name or "").strip()
    city, _, station = text.partition(", ")
    return station if station and normalize(station).startswith(normalize(city).split(" ")[0]) else text


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
