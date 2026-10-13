"""Trenitalia – e-shop lefrecce.it (Frecciarossa, Intercity, regionální vlaky).

Web volá veřejné JSON API ``www.lefrecce.it/Channels.Website.BFF.WEB/website``:

* ``GET locations/search?name=…`` – stanice; ID začíná kódem země UIC
  (``83…`` = Itálie, ``81…`` = Rakousko), „Roma ( Tutte Le Stazioni )“ je
  skupina všech nádraží města;
* ``POST ticket/solutions`` – spoje s cenou nejlevnější nabídky pro celou
  skupinu (``adults`` + ``children``).

Na co si dát pozor:

* **Mezinárodní ceny nejsou spolehlivé.** U spojů do zahraničí (Benátky →
  Vídeň) vrací buď ``indicative: true`` (orientační cena), nebo cenu jen za
  italskou část trasy (36 € za tři lidi do Vídně = jen Benátky → Udine).
  Taková cena se proto u spoje neukáže – zůstane jízdní řád a poznámka.
* Děti 4–14 let jsou „children“, do 4 let jezdí zdarma bez místa (do dotazu
  se nepočítají). Slevové karty (CartaFreccia) neuplatňujeme.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Itálii.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
WEB = "https://www.lefrecce.it/"
PLACE_TTL = 30 * 24 * 3600
ITALY = "83"


@PROVIDERS.register()
class TrenitaliaProvider(HttpProvider):
    name = "Trenitalia"
    label = "Trenitalia (jen cesty z/do Itálie; ceny jen po Itálii)"
    multi_passenger = True
    currency = "EUR"
    countries = ("IT", "AT", "CH", "FR", "DE", "SI")
    home = ("IT",)
    international_only = True
    referer = WEB
    min_interval = 0.4

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{normalize(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self.client.get_json(f"{API}/locations/search", params={"name": place, "limit": 10},
                                          referer=WEB)
        items = [p for p in data or [] if p.get("id")]
        if not items and "," in place:
            data = await self.client.get_json(f"{API}/locations/search",
                                              params={"name": place.split(",")[0], "limit": 10}, referer=WEB)
            items = [p for p in data or [] if p.get("id")]
        if not items:
            raise StationNotFoundError(self.name, f"stanici {place!r} Trenitalia nezná")
        chosen = pick_location(place, items)
        self.cache.set(self.name, key, chosen, ttl=PLACE_TTL)
        return chosen

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "IT" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Itálii")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        italian = [str(p["id"]).startswith(ITALY) for p in (origin, destination)]
        if not any(italian):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Itálii")
        adults = sum(1 for p in query.passengers if p.age >= 15)
        children = sum(1 for p in query.passengers if 4 <= p.age < 15)
        if adults + children == 0:
            adults = 1
        when = query.departure.astimezone(DEFAULT_TZ).strftime("%Y-%m-%dT%H:%M:00.000")
        data = await self.client.post_json(f"{API}/ticket/solutions", {
            "departureLocationId": origin["id"], "arrivalLocationId": destination["id"],
            "departureTime": when, "adults": adults, "children": children,
            "criteria": {"frecceOnly": False, "regionalOnly": False, "noChanges": False,
                         "order": "DEPARTURE_DATE", "limit": 10, "offset": 0},
            "advancedSearchRequest": {"bestFare": False}}, referer=WEB)
        international = not all(italian)
        journeys = []
        for item in (data or {}).get("solutions") or []:
            journey = self.to_journey(item.get("solution") or {}, international)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def to_journey(self, solution: dict[str, Any], international: bool) -> Journey | None:
        legs = parse_legs(solution)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        price = solution.get("price") or {}
        status = str(solution.get("status") or "")
        if status and status != "SALEABLE":
            journey.fare_status = FareStatus.SOLD_OUT if "SOLD" in status else FareStatus.NOT_BOOKABLE
            return journey
        if price.get("amount") is None:
            return journey
        amount = Decimal(str(price["amount"]))
        if international or price.get("indicative"):
            # orientační cena, nebo jen italská část trasy – jako cenu spoje ji nebrat
            journey.notes.append(f"Trenitalia uvádí {amount:.2f} € jen orientačně nebo za italskou část "
                                 "trasy – celou jízdenku ověřte u dopravce.")
            journey.raw = {"indicative_eur": str(amount)}
            return journey
        journey.fares = [self.make_fare(amount, "EUR", label="Trenitalia",
                                        tariff=", ".join(dict.fromkeys(str(t.get("trainCategory") or "")
                                                                       for t in solution.get("trains") or [])))]
        journey.fare_status = FareStatus.PRICED
        return journey


def pick_location(place: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Skupina „Tutte Le Stazioni“ / „(city)“ pro město, jinak nejpodobnější stanice."""
    wanted = normalize(place)
    for item in items:
        name = normalize(str(item.get("name") or ""))
        if name == wanted:
            return item
    for item in items:
        if item.get("multistation") and normalize(str(item.get("name") or "")).split(" ")[0] == wanted.split(" ")[0]:
            return item
    idx = best_match(place, [str(i.get("name") or "") for i in items])
    return items[idx if idx is not None else 0]


def parse_legs(solution: dict[str, Any]) -> list[Leg]:
    legs = []
    for node in solution.get("nodes") or []:
        dep, arr = _time(node.get("departureTime")), _time(node.get("arrivalTime"))
        if dep is None or arr is None:
            continue
        train = node.get("train") or {}
        acronym = str(train.get("acronym") or "").strip()
        number = str(train.get("name") or "").strip()
        category = str(train.get("trainCategory") or "")
        line = " ".join(p for p in (acronym, number) if p) or category or "vlak"
        bus = "bus" in category.lower() or acronym.upper() in ("BU", "BUS")
        origin, destination = str(node.get("origin") or ""), str(node.get("destination") or "")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN, carrier=category or "Trenitalia", line=line,
                        stops=[Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    return legs


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
