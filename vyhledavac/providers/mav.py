"""MÁV (Maďarské státní dráhy) – e-shop jegy.mav.hu.

Web volá veřejné JSON API ``jegy-a.mav.hu/IK_API_PROD/api`` (POST, hlavičky
``Language`` a ``UserSessionId`` – stačí libovolné UUID):

* ``OfferRequestApi/GetStationList`` – ~54 tis. míst (vlakové stanice,
  autobusové zastávky; zahraniční stanice bez diakritiky: „Brno hl. n.“,
  skupina „BUDAPEST*“ pro všechna budapešťská nádraží);
* ``OfferRequestApi/GetCustomersAndDiscounts`` – typy cestujících podle
  věku, zvlášť pro vnitrostátní (``InternalTicket``) a mezinárodní
  (``InternationalTicket``) jízdenky;
* ``OfferRequestApi/GetServicesAndSearchServices`` – kód služby „přeprava
  ve 2. třídě“ (vnitrostátně 52, mezinárodně 50);
* ``OfferRequestApi/GetOfferRequest`` – spoje s cenou za celou skupinu
  (``travelClasses`` třídy „2“; mezinárodně v EUR, po Maďarsku v HUF)
  a rozpisem vlaků (``details.routes``).

Ptáme se **jen tehdy, když aspoň jeden konec cesty leží v Maďarsku** –
jinak je dotaz zbytečný (seznam stanic má 2,5 MB a MÁV by ho stahovali
i pro Praha – Brno).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://jegy-a.mav.hu/IK_API_PROD/api/OfferRequestApi"
WEB = "https://jegy.mav.hu/"
STATIONS_TTL = 14 * 24 * 3600
TYPES_TTL = 7 * 24 * 3600
#: Kódy offerkind v dotazu na spoje.
INTERNAL, INTERNATIONAL = "1", "4"
#: Výchozí kódy služby „přeprava ve 2. třídě“, když se je nepodaří načíst.
SECOND_CLASS = {"InternalTicket": 52, "InternationalTicket": 50}
#: Kódy železničních stanic podle UIC kódu země (autobusové zastávky MÁV-Volán mají „0042…“).
#: Zkratky druhů vlaků (jako je píší ČD a ÖBB – kvůli párování spojů).
KINDS = {"railjet xpress": "rjx", "railjet": "rj", "eurocity": "EC", "intercity": "IC",
         "euronight": "EN", "nightjet": "NJ", "interregio": "IR", "interrégió": "IR",
         "gyorsvonat": "Gyv", "sebesvonat": "Sebv", "személyvonat": "Szv",
         "zónázó személyvonat": "Szv", "expresszvonat": "Exp", "regionalexpress": "REX",
         "regionalzug": "R", "rychlík": "R", "osobní vlak": "Os"}
RAIL_PREFIXES = ("0055", "0054", "0056", "0081", "0051", "0080", "0079", "0078", "0053", "0072")


@PROVIDERS.register()
class MavProvider(HttpProvider):
    name = "MÁV"
    label = "MÁV – Maďarské dráhy (jen cesty z/do Maďarska)"
    multi_passenger = True
    currency = "EUR"
    countries = ("HU", "AT", "SK", "CZ", "DE", "RO", "HR", "SI", "RS")
    international_only = True          # po Česku nemá smysl
    referer = WEB
    min_interval = 0.5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session_id = str(uuid.uuid4())

    async def _post(self, endpoint: str, body: dict[str, Any]) -> Any:
        return await self.client.post_json(f"{API}/{endpoint}", body, referer=WEB, headers={
            "Language": "en", "UserSessionId": self._session_id, "Origin": WEB.rstrip("/")})

    # ---- číselníky ---------------------------------------------------------------
    async def stations(self) -> list[list[str]]:
        """``[jméno, kód, země ISO, alias(0/1)]`` – kompaktně kvůli cache."""
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        data = await self._post("GetStationList", {})
        items = [[str(s.get("name") or ""), str(s.get("code") or ""), str(s.get("coutryIso") or ""),
                  "1" if s.get("isAlias") else "0"]
                 for s in (data or {}).get("stations") or []
                 if s.get("canUseForOfferRequest") and s.get("code") and s.get("name")]
        if items:
            self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def customer_types(self, kind: str) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, f"types:{kind}")
        if cached:
            return cached
        data = await self._post("GetCustomersAndDiscounts", {"OfferKind": kind})
        items = [{"key": str(c["key"]), "min": c.get("minAge"), "max": c.get("maxAge")}
                 for c in (data or {}).get("customerTypes") or [] if c.get("key")]
        if items:
            self.cache.set(self.name, f"types:{kind}", items, ttl=TYPES_TTL)
        return items

    async def second_class(self, kind: str) -> int:
        cached = self.cache.get(self.name, f"service:{kind}")
        if cached:
            return int(cached)
        try:
            data = await self._post("GetServicesAndSearchServices", {"OfferKind": kind})
        except Exception as exc:  # noqa: BLE001 - výchozí kód stačí
            log.debug("MÁV: služby: %s", exc)
            return SECOND_CLASS[kind]
        code = next((s.get("code") for s in (data or {}).get("services") or []
                     if s.get("isDefault") and "2nd class" in str(s.get("name") or "")), None)
        code = int(code) if code is not None else SECOND_CLASS[kind]
        self.cache.set(self.name, f"service:{kind}", code, ttl=TYPES_TTL)
        return code

    # ---- hledání -----------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "HU" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Maďarsku")
        stations = await self.stations()
        origin, destination = pick_station(query.origin, stations), pick_station(query.destination, stations)
        for place, found in ((query.origin, origin), (query.destination, destination)):
            if found is None:
                raise StationNotFoundError(self.name, f"stanici {place!r} MÁV nezná")
        if "HU" not in (origin[2], destination[2]):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Maďarsku")
        domestic = origin[2] == destination[2] == "HU"
        kind = "InternalTicket" if domestic else "InternationalTicket"
        types, service = await asyncio.gather(self.customer_types(kind), self.second_class(kind))
        passengers = []
        for i, passenger in enumerate(query.passengers):
            key = customer_type(passenger, types)
            if key is None:
                raise StationNotFoundError(self.name, f"MÁV nemá typ cestujícího pro věk {passenger.age}")
            passengers.append({"passengerCount": 1, "passengerId": i, "customerTypeKey": key,
                               "customerDiscountsKeys": []})
        when = query.departure.astimezone(DEFAULT_TZ).isoformat(timespec="seconds")
        data = await self._post("GetOfferRequest", {
            "offerkind": INTERNAL if domestic else INTERNATIONAL,
            "startStationCode": origin[1], "innerStationsCodes": [], "endStationCode": destination[1],
            "passangers": passengers, "isOneWayTicket": True, "isTravelEndTime": False,
            "isSupplementaryTicketsOnly": False, "travelStartDate": when, "travelReturnDate": when,
            "selectedServices": [service], "selectedSearchServices": [], "eszkozSzamok": [],
            "isOfDetailedSearch": False})
        journeys = []
        for raw in (data or {}).get("route") or []:
            journey = self.to_journey(raw)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def to_journey(self, raw: dict[str, Any]) -> Journey | None:
        legs = parse_legs(raw)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        price = second_class_price(raw)
        if price is not None and not raw.get("pricingFailed"):
            amount, currency = price
            tickets = "; ".join(dict.fromkeys(str(t.get("name") or "")
                                              for t in (raw.get("details") or {}).get("tickets") or []
                                              if t.get("name")))
            journey.fares = [self.make_fare(amount, currency, label="MÁV", tariff=tickets)]
            journey.fare_status = FareStatus.PRICED
        if raw.get("orderDisabled"):
            journey.fare_status = FareStatus.NOT_BOOKABLE
            reason = raw.get("orderDisabledReason")
            if reason:
                journey.notes.append(f"MÁV: {reason}")
        return journey


def pick_station(place: str, stations: list[list[str]]) -> list[str] | None:
    """Stanice MÁV pro jméno místa: přesná shoda (bez diakritiky), skupina
    celého města („BUDAPEST*“), jinak nejpodobnější železniční stanice."""
    wanted = normalize(place)
    if not wanted:
        return None
    rail = [s for s in stations if s[1].startswith(RAIL_PREFIXES)]
    group = [s for s in rail if normalize(s[0].rstrip("*")) == wanted and s[0].endswith("*")]
    if group:
        return group[0]
    exact = [s for s in rail if normalize(s[0]) == wanted]
    if exact:
        return min(exact, key=lambda s: s[3])           # skutečné jméno před aliasem
    # „Budapest“ -> skupina „Bp (BUDAPEST*)“, „Wien“ -> „Wien Hbf“
    for s in rail:
        if s[0].endswith("*)") and normalize(s[0].split("(")[-1].rstrip("*)")) == wanted:
            return s
    town = [s for s in rail if normalize(s[0]).startswith(wanted + " ") and s[3] == "0"]
    for suffix in (" hbf", " hl n", " hl st", " keleti", " nyugati"):
        hit = [s for s in town if normalize(s[0]) == wanted + suffix]
        if hit:
            return hit[0]
    idx = best_match(place, [s[0] for s in rail], threshold=0.85)
    return rail[idx] if idx is not None else None


def customer_type(passenger: Passenger, types: list[dict[str, Any]]) -> str | None:
    """Typ cestujícího MÁV podle věku (z číselníku – klíče se mění)."""
    for t in types:
        lo, hi = t.get("min"), t.get("max")
        if lo is None or hi is None:
            continue                     # kolo, pes
        if lo <= passenger.age <= hi:
            return t["key"]
    return None


def second_class_price(raw: dict[str, Any]) -> tuple[Decimal, str] | None:
    for cls in raw.get("travelClasses") or []:
        price = cls.get("price") or {}
        if str(cls.get("name")) == "2" and price.get("amount") is not None:
            currency = ((price.get("currency") or {}).get("uicCode")
                        or (price.get("currency") or {}).get("name") or "HUF")
            return Decimal(str(price["amount"])), str(currency)
    return None


def parse_legs(raw: dict[str, Any]) -> list[Leg]:
    legs = []
    for route in (raw.get("details") or {}).get("routes") or []:
        dep = _time((route.get("departure") or {}).get("time"))
        arr = _time((route.get("arrival") or {}).get("time"))
        start = (route.get("startStation") or {}).get("name")
        end = (route.get("destionationStation") or {}).get("name")
        if dep is None or arr is None or not start or not end:
            continue
        train = route.get("trainDetails") or {}
        kind = str(((train.get("trainKind") or {}).get("name")) or "").strip()
        kind = KINDS.get(kind.lower(), kind)
        number = str(train.get("trainNumber") or "").strip()
        line = " ".join(p for p in (kind, number) if p) or str(raw.get("name") or "vlak")
        bus = "bus" in kind.lower() and "s-bahn" not in kind.lower()
        legs.append(Leg(origin=str(start), destination=str(end), departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN, carrier="MÁV", line=line,
                        stops=[Stop(name=str(start), departure=dep), Stop(name=str(end), arrival=arr)]))
    return legs


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
