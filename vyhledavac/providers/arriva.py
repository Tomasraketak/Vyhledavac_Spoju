"""Arriva – e-shop shop.arriva.cz (vlaky Arriva, Arriva Express, jízdenky SJT).

E-shop Arrivy volá JSON API ``api.arriva.cz/v1`` – stejné voláme my:

1. ``GET shop.arriva.cz/api/trigger`` nastaví cookie ``ArrticCookie``
   s anonymním tokenem (JWT, platí měsíce), posílá se jako ``Bearer``;
2. ``GET /v1/Place/Search?keyword=`` -> stanice (typ 1) a zastávky (typ 2);
3. ``POST /v1/Route/Search`` -> spoje (i s přestupy a všemi zastávkami);
4. ``POST /v1/Price/CalculateBySearch`` -> cena pro skupinu podle kategorií.

Arriva prodává jízdenky na své vlaky (R Praha – Liberec, Praha – Kladno …),
expresní autobusy (Praha – Most – Litvínov, Brno – Olomouc …) a také
jízdenky **SJT** (Systém jednotného tarifu) – ty platí i ve vlacích jiných
dopravců (ČD) a bývají levnější než jízdenka ČD. U takových spojů je jako
dopravce uvedeno „jiný dopravce (SJT)“.

Úseky se neslučují: změna čísla vlaku (R 653 -> R 96653) bývá náhradní
doprava, tedy skutečný přestup.

Obec se hledá dvakrát: jednou mezi nádražími a jednou mezi autobusovými
zastávkami (Arriva nemá „celé město“ jako jedno místo).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import urllib.parse
from decimal import Decimal
from typing import Any

from ..errors import StationNotFoundError, TransientProviderError
from ..models import (
    DEFAULT_TZ,
    FareStatus,
    Journey,
    Leg,
    Mode,
    Passenger,
    SearchQuery,
    Stop,
)
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://api.arriva.cz/v1"
SHOP = "https://shop.arriva.cz/"
TRIGGER = "https://shop.arriva.cz/api/trigger"
TOKEN_TTL = 7 * 24 * 3600
PLACE_TTL = 7 * 24 * 3600
TRAIN, BUS = 1, 2
#: Dopravci podle ``carrierId`` (``/v1/Constants/AllCompanies``).
CARRIERS = {1: "Arriva", 2: "Arriva", 3: "Arriva", 4: "Arriva City", 5: "Arriva vlaky",
            6: "ČSAD MHD Kladno", 7: "RegioJet", 17: "Arriva autobusy", 27: "Arriva City"}
#: Kategorie cestujících (``/v1/Constants/AllAbstractPassengerCategories``).
ADULT, INFANT, CHILD, STUDENT, SENIOR = 1, 2, 3, 4, 5
ONEWAY = 1
CZK = 1


def category(passenger: Passenger) -> int:
    """Kategorie Arrivy: dítě do 6, dítě 6–18, student 18–26, senior 65+."""
    if passenger.age < 6:
        return INFANT
    if passenger.age < 18:
        return CHILD
    if passenger.age >= 65:
        return SENIOR
    if passenger.is_student:
        return STUDENT
    return ADULT


@PROVIDERS.register()
class ArrivaProvider(HttpProvider):
    name = "Arriva"
    label = "Arriva (vlaky Arriva, Arriva Express, jízdenky SJT)"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ",)
    referer = SHOP
    min_interval = 0.2

    # ---- token a místa -------------------------------------------------------
    async def _token(self) -> str:
        cached = self.cache.get(self.name, "token")
        if cached:
            return str(cached)
        response = await self.client.request("GET", TRIGGER, referer=SHOP, expect_json=False)
        token = response.cookies.get("ArrticCookie")
        if not token and self.client._client is not None:
            token = self.client._client.cookies.get("ArrticCookie")
        if not token:
            raise TransientProviderError(self.name, "e-shop nevydal přístupový token")
        self.cache.set(self.name, "token", token, ttl=TOKEN_TTL)
        return token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}", "Origin": SHOP.rstrip("/")}

    async def places(self, name: str) -> list[dict[str, Any]]:
        key = f"places:{normalize(name)}"
        cached = self.cache.get(self.name, key)
        if cached is not None:
            return cached
        data = await self.client.get_json(f"{API}/Place/Search", params={"keyword": name},
                                          referer=SHOP, headers=await self._headers())
        places = [p for p in data or [] if isinstance(p, dict) and p.get("id")]
        self.cache.set(self.name, key, places, ttl=PLACE_TTL)
        return places

    async def candidates(self, name: str) -> dict[int, dict[str, Any]]:
        """Nejlepší nádraží a nejlepší autobusová zastávka pro zadané místo."""
        found = pick_places(name, await self.places(name))
        town = name.split(",")[0].strip()
        if not found and town and town != name:      # zastávku nezná – zkusíme obec
            found = pick_places(town, await self.places(town))
        if not found:
            raise StationNotFoundError(self.name, f"místo {name!r} Arriva nezná")
        return found

    # ---- hledání ---------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        origins, destinations = await asyncio.gather(
            self.candidates(query.origin), self.candidates(query.destination))
        pairs = [(origins[kind], destinations[kind]) for kind in (TRAIN, BUS)
                 if kind in origins and kind in destinations]
        if not pairs:   # vlak z jedné strany, autobus z druhé – zkusíme, co je
            pairs = [(next(iter(origins.values())), next(iter(destinations.values())))]
        results = await asyncio.gather(*(self._search_pair(query, a, b) for a, b in pairs),
                                       return_exceptions=True)
        journeys: list[Journey] = []
        errors = []
        for outcome in results:
            if isinstance(outcome, Exception):
                errors.append(outcome)
            else:
                journeys.extend(outcome)
        if not journeys and errors:
            if all(isinstance(e, TransientProviderError) and "500" in e.message for e in errors):
                raise StationNotFoundError(self.name, "spojení nenalezeno")
            raise errors[0]
        # nádraží i zastávka mohou vrátit tentýž autobus – stačí jednou (levnější)
        seen, unique = set(), []
        for journey in sorted(journeys, key=lambda j: (j.departure, j.price or 0)):
            key = (journey.departure, journey.legs[0].line)
            if key not in seen:
                seen.add(key)
                unique.append(journey)
        return unique

    async def _search_pair(self, query: SearchQuery, origin: dict[str, Any],
                           destination: dict[str, Any]) -> list[Journey]:
        when = query.departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        data = await self.client.post_json(f"{API}/Route/Search", {
            "source": {"type": "arriva", "number": origin["id"]},
            "target": {"type": "arriva", "number": destination["id"]},
            "dateTime": when, "maxTransfers": 3, "isDeparture": True,
        }, referer=SHOP, headers=await self._headers())
        link = shop_link(origin["id"], destination["id"], query)
        journeys = []
        for raw in data or []:
            journey = to_journey(raw, link)
            if journey is not None and journey.departure <= query.window_end:
                journeys.append(journey)
        await asyncio.gather(*(self._price(j, query) for j in journeys))
        return journeys

    async def _price(self, journey: Journey, query: SearchQuery) -> None:
        counts: dict[int, int] = {}
        for passenger in query.passengers:
            counts[category(passenger)] = counts.get(category(passenger), 0) + 1
        body = {
            "currencyId": CZK, "abstractFareVariantId": ONEWAY,
            "requestedDateTime": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "passengers": [{"abstractPassengerCategoryId": k, "quantity": n} for k, n in counts.items()],
            "searchId": journey.journey_id,
        }
        try:
            data = await self.client.post_json(f"{API}/Price/CalculateBySearch", body,
                                               referer=SHOP, headers=await self._headers())
        except TransientProviderError as exc:
            log.debug("Arriva: cena %s: %s", journey.journey_id, exc.message)
            return
        total, tariff = parse_price(data)
        if total is None:
            return
        journey.fares = [self.make_fare(total, "CZK", label="Arriva", tariff=tariff)]
        journey.fare_status = FareStatus.PRICED


# ---- pomůcky ---------------------------------------------------------------------
TARIFFS = {1: "Tarif ARRIVA vlaky", 4: "SJT (Systém jednotného tarifu)", 12: "Tarif Arriva Express"}


def pick_places(name: str, places: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Ze seznamu míst vybere nejlepší nádraží a nejlepší zastávku.

    Přesná shoda názvu vyhrává (a pak se hledá jen v jejím typu). Jinak:
    nádraží „Obec“ / „Obec hlavní nádraží“ a zastávka „Obec,,…“ s nejvyšší
    prioritou (Arriva ji dává nejvytíženějším zastávkám).
    """
    wanted = normalize(name)
    exact = [p for p in places if normalize(p.get("name") or "") == wanted
             or normalize(p.get("shortName") or "") == wanted]
    out: dict[int, dict[str, Any]] = {}
    if exact:
        best = exact[0]
        # konkrétní zastávka („Most,,1.nám.“) nebo nádraží („Praha-Smíchov“) – jen ono;
        # nádraží jménem obce („Most“) – k němu ještě hlavní autobusová zastávka
        if best.get("typeId") != TRAIN or "," in wanted or "-" in wanted or " " in wanted:
            return {best.get("typeId") or TRAIN: best}
        out[TRAIN] = best
    city = wanted.split(",")[0].strip()
    trains = [p for p in places if p.get("typeId") == TRAIN]
    if TRAIN not in out and trains:
        def rank(p: dict[str, Any]) -> tuple[int, int]:
            n = normalize(p.get("name") or "")
            main = n in (city, f"{city} hlavni nadrazi", f"{city} hl n")
            return (0 if main else 1 if n.startswith(city) else 2, -int(p.get("priority") or 0))
        top = sorted(trains, key=rank)[0]
        if normalize(top.get("name") or "").startswith(city):
            out[TRAIN] = top
    buses = [p for p in places if p.get("typeId") == BUS
             and normalize(str(p.get("name") or "").split(",")[0]) == city]
    if buses:
        out[BUS] = max(buses, key=lambda p: int(p.get("priority") or 0))
    if not out and places:
        idx = best_match(name, [str(p.get("name") or "") for p in places], threshold=0.6)
        if idx is not None:
            out[places[idx].get("typeId") or TRAIN] = places[idx]
    return out


def to_journey(raw: dict[str, Any], link: str) -> Journey | None:
    legs = []
    for part in raw.get("routes") or []:
        departure = _time(part.get("sourceStationDepartureTime"))
        arrival = _time(part.get("targetStationArrivalTime"))
        if departure is None or arrival is None:
            continue
        line = part.get("line") or {}
        prefix = str(line.get("prefix") or "")
        stations = [s for s in part.get("stations") or [] if s.get("isOnRoute", True)]
        stops = [Stop(name=_name(s.get("name")), arrival=_time(s.get("arrivalTime")),
                      departure=_time(s.get("departureTime")), platform=s.get("platform") or None)
                 for s in stations]
        carrier = CARRIERS.get(line.get("carrierId"), "jiný dopravce (SJT)")
        is_bus = prefix.lower() == "bus"
        label = (f"Bus {line.get('number')}" if is_bus
                 else f"{prefix} {line.get('number')}".strip())
        mode = Mode.BUS if is_bus else Mode.TRAIN
        legs.append(Leg(
            origin=stops[0].name if stops else "", destination=stops[-1].name if stops else "",
            departure=departure, arrival=arrival, mode=mode,
            carrier=carrier, line=label, stops=stops,
        ))
    if not legs or not raw.get("searchId"):
        return None
    return Journey(provider="Arriva", legs=legs, fare_status=FareStatus.UNKNOWN,
                   journey_id=str(raw["searchId"]), deep_link=link)


def parse_price(data: Any) -> tuple[Decimal | None, str]:
    """Součet cen všech úseků a cestujících (+ místenky)."""
    parts = (data or {}).get("prices") if isinstance(data, dict) else None
    if not parts:
        return None, ""
    total = Decimal(0)
    tariffs = []
    for part in parts:
        for price in part.get("prices") or []:
            # ceny jsou už za všechny cestující dané kategorie (2 dospělí = 2× cena)
            total += Decimal(str(price.get("passengerPrice") or 0)) + Decimal(
                str(price.get("reservationPrice") or 0))
        name = TARIFFS.get(part.get("tariffId"))
        if name and name not in tariffs:
            tariffs.append(name)
    return total, " + ".join(tariffs)


def shop_link(origin: int, destination: int, query: SearchQuery) -> str:
    when = query.departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    params = {"sourcePlaceId": origin, "targetPlaceId": destination, "dateTime": when,
              "isOneWay": "true", "lang": "cs", "Adult": len(query.passengers)}
    return f"{SHOP}reservation/results?{urllib.parse.urlencode(params)}"


def _name(value: Any) -> str:
    """„Most,,1.nám.“ -> „Most, 1.nám.“ (formát CIS JŘ)."""
    parts = [p.strip() for p in str(value or "").split(",")]
    return ", ".join(p for p in parts if p)


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(DEFAULT_TZ) if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
