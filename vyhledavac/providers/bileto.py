"""Bileto – prodejní portál autobusových dopravců (bileto.cz).

Na Biletu prodávají jízdenky menší autobusoví dopravci, kteří jinde online
nejsou: např. linka Lipno – České Budějovice – Třeboň – Brno – Olomouc
(RETROBUS Line), České Budějovice – Brno (Dinarica) a další dálkové
a regionální linky. Web bileto.cz volá veřejné JSON API
``api.bileto.com/v1`` – to samé voláme my:

1. ``POST /oauth/auth`` (client_credentials, údaje portálu bileto.cz z jeho
   vlastního skriptu) -> token, posílá se v hlavičce ``Authorization``;
2. ``GET /places?Mask=`` -> město nebo zastávka;
3. ``GET /connections`` -> spoje (``Type=DepartureSince``);
4. ``POST /passengers`` (věk, karty) a ``POST /prices`` -> cena spoje pro
   celou skupinu.

Ptáme se jen na cesty po Česku (oba konce v ČR); které linky a zastávky
Bileto má, se nedá rozumně vypsat předem – proto se
ptáme pokaždé; když Bileto místo nezná, je to „nejezdí tam“.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import (
    DEFAULT_TZ,
    DiscountCard,
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

API = "https://api.bileto.com/v1"
AUTH_URL = f"{API}/oauth/auth"
# Veřejné údaje portálu bileto.cz (jsou v jeho JavaScriptu, ne tajemství).
CLIENT_ID = "84fc4010-e882-4f14-b1d8-01e12ed5e652:7d979075-3adc-4cea-bbf9-87f9f1522eef"
CLIENT_SECRET = "342b9677-10f6-4488-9dbb-ac3ba8d02167"
#: Karta „student 18–26 ISIC“ v číselníku portálu.
ISIC_CARD = "75fc3519-db14-4a1c-941e-b27e0ebc8f7c"
SECOND_CLASS = 11
PLACE_TTL = 30 * 24 * 3600
TOKEN_TTL = 24 * 3600
WEB = "https://bileto.cz/"
RESULTS = 10


@PROVIDERS.register()
class BiletoProvider(HttpProvider):
    name = "Bileto"
    label = "Bileto (menší autobusoví dopravci: RETROBUS, Dinarica …)"
    multi_passenger = True
    currency = "CZK"
    countries = ("CZ",)
    referer = WEB
    min_interval = 0.2

    # ---- přihlášení a místa ------------------------------------------------
    async def _token(self) -> str:
        cached = self.cache.get(self.name, "token")
        if cached:
            return str(cached)
        data = await self.client.post_json(AUTH_URL, {
            "grant_type": "client_credentials", "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET}, referer=WEB)
        token = str((data or {}).get("access_token") or "")
        if not token:
            raise TransientProviderError(self.name, "nepodařilo se získat token")
        self.cache.set(self.name, "token", token, ttl=TOKEN_TTL)
        return token

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        token = await self._token()
        return await self.client.get_json(f"{API}{path}", params=params, referer=WEB,
                                          headers={"Authorization": token})

    async def _post(self, path: str, body: Any) -> Any:
        token = await self._token()
        return await self.client.post_json(f"{API}{path}", body, referer=WEB,
                                           headers={"Authorization": token})

    async def resolve(self, place: str) -> tuple[str, str]:
        """Místo (město nebo zastávka) v Biletu: ``(id, název)``."""
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return tuple(cached)  # type: ignore[return-value]
        data = await self._get("/places", {"Mask": place})
        result = pick_place(place, data) if isinstance(data, list) and data else None
        town = place.split(",")[0].strip()
        if result is None and town and town != place:   # zastávku nezná – zkusíme obec
            data = await self._get("/places", {"Mask": town})
            result = pick_place(town, data) if isinstance(data, list) and data else None
        if result is None:
            raise StationNotFoundError(self.name, f"místo {place!r} Bileto nezná")
        self.cache.set(self.name, key, list(result), ttl=PLACE_TTL)
        return result

    # ---- hledání -------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        if geo.leaves_czechia(query.origin, query.destination):
            raise StationNotFoundError(self.name, "ptáme se jen na cesty po Česku")
        (from_id, _), (to_id, _) = await asyncio.gather(
            self.resolve(query.origin), self.resolve(query.destination))
        when = query.departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        data = await self._get("/connections", {
            "ResultsCount": RESULTS, "From": from_id, "To": to_id,
            "Datetime": when, "Type": "DepartureSince"})
        connections = (data or {}).get("Data") if isinstance(data, dict) else data
        journeys = []
        for raw in connections or []:
            journey = self._to_journey(raw, query, from_id, to_id)
            if journey is not None and journey.departure <= query.window_end:
                journeys.append(journey)
        if journeys:
            await self._price(journeys, query)
        return journeys

    async def _price(self, journeys: list[Journey], query: SearchQuery) -> None:
        """Ceny pro celou skupinu – Bileto chce cestující nejdřív založit."""
        try:
            passengers = []
            for i, passenger in enumerate(query.passengers):
                body = passenger_body(passenger, i)
                created = await self._post("/passengers", body)
                passengers.append({**body, "Id": (created or {}).get("Id")})
        except TransientProviderError as exc:
            self.note(f"ceny se nepodařilo zjistit ({exc.message})")
            return

        async def one(journey: Journey) -> None:
            try:
                data = await self._post("/prices", {
                    "Currency": query.currency, "CategoryGroups": [SECOND_CLASS],
                    "Items": [{"ItemType": "connection", "ConnectionId": journey.journey_id,
                               "ItemData": None}],
                    "Passengers": passengers, "ParentOrderItemId": None})
            except TransientProviderError as exc:
                log.debug("Bileto: cena spoje %s: %s", journey.journey_id, exc.message)
                return
            price, currency, seats = parse_price(data)
            if seats == 0:
                journey.fare_status = FareStatus.SOLD_OUT
                return
            if price is None:
                return
            journey.fares = [self.make_fare(price, currency or query.currency, label="Bileto",
                                            tariff=_tariff_label(query.passengers),
                                            seats_left=seats)]
            journey.fare_status = FareStatus.PRICED

        await asyncio.gather(*(one(j) for j in journeys))

    def _to_journey(self, raw: dict[str, Any], query: SearchQuery, from_id: str,
                    to_id: str) -> Journey | None:
        legs = []
        for part in raw.get("Legs") or []:
            if part.get("LegType") not in (None, "TransitLeg"):
                continue                  # přesun pěšky mezi zastávkami
            departure, arrival = _time(part.get("DepartureAt")), _time(part.get("ArrivalAt"))
            if departure is None or arrival is None:
                continue
            origin = ((part.get("DepartureStation") or {}).get("Name") or "").strip()
            destination = ((part.get("ArrivalStation") or {}).get("Name") or "").strip()
            trip = part.get("Trip") or {}
            route = trip.get("Route") or {}
            operator = (trip.get("Operator") or {}).get("Name") or ""
            kinds = (part.get("DepartureStation") or {}).get("Type") or ["bus"]
            legs.append(Leg(
                origin=origin, destination=destination, departure=departure, arrival=arrival,
                mode=Mode.TRAIN if "train" in kinds else Mode.BUS, carrier=operator or "Bileto",
                line=str(route.get("FullName") or route.get("Name") or ""),
                stops=[Stop(name=origin, departure=departure), Stop(name=destination, arrival=arrival)],
            ))
        if not legs:
            return None
        date = query.departure.date().isoformat()
        return Journey(
            provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
            journey_id=str(raw.get("Id") or ""),
            deep_link=f"{WEB}#/search-results?from={from_id}&to={to_id}&date={date}",
            raw={"connection": raw.get("Id")},
        )


# ---- pomůcky ---------------------------------------------------------------------
def pick_place(name: str, places: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Nejlepší shoda; při stejném názvu má přednost město před zastávkou."""
    names = [str(p.get("Name") or "") for p in places]
    wanted = normalize(name)
    exact = [p for p, n in zip(places, names) if normalize(n) == wanted]
    if exact:
        exact.sort(key=lambda p: p.get("Type") != "city")
        chosen = exact[0]
    else:
        idx = best_match(name, names, threshold=0.5)
        if idx is None:
            return None
        chosen = places[idx]
    if not chosen.get("Id"):
        return None
    return str(chosen["Id"]), str(chosen.get("Name") or name)


def passenger_body(passenger: Passenger, sequence: int) -> dict[str, Any]:
    cards = [ISIC_CARD] if passenger.is_student or (
        passenger.has(DiscountCard.ISIC) and 18 <= passenger.age < 27) else []
    return {"Age": passenger.age, "Cards": cards, "Sequence": sequence}


def parse_price(data: Any) -> tuple[Decimal | None, str | None, int | None]:
    """``POST /prices`` -> (cena za skupinu, měna, volná místa)."""
    if not isinstance(data, list) or not data:
        return None, None, None
    item = data[0] or {}
    price = ((item.get("Prices") or {}).get("userPrice") or {})
    amount = price.get("amount")
    if amount is None:
        return None, None, None
    seats = item.get("FreeSeatsCount")
    return Decimal(str(amount)), price.get("currency"), seats if isinstance(seats, int) else None


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)


def _tariff_label(passengers: tuple[Passenger, ...]) -> str:
    names = {"infant": "dítě do 6", "child": "dítě", "youth": "mládež",
             "student": "student", "adult": "dospělý", "senior": "senior"}
    return ", ".join(names[p.category.value] for p in passengers)
