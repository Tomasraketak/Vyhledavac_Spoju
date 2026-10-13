"""SNCF – francouzské vlaky (TGV INOUI, OUIGO, TER, Intercités, Lyria) přes prodejce Kombo.

Přímo od SNCF to nejde: sncf-connect.com i Trainline (thetrainline.com)
chrání hledání spojů DataDome – z datacentra vrací captchu „t=bv“
(zablokovaný návštěvník) i skutečnému prohlížeči. **Kombo** (kombo.co) je
oficiální prodejce jízdenek SNCF (a Trenitalia France, OUIGO, autobusů)
a jeho web volá API bez captchy:

* ``GET place.kombo.co/search?term=…&locale=en`` – města
  (``i`` = id města, ``c`` = země, ``st`` = nádraží);
* ``POST search.kombo.co/search`` ``{"departureCityId", "arrivalCityId",
  "dateOutward", "passengers": [{"ageGroup": …}], "currency": "EUR"}`` →
  ``key`` hledání;
* ``GET search.kombo.co/search/trips?key=…&lastIndex=…`` – spoje přibývají,
  dokud ``percentageDone`` není 100; ``newLastIndex`` říká, odkud číst příště.
  ``dependencies`` obsahuje nádraží a dopravce (``companies``) podle id.

Spoj (``trip``) má ``subTrips`` (jednotlivé jízdenky), každá ``segments``
(vlak s číslem – „TGV INOUI 6611“, nádraží, časy) a ``bestPrice``
v centech **za celou skupinu**.

Na co si dát pozor:

* **Dvě ceny.** ``subTrips[].bestPrice`` je cena dopravce (stejná jako
  na sncf-connect.com), ``trip.bestPrice`` je cena u Komba včetně jeho
  poplatku (~18 %). Jako cenu spoje bereme cenu dopravce, cenu u Komba
  píšeme do poznámky.
* Cestující jsou věkové skupiny: ``Infant`` 0–3 (na klíně, zdarma),
  ``Child`` 4–11 (u TGV INOUI −50 %), ``Young`` 12–27, ``Adult``,
  ``Senior`` 60+. Ověřeno: dva dospělí = dvojnásobek, dospělý + dítě na
  TGV INOUI = 1,5násobek.
* **Časy mají „Z“, ale jsou místní** (TGV 6611 odjíždí z Paříže v 11:00,
  Kombo píše „11:00:00.000Z“). Zónu bereme z města nádraží
  (``dependencies.cities[].timeZone``).
* Hledá se mezi **městy** (Paříž = všechna nádraží). Kombo vrací i autobusy
  (FlixBus, BlaBlaCar) – ty zahazujeme, autobusy máme od jiných dopravců;
  necháváme spoje s aspoň jedním vlakem (i Frecciarossa Paříž – Lyon).
* Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Francii.

Srovnávač virail.com ukazuje stejná data (od Komba), ale po několika
hledáních vrací rate limit 429 – proto Kombo napřímo.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

WEB = "https://www.kombo.co"
PLACE = "https://place.kombo.co"
SEARCH = "https://search.kombo.co"
PLACE_TTL = 30 * 24 * 3600
POLL_EVERY = 1.5
#: Jak dlouho nejvýš čekat, než Kombo dohledá všechny dopravce (s).
POLL_LIMIT = 45.0
FRANCE = "France"


@PROVIDERS.register()
class SncfProvider(HttpProvider):
    name = "SNCF"
    label = "SNCF – vlaky ve Francii (TGV INOUI, OUIGO, TER, Lyria) přes prodejce Kombo"
    multi_passenger = True
    currency = "EUR"
    countries = ("FR", "BE", "CH", "LU", "DE", "IT", "ES")
    home = ("FR",)
    international_only = True
    hack_legs = False
    referer = WEB + "/"
    min_interval = 0.3
    min_timeout = POLL_LIMIT + 30

    async def _headers(self) -> dict[str, str]:
        return {"Origin": WEB, "Accept": "application/json"}

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"kombo-place:{normalize(place)}"   # „place:“ měla stará verze přes Virail
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self.client.get_json(f"{PLACE}/search", params={"term": place, "locale": "en"},
                                          referer=self.referer, headers=await self._headers())
        items = [r for r in (data or {}).get("results") or [] if r.get("i")]
        if not items:
            raise StationNotFoundError(self.name, f"místo {place!r} Kombo nezná")
        chosen = pick_city(place, items)
        out = {"id": int(chosen["i"]), "name": chosen.get("n") or place, "country": chosen.get("c") or ""}
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "FR" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Francii")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if FRANCE not in (origin["country"], destination["country"]):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Francii")
        date = query.departure.astimezone(DEFAULT_TZ).date().isoformat()
        started = await self.client.post_json(f"{SEARCH}/search", {
            "departureCityId": origin["id"], "arrivalCityId": destination["id"], "dateOutward": date,
            "locale": "en", "passengers": passengers(query.passengers),
            "departureStationId": 0, "arrivalStationId": 0, "currency": "EUR"},
            referer=self.referer, headers=await self._headers())
        key = (started or {}).get("key")
        if not key:
            raise TransientProviderError(self.name, "Kombo nespustilo hledání")
        link = (f"{WEB}/en/app/outward/1/{origin['id']}/{destination['id']}/{date}/"
                f"{quote(origin['name'])}/{quote(destination['name'])}/0/0")
        return await self._poll(key, query, link)

    async def _poll(self, key: str, query: SearchQuery, link: str) -> list[Journey]:
        trips: list[dict[str, Any]] = []
        deps: dict[str, dict[str, Any]] = {}
        last, begun = 0, time.monotonic()
        while True:
            data = await self.client.get_json(f"{SEARCH}/search/trips", params={"key": key, "lastIndex": last},
                                              referer=self.referer, headers=await self._headers()) or {}
            trips += data.get("trips") or []
            for kind, values in (data.get("dependencies") or {}).items():
                deps.setdefault(kind, {}).update(values or {})
            last = data.get("newLastIndex", last)
            journeys = self.journeys({"trips": trips, "dependencies": deps}, query, link)
            done = data.get("percentageDone") == 100
            if done or time.monotonic() - begun > POLL_LIMIT:
                if not done:
                    self.note("Kombo nedohledalo všechny dopravce včas – výsledky můžou být neúplné.")
                return journeys
            self.publish(journeys)
            await asyncio.sleep(POLL_EVERY)

    def journeys(self, data: dict[str, Any], query: SearchQuery, link: str = WEB) -> list[Journey]:
        deps = data.get("dependencies") or {}
        out = []
        for trip in data.get("trips") or []:
            journey = to_journey(trip, deps, link)
            if journey is None or not query.departure <= journey.departure <= query.window_end:
                continue
            carrier_price, kombo_price = trip_prices(trip)
            if trip.get("isAvailable") is False:
                journey.fare_status = FareStatus.SOLD_OUT
            elif carrier_price is not None:
                journey.fares = [self.make_fare(carrier_price, "EUR", label="SNCF / Kombo",
                                                tariff=", ".join(dict.fromkeys(leg.carrier for leg in journey.legs)))]
                journey.fare_status = FareStatus.PRICED
                if kombo_price is not None and kombo_price != carrier_price:
                    journey.notes.append(f"Cena dopravce (sncf-connect.com). U Komba {kombo_price:.2f} € "
                                         "včetně jeho poplatku.")
            out.append(journey)
        return out


def passengers(group: tuple[Passenger, ...]) -> list[dict[str, Any]]:
    """Cestující pro Kombo podle věku (skupiny SNCF)."""
    out = []
    for p in group:
        if p.age < 4:
            kind = "Infant"
        elif p.age < 12:
            kind = "Child"
        elif p.age < 28:
            kind = "Young"
        elif p.age >= 60:
            kind = "Senior"
        else:
            kind = "Adult"
        out.append({"ageGroup": kind, "discountCards": [], "reducedMobility": False,
                    "hasWheelchair": False, "hasMobilityCard": False, "onLap": kind == "Infant"})
    if out and not any(p["ageGroup"] in ("Adult", "Senior", "Young") for p in out):
        out[0].update(ageGroup="Adult", onLap=False)      # bez dospělého Kombo neprodá
    return out


def pick_city(place: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Město z našeptávače: přesná shoda jména, pak shoda prvního slova, pak nejpodobnější."""
    wanted = normalize(place)
    words = place.replace(",", " ").replace("-", " ").split()
    first = normalize(words[0]) if words else wanted
    for target in (wanted, first):
        for item in items:
            if normalize(str(item.get("n") or "")) == target:
                return item
    idx = best_match(place, [str(i.get("n") or "") for i in items])
    return items[idx if idx is not None else 0]


def to_journey(trip: dict[str, Any], deps: dict[str, dict[str, Any]], link: str) -> Journey | None:
    stations, companies = deps.get("stations") or {}, deps.get("companies") or {}
    cities = deps.get("cities") or {}

    def zone(station_id: Any) -> str:
        city = (stations.get(str(station_id)) or {}).get("cityId")
        return str((cities.get(str(city)) or {}).get("timeZone") or "Europe/Paris")

    legs = []
    for sub in trip.get("subTrips") or []:
        for seg in sub.get("segments") or []:
            dep = _time(seg.get("departureTime"), zone(seg.get("departureStationId")))
            arr = _time(seg.get("arrivalTime"), zone(seg.get("arrivalStationId")))
            if dep is None or arr is None:
                return None
            company = companies.get(str(seg.get("companyId"))) or {}
            carrier = str(company.get("displayName") or "")
            bus = company.get("transportType") == "bus" or "autocar" in carrier.lower()
            origin = str((stations.get(str(seg.get("departureStationId"))) or {}).get("name") or "")
            destination = str((stations.get(str(seg.get("arrivalStationId"))) or {}).get("name") or "")
            legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                            mode=Mode.BUS if bus else Mode.TRAIN, carrier=carrier or "SNCF",
                            line=train_name(seg.get("transportNumber"), carrier),
                            stops=[Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    if not legs or not any(leg.mode is Mode.TRAIN for leg in legs):
        return None
    return Journey(provider="SNCF", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=link)


def train_name(number: Any, carrier: str) -> str:
    """„TER 17777 operated by SNCF VOYAGEURS 1187“ → „TER 17777“; holé číslo doplní dopravcem."""
    text = str(number or "").split(" operated by ")[0].replace(" N°", "").strip()
    if not text:
        return carrier or "vlak"
    return f"{carrier} {text}" if text.isdigit() and carrier else text


def trip_prices(trip: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """(cena dopravce, cena u Komba s poplatkem) v eurech za celou skupinu."""
    subs = [((s.get("bestPrice") or {}).get("amount")) for s in trip.get("subTrips") or []]
    carrier = sum((Decimal(int(a)) for a in subs), Decimal(0)) / 100 if subs and None not in subs else None
    total = (trip.get("bestPrice") or {}).get("amount")
    kombo = Decimal(int(total)) / 100 if total is not None else None
    return carrier if carrier is not None else kombo, kombo


def _time(value: Any, zone: str = "Europe/Paris") -> dt.datetime | None:
    """Kombo píše místní čas nádraží, ale s „Z“ – to zahodíme a přidáme zónu města."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.rstrip("Z")[:19])
    except ValueError:
        return None
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("Europe/Paris")
    return parsed.replace(tzinfo=tz)
