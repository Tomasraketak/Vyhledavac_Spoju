"""SJ – Švédské dráhy (Statens Järnvägar, e-shop www.sj.se).

Web www.sj.se (React) volá veřejné API ``prod-api.adp.sj.se`` (Azure API
Management). Stačí obyčejné HTTP požadavky s klíčem
``Ocp-Apim-Subscription-Key``, který web sám vypisuje v JS konfiguraci
(``/assets/sjse-container/env-*.js``: ``API_SUBSCRIPTION_KEY`` pro prodej,
``TRAFFIC_INFO_SUBSCRIPTION_KEY`` pro provozní informace). Klíče se proto
jednou za den přečtou z webu; když se to nepovede, použijí se záložní
z října 2026. Žádné přihlášení, cookies ani captcha nejsou potřeba.

Volání (``/public/sales/booking/v3``):

* ``GET config`` – mimo jiné všech ~6 500 stanic a zastávek (``uicStationCode``,
  ``synonyms``, ``agglomeration`` = celé město, např. „Stockholm“ 740098000).
  Země se pozná z prefixu kódu UIC (74 Švédsko, 76 Norsko, 86 Dánsko,
  80 Německo). Seznam se drží v cache týden;
* ``POST search`` ``{"origin", "destination", "departureDate", "passengers":
  [{"passengerCategory": {"type": "ADULT"}}, …]}`` – založí hledání na celý
  den a vrátí ``departureSearchId`` a ``passengerListId``;
* ``GET departures/search/{departureSearchId}`` – spoje celého dne (úseky,
  vlaky, dopravci, přestupy); bez cen;
* ``GET departures/{departureId}/offers?passengerListId=…`` – ceny jednoho
  spoje **za celou skupinu**: ``seatOffers`` (třídy SECOND, SECOND_CALM,
  FIRST × NOFLEX/SEMIFLEX/FULLFLEX) a u nočních vlaků ``bedOffers``
  (lehátko, lůžko). Bereme nejlevnější nabídku 2. třídy, 1. třídy a lůžka.

Zastávky po cestě dává provozní API (``/public/trafficinfo-api/v2/rest/
segments``, jiný klíč): jedním POSTem pro všechny úseky
(``departureUicStationCode``, ``arrivalUicStationCode``, ``serviceName``,
``serviceScheduleDate``, ``ricsCode``). Když selže, zůstanou jen konce úseků.

Cestující (kategorie a věkové hranice z ``config``, pravidla SJ):

* ``CHILD_AND_YOUTH`` 0–25 let (posílá se věk) – děti a mládež; děti s
  dospělým dostávají „SJ Familjeerbjudande“ (Stockholm – Göteborg 36 SEK);
* ``ADULT`` 26–64 let (věk se neposílá – API ho odmítne);
* ``SENIOR`` (pensionär) od 65 let (API bere i mladší důchodce, věk povinný);
* děti do 6 let včetně smějí jet zdarma na klíně cestujícího od 16 let – do
  hledání je neposíláme (web je ze seznamu také vyřadí). Bez takového
  doprovodu jedou jako ``CHILD_AND_YOUTH`` s vlastním místem.
* ``STUDENT`` (od 15 let, se studentskou kartou) nepoužíváme – do 25 let je
  cena mládeže stejná nebo nižší.

Na co si dát pozor:

* SJ prodává i průběžné jízdenky s jinými dopravci (Västtrafik, Mälartåg,
  Öresundståg, Vy, Snälltåget, Norrtåg, autobusy Vy Bus4You) – a také do
  Osla, Kodaně, Narviku (a v létě nočním vlakem do Hamburku a Berlína).
  Zahraniční konec stačí, druhý musí ležet ve Švédsku.
* Ceny se ptáme spoj po spoji (jako web) – jen u spojů v časovém okně,
  jen u prvních 3 spojů do 4:15 h od odjezdu (``price_scope``). Jízdní řád se mezitím ukáže přes ``publish``.
* ``available: false`` s ``departureStatus`` ``NO_DEPARTURE`` = spoj SJ
  neprodává (ještě není v prodeji nebo nejede), ``FULLY_BOOKED`` = vyprodáno.
* Cena je v SEK včetně případného poplatku za rezervaci („Booking fee“) cizího
  dopravce. Online lze koupit nejvýš pro 9 osob.
* ``Accept-Language: en-GB`` – názvy tarifů pak jsou anglicky
  („2 class, Non-rebookable“), stanice zůstávají švédsky.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Švédsku.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from .. import geo
from ..errors import ProviderError, StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider, PRICE_SCOPE_COUNT, price_scope

log = logging.getLogger(__name__)

WEB = "https://www.sj.se/"
API = "https://prod-api.adp.sj.se"
BOOKING = API + "/public/sales/booking/v3"
TRAFFIC = API + "/public/trafficinfo-api/v2/rest/segments"
#: Klíče z JS konfigurace www.sj.se (říjen 2026) – záloha, když se web nepodaří načíst.
FALLBACK_KEYS = {"booking": "d6625619def348d38be070027fd24ff6",
                 "traffic": "39296c1a13304493b44236e1bcb7f544"}
KEY_TTL = 24 * 3600
STATIONS_TTL = 7 * 24 * 3600
#: Nejvýš tolik spojů se ptáme na cenu (jeden požadavek na spoj).
MAX_OFFERS = PRICE_SCOPE_COUNT
MAX_PERSONS = 9
#: Prefix kódu UIC → země.
UIC_COUNTRY = {"74": "SE", "76": "NO", "86": "DK", "80": "DE", "81": "AT", "84": "NL", "88": "BE",
               "85": "CH", "87": "FR", "10": "FI", "51": "PL", "54": "CZ"}
#: Česká a jiná jména → název (bez diakritiky) v seznamu SJ.
ALIASES = {"kodan": "kopenhamn", "copenhagen": "kopenhamn", "kobenhavn": "kopenhamn",
           "gothenburg": "goteborg", "hamburk": "hamburg", "stokholm": "stockholm",
           "upsala": "uppsala"}
#: Hlavní nádraží města – podle konce názvu.
_MAIN = (" central", " c", " h", " s", " hbf", " station")
_KEY = {"booking": re.compile(r"API_SUBSCRIPTION_KEY:\s*[`'\"]([0-9a-f]{32})"),
        "traffic": re.compile(r"TRAFFIC_INFO_SUBSCRIPTION_KEY:\s*[`'\"]([0-9a-f]{32})")}
_ENV_JS = re.compile(r'(/assets/sjse-container/env-[\w-]+\.js)')


@PROVIDERS.register()
class SjProvider(HttpProvider):
    name = "SJ"
    label = "SJ – Švédské dráhy (jen cesty z/do Švédska; i do Osla, Kodaně, Narviku)"
    multi_passenger = True
    provides_stops = True
    currency = "SEK"
    countries = ("SE", "NO", "DK", "DE")
    international_only = True
    referer = WEB
    min_interval = 0.15

    # ---- přístup k API --------------------------------------------------------------
    async def api_keys(self) -> dict[str, str]:
        cached = self.cache.get(self.name, "keys")
        if cached:
            return cached
        keys = dict(FALLBACK_KEYS)
        try:
            page = await self.client.get_text(WEB, referer=WEB)
            m = _ENV_JS.search(page)
            if m:
                keys.update(parse_keys(await self.client.get_text(WEB.rstrip("/") + m.group(1), referer=WEB)))
        except Exception as exc:  # noqa: BLE001 - záložní klíče
            log.debug("SJ: konfiguraci webu nelze načíst: %s", exc)
            return keys
        self.cache.set(self.name, "keys", keys, ttl=KEY_TTL)
        return keys

    async def _headers(self, kind: str = "booking") -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": (await self.api_keys())[kind], "Accept-Language": "en-GB",
                "Origin": "https://www.sj.se", "x-client-name": "sjse-booking-client"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get_json(f"{BOOKING}/{path}", params=params, referer=WEB,
                                          headers=await self._headers())

    async def _post(self, url: str, body: Any, kind: str = "booking") -> Any:
        return await self.client.post_json(url, body, referer=WEB, headers=await self._headers(kind))

    # ---- stanice ---------------------------------------------------------------------
    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        items = compact_stations((await self._get("config") or {}).get("stations") or [])
        if not items:
            raise TransientProviderError(self.name, "seznam stanic SJ je prázdný")
        self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, Any]:
        chosen = pick_station(place, await self.stations())
        if chosen is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} SJ nezná")
        return chosen

    # ---- hledání ---------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "SE" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Švédsku")
        passengers = passenger_list(query)
        if len(passengers) > MAX_PERSONS:
            raise ProviderError(self.name, f"SJ prodává online nejvýš {MAX_PERSONS} cestujícím")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if "SE" not in (origin["country"], destination["country"]):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Švédsku")
        if origin["code"] == destination["code"]:
            raise StationNotFoundError(self.name, "výchozí a cílová stanice je stejná")
        start = query.departure.astimezone(DEFAULT_TZ)
        end = query.window_end.astimezone(DEFAULT_TZ)
        days = [start.date()]
        if end.date() > start.date() and end.hour >= 4:
            days.append(end.date())
        results = await asyncio.gather(*(self._day(origin, destination, d, passengers) for d in days),
                                       return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == len(results):
            raise errors[0]
        if errors:
            self.note(f"část spojů SJ se nepodařilo načíst: {getattr(errors[0], 'message', errors[0])}")
        pending: list[tuple[Journey, str]] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                continue
            list_id, items = result
            for item in items:
                journey = to_journey(item, origin["name"], destination["name"])
                if journey is None or journey.journey_id in seen:
                    continue
                if query.departure <= journey.departure <= query.window_end:
                    seen.add(journey.journey_id)
                    pending.append((journey, list_id))
        # cena = dotaz na každý spoj zvlášť – jen první spoje (price_scope)
        scope = {id(j) for j in price_scope([j for j, _ in pending], query, MAX_OFFERS)}
        pending = sorted((p for p in pending if id(p[0]) in scope), key=lambda p: p[0].departure)
        journeys = [j for j, _ in pending]
        self.publish(journeys)
        await asyncio.gather(*(self._price(j, list_id) for j, list_id in pending),
                             self._stops(journeys))
        return journeys

    async def _day(self, origin: dict[str, Any], destination: dict[str, Any], day: dt.date,
                   passengers: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        found = await self._post(f"{BOOKING}/search", {
            "origin": origin["code"], "destination": destination["code"],
            "departureDate": day.isoformat(), "passengers": passengers})
        search_id, list_id = (found or {}).get("departureSearchId"), (found or {}).get("passengerListId")
        if not search_id or not list_id:
            raise TransientProviderError(self.name, "SJ nezaložilo hledání")
        data = await self._get(f"departures/search/{search_id}")
        items = [d for t in (data or {}).get("travels") or [] for d in t.get("departures") or []]
        return list_id, items

    async def _price(self, journey: Journey, list_id: str) -> None:
        try:
            data = await self._get(f"departures/{journey.journey_id}/offers", {"passengerListId": list_id})
        except ProviderError as exc:
            self.note(f"cenu spoje SJ se nepodařilo zjistit: {exc.message}")
            return
        self.apply_offers(journey, data or {})

    def apply_offers(self, journey: Journey, data: dict[str, Any]) -> None:
        fares = []
        for group, amount, flex, tariff, family in cheapest_offers(data):
            note = ", rodinná sleva SJ pro děti" if family else ""
            fares.append(self.make_fare(amount, "SEK", label=f"SJ {GROUP_LABELS[group]}",
                                        tariff=tariff + note, flexible=flex != "NOFLEX",
                                        deep_link=journey.deep_link))
        if fares:
            journey.fares = fares
            journey.fare_status = FareStatus.PRICED
            return
        status = [str(s) for s in data.get("departureStatus") or []]
        if any("FULL" in s or "SOLD" in s for s in status):
            journey.fare_status = FareStatus.SOLD_OUT
        elif status and "AVAILABLE" not in status:
            journey.fare_status = FareStatus.NOT_BOOKABLE
            journey.notes.append(f"SJ tento spoj teď neprodává ({', '.join(status)}).")

    async def _stops(self, journeys: list[Journey]) -> None:
        segments, legs = [], []
        for journey in journeys:
            for leg, raw in zip(journey.legs, journey.raw.get("legs") or []):
                if raw.get("serviceName") and raw.get("ricsCode"):
                    segments.append(raw)
                    legs.append(leg)
        if not segments:
            return
        try:
            data = await self._post(TRAFFIC, {"segments": segments}, kind="traffic")
        except ProviderError as exc:
            log.debug("SJ: zastávky nelze načíst: %s", exc)
            return
        for leg, seg in zip(legs, (data or {}).get("segments") or []):
            stops = parse_stops(seg)
            if len(stops) >= 2:
                leg.stops = stops


# ---- pomocné funkce (bez sítě, kvůli testům) ------------------------------------------
GROUP_LABELS = {"2": "2. třída", "1": "1. třída", "bed": "lůžko/lehátko"}


def parse_keys(js: str) -> dict[str, str]:
    out = {}
    for kind, pattern in _KEY.items():
        m = pattern.search(js or "")
        if m:
            out[kind] = m.group(1)
    return out


def compact_stations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for s in items:
        code, name = str(s.get("uicStationCode") or ""), str(s.get("name") or "")
        if not code or not name:
            continue
        out.append({"code": code, "name": name, "short": str(s.get("shortName") or ""),
                    "synonyms": [str(x) for x in s.get("synonyms") or [] if not str(x).isdigit()],
                    "city": bool(s.get("agglomeration")), "country": UIC_COUNTRY.get(code[:2], "")})
    return out


def _wanted(place: str) -> str:
    return " ".join(ALIASES.get(w, w) for w in normalize(place).split())


def pick_station(place: str, stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Přesná shoda (i zkratka, synonymum), pak celé město, pak hlavní nádraží, pak nejpodobnější."""
    wanted = _wanted(place)
    if not wanted:
        return None
    variants = [wanted]
    if wanted.endswith(" c"):
        variants.append(wanted[:-2] + " central")
    exact = [s for s in stations
             if any(normalize(n) in variants for n in (s["name"], s["short"], *s["synonyms"]))]
    if exact:
        return min(exact, key=lambda s: (not s["city"], normalize(s["name"]) not in variants, len(s["name"])))
    city = [s for s in stations if normalize(s["name"]).startswith(wanted + " ")]
    if city:
        return min(city, key=lambda s: (not normalize(s["name"]).endswith(_MAIN), len(s["name"])))
    idx = best_match(wanted, [normalize(s["name"]) for s in stations], threshold=0.5)
    return stations[idx] if idx is not None else None


def passenger_list(query: SearchQuery) -> list[dict[str, Any]]:
    """Cestující pro ``POST search``; děti do 6 let na klíně cestujících od 16 let vynechá."""
    ages = sorted((p.age for p in query.passengers), reverse=True)
    laps = sum(1 for a in ages if a >= 16)
    out = []
    for age in ages:
        if age <= 6 and laps > 0:
            laps -= 1
            continue
        if age <= 25:
            out.append({"passengerCategory": {"type": "CHILD_AND_YOUTH", "age": age}})
        elif age >= 65:
            out.append({"passengerCategory": {"type": "SENIOR", "age": age}})
        else:
            out.append({"passengerCategory": {"type": "ADULT"}})
    return out or [{"passengerCategory": {"type": "ADULT"}}]


def _offers(data: dict[str, Any]):
    """(skupina, flexibilita, nabídka) pro sedadla i lůžka."""
    for comfort, item in ((data.get("seatOffers") or {}).get("offers") or {}).items():
        group = "1" if str(comfort).startswith("FIRST") else "2"
        for flex, offer in (item.get("flexibilities") or {}).items():
            yield group, flex, offer
    for item in ((data.get("bedOffers") or {}).get("offers") or {}).values():
        for comfort in (item.get("comfortTypes") or {}).values():
            for flex, offer in (comfort.get("flexibilities") or {}).items():
                yield "bed", flex, offer


def cheapest_offers(data: dict[str, Any]) -> list[tuple[str, Decimal, str, str, bool]]:
    """Nejlevnější dostupná nabídka v každé skupině: (skupina, cena SEK, flexibilita, tarif, rodinná sleva)."""
    best: dict[str, tuple[str, Decimal, str, str, bool]] = {}
    for group, flex, offer in _offers(data):
        if not offer.get("available"):
            continue
        prices = offer.get("journeyPrices") or {}
        amount = (prices.get("totalPriceToBePaid") or prices.get("price") or {}).get("amount")
        if amount is None:
            continue
        value = Decimal(str(amount))
        if group in best and best[group][1] <= value:
            continue
        names = [str(i.get("productName") or "") for i in offer.get("offerInformation") or []]
        names = list(dict.fromkeys(n for n in names if n and "fee" not in n.lower() and "avgift" not in n.lower()))
        best[group] = (group, value, str(flex), " + ".join(names) or str(flex),
                       bool(prices.get("hasFamilyDiscount")))
    return [best[g] for g in ("2", "1", "bed") if g in best]


def _mode(raw: dict[str, Any]) -> Mode:
    service = raw.get("serviceType") or {}
    text = " ".join(str(x or "") for x in (raw.get("transportMethod"), service.get("modality"),
                                           service.get("code"))).upper()
    if "BUS" in text:
        return Mode.BUS
    if "FERRY" in text or "BOAT" in text or "SHIP" in text:
        return Mode.FERRY
    if "TRAM" in text:
        return Mode.TRAM
    if "METRO" in text or "SUBWAY" in text:
        return Mode.METRO
    if "WALK" in text:
        return Mode.WALK
    return Mode.TRAIN


def deep_link(origin: str, destination: str, day: str) -> str:
    return f"{WEB}sok-resa/valj-resa/{quote(origin)}/{quote(destination)}/{day}"


def to_journey(item: dict[str, Any], origin_name: str = "", destination_name: str = "") -> Journey | None:
    legs, raw_legs = [], []
    for raw in item.get("legs") or []:
        dep, arr = _time(raw.get("departureDateTime")), _time(raw.get("arrivalDateTime"))
        if dep is None or arr is None:
            continue
        start, stop = raw.get("origin") or {}, raw.get("destination") or {}
        service = raw.get("serviceType") or {}
        number = str(raw.get("publicServiceName") or raw.get("serviceName") or "")
        kind = str(service.get("name") or service.get("description") or "")
        props = {str(p.get("code") or "") for p in raw.get("serviceProperties") or []}
        legs.append(Leg(origin=str(start.get("name") or ""), destination=str(stop.get("name") or ""),
                        departure=dep, arrival=arr, mode=_mode(raw),
                        carrier=str(service.get("operatorName") or "SJ"),
                        line=f"{kind} {number}".strip(),
                        reservation_required="RESERVATION-INCLUDED" in props))
        raw_legs.append({"departureUicStationCode": start.get("uicStationCode"),
                         "arrivalUicStationCode": stop.get("uicStationCode"),
                         "serviceName": raw.get("serviceName"),
                         "serviceScheduleDate": raw.get("serviceScheduleDate"),
                         "transportMethod": raw.get("transportMethod") or "TRAIN",
                         "ricsCode": service.get("ricsCode")})
    if not legs:
        return None
    day = str(item.get("departureDate") or legs[0].departure.date().isoformat())
    link = deep_link(origin_name or (item.get("origin") or {}).get("name") or legs[0].origin,
                     destination_name or (item.get("destination") or {}).get("name") or legs[-1].destination,
                     day)
    journey = Journey(provider="SJ", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=link,
                      journey_id=str(item.get("departureId") or ""))
    journey.raw = {"legs": raw_legs}
    reasons = [str(r.get("description") or r.get("code") or r) if isinstance(r, dict) else str(r)
               for r in item.get("unavailableReasons") or []]
    if reasons:
        journey.notes.append("SJ: " + "; ".join(reasons))
    return journey


def parse_stops(segment: dict[str, Any]) -> list[Stop]:
    stops = []
    for s in segment.get("stations") or []:
        arrival = (s.get("arrival") or {}).get("originalTime")
        departure = (s.get("departure") or {}).get("originalTime")
        if s.get("name"):
            stops.append(Stop(name=str(s["name"]), arrival=_local(arrival), departure=_local(departure)))
    return stops


def _local(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=DEFAULT_TZ)
    except ValueError:
        return None


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
