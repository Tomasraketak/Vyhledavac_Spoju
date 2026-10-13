"""Vy – Norské dráhy (dříve NSB; e-shop www.vy.no).

Webová aplikace vy.no (React Router + Hono) volá vlastní API na stejné
doméně. Stačí obyčejné JSON požadavky, jaké posílá stránka – bez přihlášení
a bez klíče:

* ``GET /api/location/places/v2/autosuggest?query=…&prioritizePlacesEngine=ENTUR
  &searchType=GLOBAL`` – našeptávač míst nad registrem zastávek Entur (NSR):
  ``id`` (``NSR:StopPlace:59872`` = Oslo S, ``NSR:GroupOfStopPlaces:7`` =
  Bergen jako město), ``shortDescription`` („Public transport hub in Bergen,
  Vestland“; u Švédska „… län“, u Dánska „Danmark“), ``position``,
  ``externalReferences`` a ``categories``;
* ``POST /api/global-itinerary/travel-planner/search`` – spoje. Tělo:
  ``{"from": <celý objekt z našeptávače>, "to": …, "datetime": "…Z",
  "filter": {"includeModes": ["TRAIN", "BUS"]}, "searchContext":
  "FIND_JOURNEY_INITIAL", "passengers": [{"id": 1, "age": 35, "categories": []}],
  "requestRegion": "NORWAY"}``. Vrací ``response.suggestions`` – 5–10 spojů
  (plánovač Entur OTP2, u spojů do Švédska i ``RESROBOT``) s úseky,
  mezizastávkami, dopravcem (Vy, SJ Nord, Go-Ahead, Vy Buss, Vy Bus4You …)
  a ``id`` itineráře. Další stránka: stejný dotaz s ``FIND_JOURNEY_SCROLL_LATER``
  a časem o minutu po posledním odjezdu (tak to dělá web);
* ``POST /api/offer/offer`` – ceny ``{"itineraryIds": […], "passengers":
  [{"age": 35, "interrailCode": "", "categories": []}], "addons": [],
  "promotionCode": "", "isRoundTrip": false, "currency": "nok", "userId": ""}``.
  Pro každý itinerář ``minimumPrice`` (v øre, ``amountInMonetaryUnit`` v NOK)
  – **nejlevnější cena za celou skupinu** – a ``segmentOffers[].groups``
  (Non-flexible / Flexible / Fully flexible tickets s ``fromPrice``).

Cestující (ověřeno na odpovědích API, říjen 2026): cenu určuje věk – děti
do 5 let zdarma (s dospělým; samotné Vy neprodá), 6–17 let dětská/mládežnická
cena (Oslo – Bergen 310 NOK místo 1238), od 18 let dospělý. Senior (od 67 let)
a student potřebují kategorii (``{"type": "senior-citizen"}``,
``{"type": "student"}``) – bez ní API počítá plné jízdné; s ní 50 % plné
(flexibilní) ceny, tj. i dráž než akční Non-flex jízdenka dospělého. Proto
seniora posíláme s kategorií a API samo vybere nejlevnější nabídku.

Na co si dát pozor:

* **AWS WAF.** API chrání AWS WAF: bezhlavý Chromium dostane rovnou
  „Human Verification“ (HTTP 405, captcha), obyčejné požadavky občas
  ``HTTP 202`` s prázdným tělem a hlavičkou ``x-amzn-waf-action: challenge``.
  Výzvu neřešíme – zkusíme to po chvilce ještě jednou, a když ochrana trvá,
  vrátíme jízdní řád bez cen (u hledání spojů chybu „blokováno“).
* Spoje z Resrobotu (Oslo → Stockholm se SJ) vy.no neprodává –
  ``BOOKABLE_ON_EXTERNAL_SITE`` s cenou 0 a odkazem na vy.se; cenu 0 nebereme.
* Itinerář platí jen krátce – ceny je potřeba žádat hned po hledání se
  stejnými ``id``.
* Časy jsou s posunem (+01:00/+02:00, Europe/Oslo) – stejné pásmo jako u nás.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Norsku (druhý může
být ve Švédsku či Dánsku).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from .. import geo
from ..errors import BlockedError, ProviderError, StationNotFoundError, TransientProviderError
from ..models import (DEFAULT_TZ, DiscountCard, FareStatus, Journey, Leg, Mode, SearchQuery,
                      Stop)
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

WEB = "https://www.vy.no"
API = WEB + "/api"
RESULTS = WEB + "/en/see-travel-suggestions"
PLACE_TTL = 30 * 24 * 3600
#: Kolikrát zkusit požadavek, když AWS WAF vrátí výzvu (202 / challenge).
WAF_ATTEMPTS = 3
WAF_PAUSE = 2.0
#: Nejvýš tolik stránek spojů (první + „pozdější“).
MAX_PAGES = 3
SENIOR_AGE = 67
CHILD_AGE = 6        # mladší jedou zdarma
ADULT_AGE = 18
#: Norské kraje (i zrušené z let 2020–2023) – poslední část ``shortDescription``.
NO_COUNTIES = {
    "oslo", "akershus", "buskerud", "ostfold", "innlandet", "vestfold", "telemark", "agder",
    "rogaland", "vestland", "more og romsdal", "trondelag", "nordland", "troms", "finnmark",
    "viken", "vestfold og telemark", "troms og finnmark", "hedmark", "oppland", "hordaland",
    "sogn og fjordane", "aust agder", "vest agder", "sor trondelag", "nord trondelag",
}
#: Česká jména → jak je zná našeptávač Vy.
ALIASES = {"stokholm": "Stockholm", "goteborg": "Göteborg", "kodan": "København",
           "copenhagen": "København", "trondhjem": "Trondheim"}
SCANDINAVIA = ("NO", "SE", "DK")
MODES = {"TRAIN": Mode.TRAIN, "BUS": Mode.BUS, "COACH": Mode.BUS, "TRAM": Mode.TRAM,
         "METRO": Mode.METRO, "WATER": Mode.FERRY, "FERRY": Mode.FERRY, "WALK": Mode.WALK}
GROUP_NAMES = {"NON_FLEX": "Non-flex", "SEMI_FLEX": "Flex", "FULL_FLEX": "Full flex"}


@PROVIDERS.register()
class VyProvider(HttpProvider):
    name = "Vy"
    label = "Vy – Norské dráhy (jen cesty z/do Norska; vlaky Vy, SJ Nord, Go-Ahead a autobusy Vy)"
    multi_passenger = True
    currency = "NOK"
    countries = ("NO", "SE", "DK")
    international_only = True
    provides_stops = True
    referer = RESULTS
    min_interval = 0.8
    min_timeout = 30.0

    # ---- přístup k API -----------------------------------------------------------
    async def _call(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                    body: Any = None) -> Any:
        """Požadavek jako z webu; výzvu AWS WAF neřešíme, jen to chvíli nato zkusíme znovu."""
        for attempt in range(WAF_ATTEMPTS):
            if attempt:
                await asyncio.sleep(WAF_PAUSE * attempt)
            response = await self.client.request(method, f"{API}/{path}", params=params, json_body=body,
                                                 referer=RESULTS, expect_json=False,
                                                 headers={"x-language": "en"})
            if is_waf_challenge(response.status_code, response.headers, response.text):
                log.debug("Vy: AWS WAF vrátil výzvu (%s), pokus %d", response.status_code, attempt + 1)
                continue
            try:
                return response.json()
            except ValueError as exc:
                raise TransientProviderError(self.name, f"nevalidní JSON z vy.no: {response.text[:120]!r}") \
                    from exc
        raise BlockedError(self.name, "vy.no odpovídá ochranou AWS WAF (výzva) – zkuste to později")

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        text = ALIASES.get(normalize(place), place)
        data = await self._call("GET", "location/places/v2/autosuggest",
                                params={"query": text, "prioritizePlacesEngine": "ENTUR",
                                        "searchType": "GLOBAL"})
        chosen = pick_place(text, (data or {}).get("suggestions") or [])
        if chosen is None:
            raise StationNotFoundError(self.name, f"místo {place!r} Vy nezná")
        self.cache.set(self.name, key, chosen, ttl=PLACE_TTL)
        return chosen

    # ---- hledání ---------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        # Vy (i Vy Tåg ve Švédsku) – cesty po Norsku, Švédsku a Dánsku i mezi nimi
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if any(c not in (None, *SCANDINAVIA) for c in known):
            raise StationNotFoundError(self.name, "Vy – jen cesty po Norsku, Švédsku a Dánsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        countries = [place_country(origin), place_country(destination)]
        if any(c not in (None, *SCANDINAVIA) for c in countries):
            raise StationNotFoundError(self.name, "Vy – jen cesty po Norsku, Švédsku a Dánsku")
        if origin["id"] == destination["id"]:
            raise StationNotFoundError(self.name, "stejné místo odjezdu i příjezdu")

        suggestions: dict[str, dict[str, Any]] = {}
        when, context = query.departure, "FIND_JOURNEY_INITIAL"
        for _ in range(MAX_PAGES):
            data = await self._call("POST", "global-itinerary/travel-planner/search",
                                    body=search_body(origin, destination, when, query, context))
            page = ((data or {}).get("response") or {}).get("suggestions") or []
            new = [s for s in page if s.get("id") and s["id"] not in suggestions]
            suggestions.update((s["id"], s) for s in new)
            last = max((_time(s.get("departure")) for s in page if _time(s.get("departure"))), default=None)
            if not new or last is None or last >= query.window_end:
                break
            when, context = last + dt.timedelta(minutes=1), "FIND_JOURNEY_SCROLL_LATER"

        link = deep_link(origin, destination, query.departure)
        journeys = []
        for raw in suggestions.values():
            journey = to_journey(raw, link)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        journeys.sort(key=lambda j: j.departure)
        if not journeys:
            return []

        try:
            offers = await self._call("POST", "offer/offer", body=offer_body([j.journey_id for j in journeys],
                                                                             query))
        except ProviderError as exc:
            self.note(f"ceny Vy se nepodařilo načíst: {exc.message}")
            for journey in journeys:
                journey.notes.append("Cenu Vy se nepodařilo zjistit – ověřte na vy.no.")
            return journeys
        by_id = {str(o.get("itineraryId")): o for o in offers if isinstance(o, dict)} \
            if isinstance(offers, list) else {}
        for journey in journeys:
            self.apply_offer(journey, by_id.get(journey.journey_id), query)
        return journeys

    def apply_offer(self, journey: Journey, offer: dict[str, Any] | None, query: SearchQuery) -> None:
        """Doplní do spoje cenu z ``offer/offer`` (cena je za celou skupinu)."""
        if not offer:
            return
        bookability = offer.get("bookability") or {}
        kind = str(bookability.get("type") or "")
        external = ((bookability.get("externalLink") or {}).get("url")) or None
        if kind == "BOOKABLE_ON_EXTERNAL_SITE":
            journey.fare_status = FareStatus.NOT_BOOKABLE
            journey.notes.append("Tento spoj vy.no neprodává – jízdenku kupte na vy.se nebo u dopravce.")
            if external:
                journey.deep_link = external
            return
        price = offer.get("minimumPrice") or {}
        if kind.startswith("FULLY_BOOKABLE") or kind == "PARTIALLY_BOOKABLE":
            amount = _amount(price)
            if amount:
                tariff = cheapest_tariff(offer)
                note = passenger_note(query)
                if note:
                    tariff = f"{tariff}, {note}" if tariff else note
                journey.fares = [self.make_fare(amount, "NOK", label="Vy", tariff=tariff or "Vy",
                                                flexible=tariff.startswith("Full flex"),
                                                deep_link=journey.deep_link)]
                journey.fare_status = FareStatus.PRICED
                if kind == "PARTIALLY_BOOKABLE":
                    journey.notes.append("Vy prodává jen část cesty – zbytek kupte u dalšího dopravce.")
                return
        if _sold_out(offer):
            journey.fare_status = FareStatus.SOLD_OUT
            return
        journey.fare_status = FareStatus.NOT_BOOKABLE
        summary = str(bookability.get("summary") or bookability.get("description") or "").strip()
        if summary:
            journey.notes.append(f"Vy: {summary}")


# ---- pomocné funkce (bez sítě, kvůli testům) ------------------------------------------
def is_waf_challenge(status: int, headers: Any, body: str) -> bool:
    """Odpověď AWS WAF místo dat: 202 + ``x-amzn-waf-action``, nebo stránka „Human Verification“."""
    action = ""
    try:
        action = str(headers.get("x-amzn-waf-action") or "")
    except AttributeError:
        pass
    if action:
        return True
    if status == 202 and not (body or "").strip():
        return True
    return status in (403, 405) and "awsWafCookieDomainList" in (body or "")[:4000]


def place_country(place: dict[str, Any]) -> str | None:
    """Země místa podle popisu z našeptávače (``…, Vestland`` / ``… län`` / ``Danmark``)."""
    desc = normalize(place.get("description") or "")
    if not desc:
        return None
    last = desc.split(",")[-1].strip()
    for prefix in ("city place in ", "public transport hub in ", "train station in ", "bus station in ",
                   "stop place in ", "address in ", "in "):
        if last.startswith(prefix):
            last = last[len(prefix):]
    if last.endswith(" lan") or last == "sverige":
        return "SE"
    if last in ("danmark", "denmark"):
        return "DK"
    if last in NO_COUNTIES or last == "norge":
        return "NO"
    if place.get("zones"):
        return "NO"      # tarifní zóny mají jen norské zastávky
    return None


def pick_place(text: str, suggestions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Zastávku (NSR) se shodným jménem, jinak hlavní nádraží města, jinak první zastávku."""
    items = [s for s in suggestions if str(s.get("id") or "").startswith("NSR:") and s.get("position")]
    if not items:
        return None
    wanted = normalize(text)

    def is_station(s: dict[str, Any]) -> bool:
        return any(c.get("id") == "railway-station" for c in s.get("categories") or [])

    exact = [s for s in items if normalize(s.get("name") or "") == wanted]
    chosen = None
    if exact:
        # Pro „Bergen“ raději „Bergen stasjon“ než skupinu „Bergen“ (město) – pokud je nádraží.
        chosen = exact[0]
        if not is_station(chosen):
            station = next((s for s in items if is_station(s)
                            and normalize(s.get("name") or "").startswith(wanted + " ")), None)
            chosen = station or chosen
    if chosen is None:
        city = [s for s in items if normalize(s.get("name") or "").split(" ")[0] == wanted.split(" ")[0]]
        stations = [s for s in city if is_station(s)]
        if stations:
            chosen = stations[0]
        elif city:
            chosen = city[0]
        else:
            idx = best_match(text, [str(s.get("name") or "") for s in items])
            chosen = items[idx if idx is not None else 0]
    return compact_place(chosen)


def compact_place(s: dict[str, Any]) -> dict[str, Any]:
    """Uložíme celý objekt z našeptávače – API hledání ho chce zpět beze změny."""
    return {"id": str(s["id"]), "name": str(s.get("name") or ""),
            "description": str(s.get("shortDescription") or ""),
            "zones": [z.get("id") for z in s.get("tariffZones") or [] if isinstance(z, dict)],
            "raw": s}


def search_body(origin: dict[str, Any], destination: dict[str, Any], when: dt.datetime,
                query: SearchQuery, context: str = "FIND_JOURNEY_INITIAL") -> dict[str, Any]:
    return {
        "from": origin["raw"], "to": destination["raw"],
        "datetime": _utc(when),
        "filter": {"includeModes": ["TRAIN", "BUS"]},
        "searchContext": context,
        "passengers": [{"id": i + 1, "age": p.age, "categories": []}
                       for i, p in enumerate(query.passengers)] or [{"id": 1, "age": 30, "categories": []}],
        "requestRegion": "NORWAY",
    }


def passenger_categories(age: int, cards: tuple[DiscountCard, ...] = ()) -> list[dict[str, str]]:
    """Slevová kategorie Vy podle věku (senior od 67) a průkazu (student)."""
    if age >= SENIOR_AGE:
        return [{"type": "senior-citizen"}]
    if ADULT_AGE <= age < 26 and (DiscountCard.ISIC in cards or DiscountCard.CZ_STUDENT in cards):
        return [{"type": "student"}]
    return []


def offer_body(itinerary_ids: list[str], query: SearchQuery) -> dict[str, Any]:
    passengers = [{"age": p.age, "interrailCode": "", "categories": passenger_categories(p.age, p.cards)}
                  for p in query.passengers] or [{"age": 30, "interrailCode": "", "categories": []}]
    if all(p["age"] < CHILD_AGE for p in passengers):
        # malé děti samy necestují – nejstarší pojede jako dítě s vlastní jízdenkou
        oldest = max(passengers, key=lambda p: p["age"])
        oldest["age"] = CHILD_AGE
    return {"itineraryIds": itinerary_ids, "passengers": passengers, "addons": [], "promotionCode": "",
            "isRoundTrip": False, "currency": "nok", "userId": ""}


def passenger_note(query: SearchQuery) -> str:
    parts = []
    if any(CHILD_AGE <= p.age < ADULT_AGE for p in query.passengers):
        parts.append("děti/mládež 6–17 let zlevněné")
    if any(p.age < CHILD_AGE for p in query.passengers):
        parts.append("děti do 5 let zdarma")
    if any(p.age >= SENIOR_AGE for p in query.passengers):
        parts.append("senior 67+ −50 %")
    if any(passenger_categories(p.age, p.cards) == [{"type": "student"}] for p in query.passengers):
        parts.append("student")
    return ", ".join(parts)


def cheapest_tariff(offer: dict[str, Any]) -> str:
    """Název nejlevnější skupiny jízdenek u každého úseku („Non-flex“, „Flex“, „Full flex“)."""
    names = []
    for segment in offer.get("segmentOffers") or []:
        groups = [g for g in segment.get("groups") or [] if not g.get("soldOut") and _amount(g.get("fromPrice"))]
        if not groups:
            continue
        best = min(groups, key=lambda g: _amount(g.get("fromPrice")) or Decimal(0))
        names.append(GROUP_NAMES.get(str(best.get("type") or ""), str(best.get("name") or "")))
    names = list(dict.fromkeys(n for n in names if n))
    return " + ".join(names)


def _sold_out(offer: dict[str, Any]) -> bool:
    groups = [g for s in offer.get("segmentOffers") or [] for g in s.get("groups") or []]
    return bool(groups) and all(g.get("soldOut") for g in groups)


def deep_link(origin: dict[str, Any], destination: dict[str, Any], when: dt.datetime) -> str:
    """Odkaz na výsledky hledání na vy.no (stejné parametry, jaké skládá web)."""
    params = [("fromDateTime", _utc(when))]
    for prefix, place in (("from", origin), ("to", destination)):
        raw = place.get("raw") or {}
        refs = ",".join(f"{r.get('origin')}:{r.get('id')}" for r in raw.get("externalReferences") or [])
        pos = raw.get("position") or {}
        params += [(prefix, place["name"]), (f"{prefix}ExternalId", refs or f"NSR:{place['id']}"),
                   (f"{prefix}Position", f"{pos.get('latitude')},{pos.get('longitude')}")]
        zones = raw.get("tariffZones")
        if zones:
            encoded = base64.b64encode(json.dumps(zones, separators=(",", ":")).encode()).decode()
            params.append((f"{prefix}TariffZones", encoded.replace("=", "")))
    return f"{RESULTS}?{urlencode(params)}"


def to_journey(raw: dict[str, Any], link: str | None = None) -> Journey | None:
    legs = []
    for leg in raw.get("legs") or []:
        kind = str(leg.get("mode") or "").upper()
        if kind in ("TRANSFER", "WAIT"):
            continue
        src, dst = leg.get("from") or {}, leg.get("to") or {}
        dep = _time(leg.get("departureScheduled"))
        arr = _time(leg.get("arrivalScheduled"))
        if dep is None or arr is None:
            continue
        origin, destination = str(src.get("displayName") or ""), str(dst.get("displayName") or "")
        mode = MODES.get(kind, Mode.UNKNOWN)
        if mode is Mode.WALK and origin == destination:
            continue          # „přechod“ v rámci jedné zastávky
        stops = [Stop(name=origin, departure=dep, platform=src.get("platform") or None)]
        for s in leg.get("intermediateStops") or []:
            if s.get("isCancelled"):
                continue
            when = _time(s.get("departureScheduled") or s.get("arrivalScheduled"))
            stops.append(Stop(name=str(s.get("displayName") or ""), arrival=when, departure=when))
        stops.append(Stop(name=destination, arrival=arr, platform=dst.get("platform") or None))
        line = leg.get("line") or {}
        operator = leg.get("operator") or leg.get("authority") or {}
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr, mode=mode,
                        carrier=str(operator.get("name") or ("" if mode is Mode.WALK else "Vy")),
                        line=str(line.get("name") or ""), stops=[s for s in stops if s.name]))
        if leg.get("cancellationStatus") not in (None, "NOT_CANCELLED") or src.get("isCancelled") \
                or dst.get("isCancelled"):
            raw.setdefault("_cancelled", True)
    if not any(leg.mode is not Mode.WALK for leg in legs):
        return None
    journey = Journey(provider="Vy", legs=legs, fare_status=FareStatus.UNKNOWN,
                      journey_id=str(raw.get("id") or ""), deep_link=link or RESULTS)
    if raw.get("_cancelled"):
        journey.notes.append("Vy hlásí u spoje zrušení – ověřte na vy.no.")
    for msg in raw.get("messages") or []:
        title = msg.get("title") if isinstance(msg, dict) else None
        if title:
            journey.notes.append(f"Vy: {title}")
    return journey


def _amount(price: Any) -> Decimal | None:
    if not isinstance(price, dict):
        return None
    if price.get("value") is not None:
        return Decimal(int(price["value"])) / 100
    if price.get("amountInMonetaryUnit") not in (None, ""):
        return Decimal(str(price["amountInMonetaryUnit"]))
    return None


def _utc(when: dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=DEFAULT_TZ)
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(re.sub(r"Z$", "+00:00", value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
