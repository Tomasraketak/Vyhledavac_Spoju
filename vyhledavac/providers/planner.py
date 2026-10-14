"""Plánovač tras po celé Evropě (Transitous) – spoj bez ceny, který ocení hack.

IDOS zná jen cesty z Česka nebo do Česka. Pro cesty jinde (Kraków → Hamar,
Stockholm → Amsterdam, Valencie → Turín) plánuje trasu Transitous
(api.transitous.org, MOTIS nad otevřenými jízdními řády celé Evropy):
vlaky, dálkové autobusy (FlixBus, BlaBlaCar) i místní dopravu, s dopravcem
každého úseku. Ceny nezná – spoje vrací bez ceny s rozpisem jízdenek
(``raw["plans"]``) a hack ``idos_complete`` je ocení u prodejců: celý kus
u dopravce, který obsluhuje obě země, jinak po kusech v přestupech a na
hranicích (PKP do Berlína, DB/DSB do Kodaně, SJ do Osla, Vy do Hamaru).

Jak se vybírá místo: zahraniční město má v databázi měst souřadnice středu –
ty dostane Transitous a nádraží si vybere podle trasy (Paříž má šest hlavních
nádraží; geokódování podle jména vracelo i zastávky z cizích dat, „Kraków“ ze
švédského feedu nebo Paříž v Brazílii). České místo hledá Transitous podle
jména (data CIS JŘ).

Městská doprava na začátku a na konci cesty (tramvaj na nádraží) se z trasy
vypustí – spoj začíná a končí na nádraží. MHD uprostřed (metro mezi nádražími)
zůstane jako úsek „MHD“ bez ceny (jízdenku koupíte na místě).

Ocení se první 3 spoje (jako u ostatních dopravců spoj po spoji).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any

from .. import geo, stations, transitous
from ..errors import StationNotFoundError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import normalize, same_place
from ..registry import PROVIDERS
from ..trace import trace
from .base import PRICE_SCOPE_COUNT, HttpProvider, scope_end

log = logging.getLogger(__name__)

PLACE_TTL = 30 * 24 * 3600
#: Záložní zastávka (z dat země města) nejdál tolik km od středu.
HOME_STOP_KM = 10.0
#: Úsek MHD: tramvaj/metro, nebo autobus kratší než tolik km.
LOCAL_KM = 20.0
MAX_RIDES = 9
MODES = {"BUS": Mode.BUS, "COACH": Mode.BUS, "TRAM": Mode.TRAM, "SUBWAY": Mode.METRO, "METRO": Mode.METRO,
         "FERRY": Mode.FERRY, "WALK": Mode.WALK, "RAIL": Mode.TRAIN, "HIGHSPEED_RAIL": Mode.TRAIN,
         "LONG_DISTANCE": Mode.TRAIN, "NIGHT_RAIL": Mode.TRAIN, "REGIONAL_FAST_RAIL": Mode.TRAIN,
         "REGIONAL_RAIL": Mode.TRAIN, "SUBURBAN": Mode.TRAIN}


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def pick_czech(name: str, results: list[dict[str, Any]]) -> str | None:
    """České místo jen z českých dat (``cz-…``) – „Olomouc“ jinak padne na
    estonskou zastávku. Přednost má vlaková stanice (CZPTT) jménem obce."""
    wanted = normalize(name)
    best, best_key = None, None
    for r in results:
        rid = str(r.get("id") or "")
        if r.get("type") != "STOP" or not rid.startswith("cz-"):
            continue
        rname = normalize(str(r.get("name") or ""))
        if not rname.startswith(wanted.split(" ")[0]):
            continue
        key = ("CZPTT" not in rid, rname != wanted and not rname.startswith(wanted + " "), len(rname))
        if best_key is None or key < best_key:
            best, best_key = rid, key
    return best or transitous.pick_place(name, [r for r in results if str(r.get("id") or "").startswith("cz-")])


def leg_mode(raw: dict[str, Any]) -> Mode:
    mode = str(raw.get("mode") or "")
    if mode in MODES:
        return MODES[mode]
    kind = transitous.kind_of(raw)
    return Mode.BUS if "autobus" in kind else Mode.TRAM if kind == "tramvaj" else \
        Mode.METRO if kind == "metro" else Mode.TRAIN


#: Krátká jízda vlakem po městě (S-Bahn Lichtenberg → Hauptbahnhof) = MHD.
LOCAL_TRAIN_KM = 12.0


def is_local(leg: Leg, length: float) -> bool:
    if leg.mode is Mode.TRAIN:
        return 0 < length < LOCAL_TRAIN_KM
    return leg.mode in (Mode.TRAM, Mode.METRO) or (leg.mode is Mode.BUS and length < LOCAL_KM
                                                     and "flix" not in leg.carrier.lower())


@PROVIDERS.register()
class PlannerProvider(HttpProvider):
    name = "Plánovač"
    label = "Plánovač tras po Evropě (Transitous) – trasu ocení dopravci po kusech"
    multi_passenger = True
    provides_prices = False         # cenu doplní hack idos_complete
    currency = "EUR"
    countries = ()
    region = "west"
    czech_names = True
    international_only = True
    hack_legs = False
    min_interval = 0.6

    async def places(self, name: str) -> list[str]:
        """Kde v Transitous začít/skončit: u zahraničního města souřadnice středu
        a jako záloha zastávka z dat té země nejblíž středu (ze středu Košic
        Transitous cestu nenajde, ze zastávky ŽSR ano); u českého místa zastávka."""
        key = f"planner-place5:{normalize(name)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return list(cached)
        city = stations.place(name)
        headers = {"User-Agent": transitous.USER_AGENT}
        out: list[str] = []
        if city is not None and city.code and city.lat is not None:
            out.append(f"{city.lat},{city.lon}")
            found = await self.client.get_json(f"{transitous.API}/v1/geocode",
                                               params={"text": city.local or city.name, "language": "en"},
                                               headers=headers)
            home = [r for r in found or [] if r.get("type") == "STOP" and r.get("lat") is not None
                    and str(r.get("id") or "").lower().startswith(f"{city.code.lower()}-")
                    and _km(city.lat, city.lon, float(r["lat"]), float(r["lon"])) <= HOME_STOP_KM]
            if home:
                home.sort(key=lambda r: _km(city.lat, city.lon, float(r["lat"]), float(r["lon"])))
                out.append(str(home[0]["id"]))
        else:
            found = await self.client.get_json(f"{transitous.API}/v1/geocode",
                                               params={"text": name, "language": "cs"}, headers=headers)
            chosen = pick_czech(name, found or [])
            if chosen is not None:
                out.append(chosen)
        if not out:
            raise StationNotFoundError(self.name, f"místo {name!r} Transitous nezná")
        trace(f"plánovač: {name} → {' / '.join(out)}")
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    async def search(self, query: SearchQuery) -> list[Journey]:
        if not geo.leaves_czechia(query.origin, query.destination):
            raise StationNotFoundError(self.name, "cesty po Česku plánuje IDOS")
        origins, destinations = await self.places(query.origin), await self.places(query.destination)
        tries = [(origins[0], destinations[0])]
        if (origins[-1], destinations[-1]) != tries[0]:
            tries.append((origins[-1], destinations[-1]))
        data: dict[str, Any] = {}
        for origin, destination in tries:
            params = {"fromPlace": origin, "toPlace": destination, "numItineraries": 6,
                      "time": query.departure.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            # čistě vlakové spojení, jinak vše pozemní – bez letadel
            params["transitModes"] = "RAIL" if query.trains_only else transitous.GROUND_MODES
            data = await self.client.get_json(f"{transitous.API}/v5/plan", params=params,
                                              headers={"User-Agent": transitous.USER_AGENT}) or {}
            if data.get("itineraries"):
                break
            trace(f"plánovač: z {origin} do {destination} nic – zkouším zastávky")
        journeys = []
        for it in data.get("itineraries") or []:
            if any(transitous.is_air(leg) for leg in it.get("legs") or []):
                continue                          # letadlo nehledáme
            journey = self.to_journey(it, query)
            if journey is None:
                continue
            if any((journey.departure, journey.arrival) == (o.departure, o.arrival) for o in journeys):
                continue
            journeys.append(journey)
        until = scope_end(query, [j.departure for j in journeys])
        journeys = sorted((j for j in journeys if j.departure <= until), key=lambda j: (j.departure, j.arrival))
        return journeys[:PRICE_SCOPE_COUNT]

    def to_journey(self, it: dict[str, Any], query: SearchQuery) -> Journey | None:
        legs: list[Leg] = []
        lengths: list[float] = []
        for raw in it.get("legs") or []:
            leg = parse_leg(raw)
            if leg is None:
                return None
            a, b = raw.get("from") or {}, raw.get("to") or {}
            length = _km(a["lat"], a["lon"], b["lat"], b["lon"]) if None not in (
                a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")) else 0.0
            legs.append(leg)
            lengths.append(length)
        # MHD a chůze na začátku a na konci: spoj začíná a končí na nádraží.
        # U českého konce jen MHD po tom městě – vlak Ostrava → Bohumín (8 km)
        # je část cesty, ne městská doprava
        def local_end(leg: Leg, length: float, place: str, inner: str) -> bool:
            if leg.mode is Mode.WALK:
                return True
            if not is_local(leg, length):
                return False
            return geo.country_of(place) not in (None, "CZ") or same_place(inner, place)

        while legs and local_end(legs[0], lengths[0], query.origin, legs[0].destination):
            legs.pop(0), lengths.pop(0)
        while legs and local_end(legs[-1], lengths[-1], query.destination, legs[-1].origin):
            legs.pop(), lengths.pop()
        rides = [(leg, n) for leg, n in zip(legs, lengths) if leg.mode is not Mode.WALK]
        if not rides or len(rides) > MAX_RIDES:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link="https://transitous.org/")
        plan: list[dict] = []
        for k, (leg, length) in enumerate(rides):
            kind = "local" if is_local(leg, length) else "buy"
            last = max(len(leg.stops), 2) - 1
            if plan and plan[-1]["kind"] == kind == "buy":
                plan[-1]["to"] = [k, last]
            else:
                plan.append({"kind": kind, "from": [k, 0], "to": [k, last], "splits": 6})
        journey.raw = {"plans": [plan]}
        journey.notes.append("Trasa z plánovače Transitous – jízdenky na jednotlivé kusy dohledá hack "
                             "„Spoje z IDOS / plánovače: úseky ocení dopravci“.")
        return journey


def parse_leg(raw: dict[str, Any]) -> Leg | None:
    dep, arr = transitous._time(raw.get("startTime")), transitous._time(raw.get("endTime"))
    if dep is None or arr is None:
        return None
    mode = leg_mode(raw)
    origin = str((raw.get("from") or {}).get("name") or "")
    destination = str((raw.get("to") or {}).get("name") or "")
    if mode is Mode.WALK:
        return Leg(origin=origin, destination=destination, departure=dep, arrival=arr, mode=Mode.WALK,
                   line="přesun")
    stops = [Stop(origin, departure=dep)]
    for s in raw.get("intermediateStops") or []:
        stops.append(Stop(str(s.get("name") or ""),
                          arrival=transitous._time(s.get("arrival") or s.get("scheduledArrival")),
                          departure=transitous._time(s.get("departure") or s.get("scheduledDeparture"))))
    stops.append(Stop(destination, arrival=arr))
    kind = "vlak" if mode is Mode.TRAIN else transitous.kind_of(raw)
    return Leg(origin=origin, destination=destination, departure=dep, arrival=arr, mode=mode,
               carrier=str(raw.get("agencyName") or ""), line=transitous.line_of(raw, kind), stops=stops)
