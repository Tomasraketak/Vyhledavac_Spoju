"""Eurostar (eurostar.com) – Londýn, Paříž, Brusel, Amsterdam, Kolín (i bývalý Thalys).

Web www.eurostar.com/search volá veřejné GraphQL API
``POST https://site-api.eurostar.com/gateway`` (bez přihlášení a bez klíče,
stačí ``Content-Type: application/json``):

* ``query getStations`` – všechny stanice, které Eurostar prodává (asi 240):
  ``uic``, ``name`` (anglicky: „Brussels Midi / Zuid“, „Cologne Hbf“),
  ``city``, ``countryCode`` (GBR, FRA, BEL, NLD, DEU, CHE). Vlastní vlaky
  Eurostaru mají ``isEurostarDirect`` (ex-Eurostar) nebo ``isErDirect``
  (ex-Thalys); ostatní stanice (Lyon, Antverpy …) jsou přípoje SNCF/SNCB;
* ``query NewBookingSearch`` → ``journeySearch(outboundDate, origin,
  destination, adults, youths, children, seniors, infants, …ages, currency)``
  – všechny spoje jednoho dne. U každého spoje ``fares`` po třídách
  (STANDARD, PLUS, PREMIER) s cenou ``prices.total`` pro celou skupinu
  a úseky ``legs`` (číslo vlaku ``serviceName``, dopravce ``serviceType``,
  místní časy ``departureDate``/``departs``).

Cestující podle věku (pravidla Eurostaru): do 3 let ``infants`` (zdarma, bez
místa), 4–11 ``children``, 12–25 ``youths``, 26–59 ``adults``, od 60
``seniors``; u dětí, mládeže a seniorů se posílá i věk (``childAges`` …).

Na co si dát pozor:

* **Časy jsou místní** – odjezd z Londýna v britském čase, příjezd do Paříže
  ve středoevropském (``isoDate`` s „Z“ na konci lže, není to UTC). Časové
  pásmo bereme podle UIC kódu stanice (70… = Velká Británie).
* API vrací celý den bez ohledu na hodinu; okno hledání filtrujeme samy
  a přes půlnoc se ptáme i na další den.
* Ceny chceme v EUR: v GBP API u některých přípojů (Londýn → Antverpy) cenu
  nevrátí vůbec. Cena v GBP na britském webu může vyjít o pár procent jinak.
* Vyprodaná třída má ``prices: null`` a ``seats: 0``; spoj bez jediné ceny
  je vyprodaný. Speciální „třídy“ WC_COMP, EVAC, UNASSIGNED (vozíčkáři)
  ignorujeme.
* U přípojů (Londýn → Lyon přes Paříž) je ``isSummatedFare`` – cena je
  součet dvou jízdenek, ale Eurostar ji prodává jako celek, takže platí.
* Stejný odjezd může přijít dvakrát s jiným číslem vlaku (spojené soupravy
  9339/9439 Brusel → Amsterdam) – jsou to dva prodejní spoje.

Ptáme se jen tehdy, když oba konce cesty mohou ležet v síti Eurostaru
(GB, FR, BE, NL, DE – jen Porýní, CH jako přípoj).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, CentralEuropeanTime, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://site-api.eurostar.com/gateway"
WEB = "https://www.eurostar.com/"
SEARCH_WEB = "https://www.eurostar.com/search/uk-en"
STATIONS_TTL = 7 * 24 * 3600
#: Země, kam Eurostar jezdí nebo kam prodává přípoj.
SERVED = {"GB", "FR", "BE", "NL", "DE", "CH"}
#: Německá města s vlakem Eurostaru (jinak se na Německo neptáme).
GERMAN_WORDS = ("koln", "koeln", "cologne", "kolin nad rynem", "aachen", "cachy", "dusseldorf",
                "duisburg", "essen", "dortmund")
COUNTRY = {"GBR": "GB", "FRA": "FR", "BEL": "BE", "NLD": "NL", "DEU": "DE", "CHE": "CH"}
#: Třídy, které nejsou na prodej běžnému cestujícímu (doprovod vozíčkáře apod.).
SKIP_CLASSES = {"WC_COMP", "EVAC", "UNASSIGNED"}
#: České a místní názvy měst -> ``city`` v seznamu stanic Eurostaru.
CITY_ALIASES = {
    "londyn": "london", "londres": "london", "pariz": "paris", "brusel": "brussels",
    "bruxelles": "brussels", "brussel": "brussels", "kolin nad rynem": "cologne", "koln": "cologne",
    "koeln": "cologne", "cachy": "aachen", "antverpy": "antwerp", "antwerpen": "antwerp",
    "lutych": "liege", "dusseldorf": "dusseldorf", "zeneva": "geneve", "geneva": "geneve",
}

STATIONS_QUERY = """query getStations($withDefaultConnections: Boolean) {
  stations(withDefaultConnections: $withDefaultConnections) {
    uic name country countryCode city isEurostarDirect isErDirect
  }
}"""

SEARCH_QUERY = """query NewBookingSearch($origin: String!, $destination: String!, $outbound: String!,
  $currency: Currency!, $adult: Int, $child: Int, $infant: Int, $youth: Int, $senior: Int,
  $childAges: [Int!], $youthAges: [Int!], $seniorsAges: [Int!],
  $productFamilies: [String] = ["PUB"], $contractCode: String = "EIL_ALL",
  $multipleFlexibility: Boolean = true, $hideExternalCarrierTrains: Boolean = true,
  $hideDirectExternalCarrierTrains: Boolean = true) {
  journeySearch(outboundDate: $outbound, origin: $origin, destination: $destination, adults: $adult,
    seniors: $senior, productFamilies: $productFamilies, contractCode: $contractCode, children: $child,
    youths: $youth, infants: $infant, currency: $currency, multipleFlexibility: $multipleFlexibility,
    childAges: $childAges, youthAges: $youthAges, seniorsAges: $seniorsAges) {
    outbound {
      origin { name city uic }
      destination { name city uic }
      journeys(hideIndirectTrainsWhenDisruptedAndCancelled: false, hideDepartedTrains: true,
               hideExternalCarrierTrains: $hideExternalCarrierTrains,
               hideDirectExternalCarrierTrains: $hideDirectExternalCarrierTrains) {
        timing { date departureTime: departs arrivalTime: arrives duration }
        fares {
          isThroughFare isSummatedFare
          class { name code }
          classOfService { name code }
          prices { displayPrice total child adult senior youth }
          seats
          legs {
            origin { name uic city }
            destination { name uic city }
            serviceName
            serviceType { name code brandCode }
            timing { departureTime: departs arrivalTime: arrives departureDate arrivalDate duration }
          }
        }
      }
    }
  }
}"""


class _BritishTime(CentralEuropeanTime):
    """GMT/BST – záloha bez databáze pásem; letní čas začíná i končí ve stejný okamžik jako v EU."""

    _STD = dt.timedelta(0)

    def tzname(self, when: dt.datetime | None) -> str:
        return "BST" if self.dst(when) else "GMT"


def _london() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/London")
    except Exception:
        return _BritishTime()


LONDON_TZ = _london()
CONTINENT_TZ = DEFAULT_TZ        # Paříž, Brusel, Amsterdam, Kolín = stejná pravidla jako Praha


@PROVIDERS.register()
class EurostarProvider(HttpProvider):
    name = "Eurostar"
    label = "Eurostar (Londýn, Paříž, Brusel, Amsterdam, Kolín – i ex-Thalys)"
    multi_passenger = True
    currency = "EUR"
    countries = ("GB", "FR", "BE", "NL", "DE")
    international_only = True
    referer = WEB
    min_interval = 0.4

    async def _gql(self, operation: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        data = await self.client.post_json(API, {"operationName": operation, "variables": variables,
                                                 "query": query},
                                           referer=WEB, headers={"Origin": "https://www.eurostar.com"})
        if not isinstance(data, dict):
            return {}
        if data.get("errors") and not data.get("data"):
            raise TransientProviderError(self.name, f"GraphQL: {data['errors'][0].get('message', '')[:160]}")
        return data.get("data") or {}

    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        data = await self._gql("getStations", STATIONS_QUERY, {"withDefaultConnections": True})
        items = [s for s in data.get("stations") or [] if s.get("uic")]
        if items:
            self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, Any]:
        chosen = pick_station(place, await self.stations())
        if chosen is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} Eurostar neobsluhuje")
        return chosen

    async def search(self, query: SearchQuery) -> list[Journey]:
        reason = skip_reason(query.origin, query.destination)
        if reason:
            raise StationNotFoundError(self.name, reason)
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if origin["uic"] == destination["uic"]:
            raise StationNotFoundError(self.name, "výchozí a cílová stanice je stejná")
        variables = {"origin": origin["uic"], "destination": destination["uic"], "currency": "EUR",
                     **travellers(query.passengers)}
        journeys: dict[tuple, Journey] = {}
        for day in search_days(query):
            data = await self._gql("NewBookingSearch", SEARCH_QUERY, {**variables, "outbound": day.isoformat()})
            bound = ((data.get("journeySearch") or {}).get("outbound") or {})
            for raw in bound.get("journeys") or []:
                journey = self.to_journey(raw, search_link(origin["uic"], destination["uic"], day, query))
                if journey is None or not query.departure <= journey.departure <= query.window_end:
                    continue
                journeys.setdefault(journey.signature(), journey)
        return sorted(journeys.values(), key=lambda j: j.departure)

    def to_journey(self, raw: dict[str, Any], link: str = WEB) -> Journey | None:
        fares = [f for f in raw.get("fares") or []
                 if ((f.get("classOfService") or {}).get("code") or "") not in SKIP_CLASSES]
        legs = next((parse_legs(f) for f in fares if f.get("legs")), [])
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.SOLD_OUT,
                          deep_link=link)
        for fare in fares:
            total = (fare.get("prices") or {}).get("total")
            if not total:
                continue
            cls = fare.get("classOfService") or {}
            tariff = str(cls.get("name") or cls.get("code") or "")
            if fare.get("isSummatedFare"):
                tariff += " (součet jízdenek)"
            seats = fare.get("seats")
            journey.fares.append(self.make_fare(Decimal(str(total)), "EUR", label="Eurostar", tariff=tariff,
                                                seats_left=seats if isinstance(seats, int) and seats else None,
                                                deep_link=journey.deep_link))
        if journey.fares:
            journey.fare_status = FareStatus.PRICED
        else:
            journey.notes.append("Eurostar: spoj je pro tuto skupinu vyprodaný.")
        return journey


# ---- pomocné funkce --------------------------------------------------------------------
def skip_reason(origin: str, destination: str) -> str | None:
    """Důvod, proč se Eurostaru vůbec neptat (bez síťového dotazu), jinak ``None``."""
    for place in (origin, destination):
        country = geo.country_of(place)
        if country is None:
            continue
        if country not in SERVED:
            return "Eurostar jezdí jen mezi Londýnem, Francií, Beneluxem a Porýním"
        if country == "DE":
            n = " " + normalize(place) + " "
            if not any(f" {w} " in n for w in GERMAN_WORDS):
                return "Eurostar jezdí v Německu jen do Kolína, Cách a Porýní"
    known = {geo.country_of(origin), geo.country_of(destination)}
    if None not in known and known <= {"CH"}:
        return "Eurostar ve Švýcarsku nejezdí"
    return None


def pick_station(place: str, stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Stanice podle názvu: přesný název, pak město (vlastní stanice Eurostaru přednostně)."""
    if not stations:
        return None
    wanted = normalize(place)
    wanted = CITY_ALIASES.get(wanted, wanted)
    for s in stations:
        if normalize(str(s.get("name") or "")) == wanted:
            return s
    by_city = [s for s in stations if normalize(str(s.get("city") or "")) == wanted]
    if by_city:
        return max(by_city, key=lambda s: (bool(s.get("isEurostarDirect")), bool(s.get("isErDirect"))))
    first = wanted.split(" ")[0]
    first = CITY_ALIASES.get(first, first)
    names = [str(s.get("name") or "") for s in stations]
    idx = best_match(place, names)
    if idx is not None:
        return stations[idx]
    starts = [s for s, n in zip(stations, names) if normalize(n).split(" ")[0] == first
              or normalize(str(s.get("city") or "")).split(" ")[0] == first]
    if starts:
        return max(starts, key=lambda s: (bool(s.get("isEurostarDirect")), bool(s.get("isErDirect"))))
    return None


def travellers(passengers: tuple[Passenger, ...]) -> dict[str, Any]:
    """Počty a věky cestujících podle kategorií Eurostaru."""
    groups: dict[str, list[int]] = {"infant": [], "child": [], "youth": [], "adult": [], "senior": []}
    for p in passengers:
        kind = "infant" if p.age < 4 else "child" if p.age < 12 else "youth" if p.age < 26 \
            else "senior" if p.age >= 60 else "adult"
        groups[kind].append(p.age)
    if not (groups["child"] or groups["youth"] or groups["adult"] or groups["senior"]):
        groups["adult"].append(30)          # samotné batole necestuje – aspoň jeden dospělý
    out: dict[str, Any] = {kind: len(ages) for kind, ages in groups.items()}
    if groups["child"]:
        out["childAges"] = groups["child"]
    if groups["youth"]:
        out["youthAges"] = groups["youth"]
    if groups["senior"]:
        out["seniorsAges"] = groups["senior"]
    return out


def search_days(query: SearchQuery) -> list[dt.date]:
    """Dny, na které se ptát: den odjezdu, a přesahuje-li okno půlnoc, i další den."""
    first = query.departure.astimezone(DEFAULT_TZ).date()
    last = query.window_end.astimezone(DEFAULT_TZ).date()
    days = [first]
    if last > first:
        days.append(first + dt.timedelta(days=1))
    return days


def _tz_for(uic: Any) -> dt.tzinfo:
    return LONDON_TZ if str(uic or "").startswith("70") else CONTINENT_TZ


def _local(date: Any, clock: Any, uic: Any) -> dt.datetime | None:
    if not isinstance(date, str) or not isinstance(clock, str):
        return None
    try:
        naive = dt.datetime.fromisoformat(f"{date}T{clock}")
    except ValueError:
        return None
    return naive.replace(tzinfo=_tz_for(uic)).astimezone(DEFAULT_TZ)


def parse_legs(fare: dict[str, Any]) -> list[Leg]:
    legs = []
    for raw in fare.get("legs") or []:
        origin, destination = raw.get("origin") or {}, raw.get("destination") or {}
        timing = raw.get("timing") or {}
        dep = _local(timing.get("departureDate"), timing.get("departureTime"), origin.get("uic"))
        arr = _local(timing.get("arrivalDate") or timing.get("departureDate"), timing.get("arrivalTime"),
                     destination.get("uic"))
        if dep is None or arr is None:
            continue
        if arr < dep:                       # příjezd po půlnoci bez data příjezdu
            arr += dt.timedelta(days=1)
        service = raw.get("serviceType") or {}
        carrier = str(service.get("name") or "Eurostar")
        brand = str(service.get("brandCode") or carrier)
        number = str(raw.get("serviceName") or "").strip()
        code = str(service.get("code") or "").upper()
        mode = Mode.BUS if code in ("BUS", "BS") or "bus" in brand.lower() else Mode.TRAIN
        o_name, d_name = str(origin.get("name") or ""), str(destination.get("name") or "")
        legs.append(Leg(origin=o_name, destination=d_name, departure=dep, arrival=arr, mode=mode,
                        carrier=carrier, line=" ".join(p for p in (brand, number) if p),
                        stops=[Stop(name=o_name, departure=dep), Stop(name=d_name, arrival=arr)],
                        reservation_required=carrier == "Eurostar"))
    return legs


def search_link(origin_uic: str, destination_uic: str, day: dt.date, query: SearchQuery) -> str:
    """Odkaz na výsledky hledání na eurostar.com pro stejnou skupinu."""
    counts = travellers(query.passengers)
    params = {"origin": origin_uic, "destination": destination_uic, "outbound": day.isoformat()}
    params.update({k: v for k, v in counts.items() if isinstance(v, int) and v})
    return f"{SEARCH_WEB}?{urlencode(params)}"
