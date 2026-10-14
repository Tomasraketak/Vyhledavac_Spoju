"""European Sleeper – noční vlak Praha – Drážďany – Berlín – Amsterdam – Brusel
(a od 2026 i Brusel – Kolín – Curych – Milán a Paříž – Brusel – Berlín).

Prodej běží na vlastním e-shopu ``booking.europeansleeper.eu`` (Next.js, ne
Bileto). Ten volá veřejné JSON API ``europeansleeperprod-api.azurewebsites.net``
bez přihlášení (hlavička ``Authorization`` je prázdná):

* ``GET /api/constants`` – stanice (vlastní ID, např. Praha hl. n. ``5457076``,
  Amsterdam CS ``8400058``), trasy (``routes``), typy cestujících a země, kde
  se nesmí jet vnitrostátně (``domesticJourneyDisabledCountries``: CZ, NL, BE,
  FR, CH, IT – Praha – Děčín tedy koupit nejde, Berlín – Drážďany ano);
* ``POST /api/search/trains`` – ``{fromLocationId, toLocationId, departureDate,
  passengerTypes, bicycleCount, petsCount}`` → jízdy na několik dní kolem data
  (``trip`` = null ve dnech, kdy vlak nejede): číslo vlaku, odjezd, příjezd,
  ``routeId``;
* ``POST /api/search/availability`` – ``{trainRouteId, fromLocationId,
  toLocationId, passengerTypes, trainNumber, travelDate, bicycleCount}`` →
  ``priceClasses`` (sedadlo, lehátko, lůžko …) s cenou **za celou skupinu**
  v EUR/CZK a tarify Easy/Good/Flex Night.

Postup: stanice podle statického seznamu (níže, ověřeno z ``/api/constants``
v říjnu 2026) → spoje → pro každý spoj v okně dostupnost a ceny. Cena spoje
= nejlevnější prodejná kategorie (obvykle lehátko Easy Night); ostatní
kategorie jsou v ``fares`` a v ``journey.raw["offers"]``.

Na co si dát pozor:

* Vlak nejezdí denně (Praha → Amsterdam v říjnu 2026 út, čt, ne v 18:00;
  zpět po, st, pá). Hledání posílá jen datum, API vrátí okolní dny – filtrujeme
  podle okna dotazu.
* Cestující: dospělý (typ 72) od 12 let, dítě 4–11 let (73), do 3 let zdarma
  bez vlastního místa (do dotazu se nepočítá). Nejvýš 5 osob na rezervaci –
  větší skupině se cena neukáže.
* „Soukromé kupé“ (``*-private``) má cenu za celé kupé – pro 4–5 lidí bývá
  nejlevnější. Kupé „jen pro ženy“ se jako cena spoje nebere.
* Vnitrostátně v Česku (Praha – Ústí – Děčín) se neprodává – proto
  ``international_only``. Ptáme se jen, když oba konce leží na stejné trase.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://europeansleeperprod-api.azurewebsites.net/api"
WEB = "https://booking.europeansleeper.eu/"
AVAILABILITY_TTL = 10 * 60
ADULT, CHILD = 72, 73
MAX_PASSENGERS = 5

#: Stanice: ID, název u dopravce, země (ISO), klíčová slova (normalizovaná).
STATIONS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("5457076", "Praha hl. n.", "CZ", ("praha", "praha hl n", "praha hlavni nadrazi", "prague", "prag")),
    ("5453179", "Ústí nad Labem hl. n.", "CZ", ("usti nad labem", "usti nad labem hl n")),
    ("5455659", "Děčín hl. n.", "CZ", ("decin", "decin hl n")),
    ("8001311", "Bad Schandau", "DE", ("bad schandau",)),
    ("8001305", "Dresden Hbf", "DE", ("dresden", "dresden hbf", "drazdany", "drazdany hl n")),
    ("8010100", "Berlin Hbf", "DE", ("berlin", "berlin hbf", "berlin hauptbahnhof", "berlin hl n")),
    ("8010110", "Berlin Ostbahnhof", "DE", ("berlin ostbahnhof", "berlin ost")),
    ("8020401", "Hamburg-Harburg", "DE", ("hamburg harburg", "harburg")),
    ("8052101", "Aachen Hbf", "DE", ("aachen", "aachen hbf", "cachy")),
    ("8050500", "Köln Hbf", "DE", ("koln", "koeln", "koln hbf", "koeln hbf", "kolin nad rynem", "cologne")),
    ("8400173", "Deventer", "NL", ("deventer",)),
    ("8400055", "Amersfoort Centraal", "NL", ("amersfoort", "amersfoort centraal")),
    ("8400058", "Amsterdam Centraal", "NL", ("amsterdam", "amsterdam cs", "amsterdam centraal",
                                             "amsterdam c")),
    ("8400280", "Den Haag HS", "NL", ("den haag", "den haag hs", "haag", "the hague")),
    ("8400530", "Rotterdam Centraal", "NL", ("rotterdam", "rotterdam cs", "rotterdam centraal",
                                             "rotterdam c")),
    ("8400131", "Breda", "NL", ("breda",)),
    ("8400526", "Roosendaal", "NL", ("roosendaal",)),
    ("8400206", "Eindhoven Centraal", "NL", ("eindhoven", "eindhoven centraal")),
    ("8800210", "Antwerpen-Centraal", "BE", ("antwerpen", "antwerpen centraal", "antverpy", "antwerp")),
    ("8800104", "Bruxelles-Midi", "BE", ("bruxelles", "bruxelles midi", "brussel", "brussel zuid",
                                         "brussels", "brusel", "brussels midi")),
    ("8800410", "Liège-Guillemins", "BE", ("liege", "liege guillemins", "lutych")),
    ("8800440", "Verviers-Central", "BE", ("verviers", "verviers central")),
    ("8800810", "Mons", "BE", ("mons",)),
    ("8700015", "Paris Nord", "FR", ("paris", "paris nord", "pariz", "pariz nord")),
    ("8700433", "Saint-Quentin", "FR", ("st quentin", "saint quentin")),
    ("8700021", "Aulnoye-Aymeries", "FR", ("aulnoye aymeries", "aulnoye")),
    ("8500375", "Aarau", "CH", ("aarau",)),
    ("8500004", "Arth-Goldau", "CH", ("arth goldau",)),
    ("8500200", "Zürich HB", "CH", ("zurich", "zurich hb", "curych", "zuerich")),
    ("8500689", "Göschenen", "CH", ("goschenen", "goeschenen")),
    ("8500521", "Bellinzona", "CH", ("bellinzona",)),
    ("8500011", "Lugano", "CH", ("lugano",)),
    ("8500360", "Chiasso", "CH", ("chiasso",)),
    ("8300133", "Como San Giovanni", "IT", ("como", "como san giovanni")),
    ("8300112", "Milano Porta Garibaldi", "IT", ("milano", "milano porta garibaldi", "milan",
                                                 "milan porta garibaldi")),
]
#: Trasy (pořadí zastávek, vlak jede oběma směry) podle ``/api/constants``.
ROUTES: list[tuple[str, ...]] = [
    # Praha – Berlín – Amsterdam – Brusel (ES 452/453)
    ("8800104", "8800210", "8400526", "8400131", "8400530", "8400280", "8400058", "8400055",
     "8400173", "8010100", "8010110", "8001305", "8001311", "5455659", "5453179", "5457076"),
    # Brusel – Kolín – Curych – Milán (od 9. 9. 2026; NL zastávky od 13. 12. 2026)
    ("8800104", "8800410", "8800440", "8800210", "8400131", "8400526", "8400058", "8400280",
     "8400530", "8400206", "8052101", "8050500", "8500375", "8500004", "8500200", "8500689",
     "8500521", "8500011", "8500360", "8300133", "8300112"),
    # Paříž – Brusel – Berlín
    ("8700015", "8700433", "8700021", "8800810", "8800104", "8800410", "8020401", "8010110",
     "8010100"),
]
#: Poloha stanic (pro hack ``night_train`` – která stanice trasy je nejblíž cíli).
COORDS: dict[str, tuple[float, float]] = {
    "5457076": (50.083, 14.435), "5453179": (50.659, 14.044), "5455659": (50.773, 14.212),
    "8001311": (50.917, 14.153), "8001305": (51.040, 13.732), "8010100": (52.525, 13.369),
    "8010110": (52.510, 13.435), "8020401": (53.456, 9.992), "8052101": (50.768, 6.091),
    "8050500": (50.943, 6.959), "8400173": (52.257, 6.161), "8400055": (52.153, 5.374),
    "8400058": (52.379, 4.900), "8400280": (52.070, 4.322), "8400530": (51.925, 4.469),
    "8400131": (51.595, 4.780), "8400526": (51.540, 4.459), "8400206": (51.443, 5.481),
    "8800210": (51.217, 4.421), "8800104": (50.836, 4.336), "8800410": (50.624, 5.567),
    "8800440": (50.588, 5.858), "8800810": (50.454, 3.942), "8700015": (48.881, 2.355),
    "8700433": (49.840, 3.300), "8700021": (50.207, 3.833), "8500375": (47.391, 8.051),
    "8500004": (47.050, 8.548), "8500200": (47.378, 8.540), "8500689": (46.666, 8.588),
    "8500521": (46.195, 9.029), "8500011": (46.005, 8.947), "8500360": (45.832, 9.032),
    "8300133": (45.809, 9.073), "8300112": (45.485, 9.187),
}
#: Země, kde dopravce neprodává vnitrostátní jízdenky.
NO_DOMESTIC = {"CZ", "NL", "BE", "FR", "CH", "IT"}
_BY_ID = {s[0]: s for s in STATIONS}

#: Kategorie míst (``placeTypeKey``, např. ``couchette-6-private``) -> český popis.
PLACE_KINDS = {"seat": "sedadlo", "couchette": "lehátko", "berth": "lůžko", "comfort": "lůžko Comfort"}
PLACE_SIZES = {"single": 1, "double": 2, "triple": 3}
FARE_TYPES = {"easy-night": "Easy Night", "good-night": "Good Night", "flex-night": "Flex Night"}


@PROVIDERS.register()
class EuropeanSleeperProvider(HttpProvider):
    name = "EuropeanSleeper"
    label = "European Sleeper (noční vlak Praha – Berlín – Amsterdam – Brusel)"
    multi_passenger = True
    currency = "EUR"
    countries = ("CZ", "DE", "NL", "BE", "FR", "CH", "IT")
    region = "west"
    international_only = True          # Praha – Děčín se neprodává
    referer = WEB
    min_interval = 0.4

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        return await self.client.post_json(f"{API}/{path}", body, referer=WEB,
                                           headers={"Authorization": ""})

    async def search(self, query: SearchQuery) -> list[Journey]:
        origin, destination = pick_station(query.origin), pick_station(query.destination)
        for place, found in ((query.origin, origin), (query.destination, destination)):
            if found is None:
                raise StationNotFoundError(self.name, f"stanice {place!r} neleží na trase European Sleeper")
        if not same_route(origin, destination):
            raise StationNotFoundError(self.name, "obě stanice neleží na stejné trase European Sleeper")
        types = passenger_types(query)
        date = query.departure.astimezone(DEFAULT_TZ).date().isoformat()
        data = await self._post("search/trains", {
            "fromLocationId": origin, "toLocationId": destination, "departureDate": date,
            "passengerTypes": types or [ADULT], "bicycleCount": 0, "petsCount": 0, "returnDate": None})
        journeys = []
        for trip in parse_trips(data):
            journey = to_journey(self.name, trip, query, types)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        if journeys and not types:
            self.note(f"European Sleeper prodá online nejvýš {MAX_PASSENGERS} cestujících – "
                      "cenu pro skupinu ověřte u dopravce")
        elif journeys:
            await asyncio.gather(*(self._price(j, types) for j in journeys))
        return journeys

    async def _price(self, journey: Journey, types: list[int]) -> None:
        trip = journey.raw["trip"]
        key = (f"avail:{trip['routeId']}:{trip['trainNumber']}:{trip['departureTime']}:"
               f"{trip['fromLocationId']}:{trip['toLocationId']}:{','.join(map(str, sorted(types)))}")
        data = self.cache.get(self.name, key)
        if data is None:
            try:
                data = await self._post("search/availability", {
                    "trainRouteId": trip["routeId"], "fromLocationId": trip["fromLocationId"],
                    "toLocationId": trip["toLocationId"], "passengerTypes": types,
                    "trainNumber": trip["trainNumber"], "travelDate": trip["departureTime"],
                    "bicycleCount": 0, "connectingTrainSelection": None})
            except Exception as exc:  # noqa: BLE001 - jízdní řád zůstane
                log.debug("European Sleeper: dostupnost %s: %s", trip["trainNumber"], exc)
                self.note("ceny se nepodařilo zjistit – jízdní řád je bez ceny")
                return
            self.cache.set(self.name, key, data, ttl=AVAILABILITY_TTL)
        apply_offers(self, journey, data)


# ---- pomůcky ---------------------------------------------------------------------
def pick_station(place: str) -> str | None:
    """ID stanice European Sleeper pro jméno místa (``None`` = na trase není)."""
    import re

    # zkratky z IDOS/ÖBB: „Ústí n. L. hl. n.“, „Usti n.L.“
    wanted = re.sub(r"\bn l\b", "nad labem", normalize(place))
    if not wanted:
        return None
    for sid, name, _, aliases in STATIONS:
        if wanted in aliases or wanted == normalize(name):
            return sid
    # „Praha hl.n.“, „Amsterdam Centraal (NL)“ – první slova jako u aliasu
    for sid, _, _, aliases in STATIONS:
        for alias in sorted(aliases, key=len, reverse=True):
            if wanted.startswith(alias + " ") and len(alias) >= 4:
                rest = wanted[len(alias) + 1:]
                if rest in ("hl n", "hbf", "hauptbahnhof", "centraal", "cs", "c", "midi", "zuid", "nord",
                            "hb", "centrale", "central", "hlavni nadrazi", "nl", "de", "be", "cz"):
                    return sid
    return None


def same_route(origin: str, destination: str) -> bool:
    if origin == destination:
        return False
    a, b = _BY_ID[origin][2], _BY_ID[destination][2]
    if a == b and a in NO_DOMESTIC:
        return False
    return any(origin in route and destination in route for route in ROUTES)


def passenger_types(query: SearchQuery) -> list[int]:
    """Typy cestujících: od 12 let dospělý, 4–11 dítě, do 3 let zdarma (bez místa).

    Prázdný seznam = skupina je pro online prodej příliš velká.
    """
    types = [ADULT if p.age >= 12 else CHILD for p in query.passengers if p.age >= 4]
    if len(types) > MAX_PASSENGERS:
        return []
    return types or [ADULT]


def parse_trips(data: Any) -> list[dict[str, Any]]:
    """Jízdy z ``/search/trains`` (jen dny, kdy vlak jede, a jen prodejné)."""
    trips = []
    for route in (data or {}).get("routeSearchResults") or []:
        for item in route.get("searchResultOutward") or []:
            trip = item.get("trip")
            if not trip or not trip.get("departureTime") or not trip.get("arrivalTime"):
                continue
            trips.append({**trip, "routeId": item.get("routeId"), "bookable": trip.get("bookable", True),
                          "connecting": bool(item.get("connectingTrain"))})
    return trips


def to_journey(provider: str, trip: dict[str, Any], query: SearchQuery,
               types: list[int]) -> Journey | None:
    dep, arr = _time(trip.get("departureTime")), _time(trip.get("arrivalTime"))
    start, end = _BY_ID.get(str(trip.get("fromLocationId"))), _BY_ID.get(str(trip.get("toLocationId")))
    if dep is None or arr is None or start is None or end is None:
        return None
    line = f"ES {trip.get('trainNumber')}".strip()
    leg = Leg(origin=start[1], destination=end[1], departure=dep, arrival=arr, mode=Mode.TRAIN,
              carrier="European Sleeper", line=line, reservation_required=True,
              stops=[Stop(name=start[1], departure=dep), Stop(name=end[1], arrival=arr)])
    params = [("departureStation", start[0]), ("arrivalStation", end[0]),
              ("departureDate", dep.date().isoformat())]
    params += [("passengerTypes", str(t)) for t in types or [ADULT]]
    params += [("bicycleCount", "0"), ("petsCount", "0")]
    journey = Journey(provider=provider, legs=[leg], fare_status=FareStatus.UNKNOWN,
                      journey_id=f"{trip.get('routeId')}:{trip.get('trainNumber')}:{dep.date()}",
                      deep_link=f"{WEB}cs/?{urlencode(params)}", raw={"trip": trip})
    if not trip.get("bookable"):
        journey.fare_status = FareStatus.NOT_BOOKABLE
        journey.notes.append("European Sleeper: spoj teď nelze koupit online.")
    detour = trip.get("detourInfo") or {}
    if detour.get("hasDetour"):
        journey.notes.append("European Sleeper: vlak jede odklonem – ověřte zastávky u dopravce.")
    return journey


def apply_offers(provider: Any, journey: Journey, data: Any) -> None:
    """Ceny z ``/search/availability``: jedna nabídka (nejlevnější tarif) na kategorii."""
    availability = ((data or {}).get("availabilityResult") or {}).get("availability") or {}
    offers, fares = [], []
    for cls in availability.get("priceClasses") or []:
        key = str(cls.get("placeTypeKey") or "")
        if not cls.get("bookable"):
            continue
        options = [f for f in cls.get("fareTypes") or []
                   if f.get("bookable") and (f.get("prices") or {}).get("eur") is not None]
        if not options:
            continue
        best = min(options, key=lambda f: Decimal(str(f["prices"]["eur"])))
        amount = Decimal(str(best["prices"]["eur"]))
        label = place_label(key)
        fare_name = FARE_TYPES.get(str(best.get("id")), str(best.get("id") or ""))
        seats = best.get("availableSeats")
        offers.append({"class": key, "label": label, "fare": fare_name, "eur": str(amount),
                       "czk": (best["prices"] or {}).get("czk"), "seats": seats})
        if "women-only" in key:
            continue              # kupé jen pro ženy jako cenu spoje nebereme
        fares.append(provider.make_fare(amount, "EUR", label="European Sleeper",
                                        tariff=f"{label}, {fare_name}",
                                        flexible=best.get("id") == "flex-night",
                                        seats_left=seats if isinstance(seats, int) else None,
                                        deep_link=journey.deep_link))
    journey.raw["offers"] = offers
    if fares:
        journey.fares = sorted(fares, key=lambda f: f.sort_value)
        journey.fare_status = FareStatus.PRICED
    elif journey.fare_status is not FareStatus.NOT_BOOKABLE:
        journey.fare_status = FareStatus.SOLD_OUT


def place_label(key: str) -> str:
    """``couchette-6-private`` -> „lehátko (kupé pro 6), celé kupé“."""
    if key == "seat-second-class":
        return "sedadlo 2. tř."
    parts = key.split("-")
    label = PLACE_KINDS.get(parts[0], parts[0])
    size = next((PLACE_SIZES.get(p) or (int(p) if p.isdigit() else None) for p in parts[1:]
                 if p in PLACE_SIZES or p.isdigit()), None)
    if size:
        label += f" (kupé pro {size})"
    if "private" in parts:
        label += ", celé kupé"
    if "women" in parts:
        label += ", jen pro ženy"
    return label


def _time(value: Any) -> dt.datetime | None:
    """Časy jsou místní (všechny stanice leží v pásmu SEČ/SELČ)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
