"""Spojení z Transitous (api.transitous.org) – záloha za IDOS.

Transitous je otevřený evropský plánovač (MOTIS) nad veřejnými daty jízdních
řádů. Pro Česko má celostátní data CIS JŘ – všechny autobusové linky (JDF),
vlaky (CZPTT) – a data IDS (PID, IDS JMK …). Ceny nezná. Volání:

* ``GET /api/v1/geocode?text=…&language=cs`` – zastávky (``type: STOP``,
  ``id`` např. ``cz-JDF-merged_jdf:stop:33720`` = „Domanín,Vrchy“) a obce
  (``type: PLACE`` se souřadnicemi);
* ``GET /api/v5/plan?fromPlace=…&toPlace=…&time=…`` – spojení. Místo je id
  zastávky, nebo „lat,lon“ (u obce – pak spojení začíná a končí pěšky).
  ``nextPageCursor`` vrací další stránku.

Každý úsek má ``agencyName`` (dopravce), ``routeShortName`` („100“, „R19“),
``tripShortName`` (u autobusu licence a spoj „340100 7“, u vlaku „R 865“),
``routeType`` (GTFS), ``intermediateStops`` s časy a časy v UTC.

Na co si dát pozor:

* Data jsou sloučená z více zdrojů – stejný vlak přijde z CZPTT, z dat ZSSK
  i z KORDIS JMK. Spojení proto slučujeme podle odjezdu, příjezdu a prvního
  dopravního úseku.
* Jména zastávek jsou ve tvaru CIS JŘ („Třeboň,aut.nádr.“) jako v IDOS.
* Jde o komunitní službu – ptáme se střídmě (jen když IDOS nejde, nebo když
  si uživatel dopravce „Transitous“ zapne) a posíláme User-Agent s odkazem
  na projekt.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .idos import IdosConnection, IdosLeg
from .models import DEFAULT_TZ
from .normalize import normalize

log = logging.getLogger(__name__)

API = "https://api.transitous.org/api"
USER_AGENT = "vyhledavac-spoju (+https://github.com/tomasraketak/vyhledavac_spoju)"

#: Druh úseku podle GTFS ``routeType`` → text jako v IDOS (podle něj se pozná bus/MHD/vlak).
def kind_of(leg: dict[str, Any]) -> str:
    route_type = int(leg.get("routeType") or 0)
    mode = str(leg.get("mode") or "")
    if route_type in (0, 900) or mode == "TRAM":
        return "tramvaj"
    if route_type in (1, 400) or mode == "SUBWAY":
        return "metro"
    if route_type in (11, 800):
        return "trolejbus"
    if route_type in (3, 700, 704, 715) or mode == "BUS":
        return "městský autobus" if route_type == 704 else "regionální autobus"
    if route_type in (200, 201, 202) or mode == "COACH":
        return "dálkový autobus"
    if route_type in (4, 1000, 1200) or mode == "FERRY":
        return "přívoz"
    return "vlak"


def line_of(leg: dict[str, Any], kind: str) -> str:
    """Název úseku jako v IDOS: „Bus 340100 7“, „R19 (R 865)“, „Tram 2“."""
    short = str(leg.get("routeShortName") or "").strip()
    trip = str(leg.get("tripShortName") or "").strip()
    if "autobus" in kind or kind == "trolejbus":
        licence = trip if trip[:6].isdigit() else ""
        return f"Bus {licence or short}".strip()
    if kind in ("tramvaj", "metro"):
        return f"{'Tram' if kind == 'tramvaj' else 'Metro'} {short}".strip()
    if trip and short and trip != short:
        return f"{short} ({trip})"
    return trip or short or "vlak"


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(DEFAULT_TZ)
    except ValueError:
        return None


def to_connections(data: dict[str, Any]) -> list[IdosConnection]:
    """Itineráře z ``plan`` → spojení jako z IDOS (bez pěších úseků, bez duplicit)."""
    out: list[IdosConnection] = []
    seen: set[tuple] = set()
    for it in (data or {}).get("itineraries") or []:
        legs = []
        for raw in it.get("legs") or []:
            if raw.get("mode") == "WALK":
                continue
            dep, arr = _time(raw.get("startTime")), _time(raw.get("endTime"))
            if dep is None or arr is None:
                legs = []
                break
            kind = kind_of(raw)
            stops = [(str(s.get("name") or ""),
                      _hhmm(s.get("arrival") or s.get("scheduledArrival")),
                      _hhmm(s.get("departure") or s.get("scheduledDeparture")))
                     for s in raw.get("intermediateStops") or []]
            origin, destination = str(raw["from"].get("name") or ""), str(raw["to"].get("name") or "")
            leg = IdosLeg(line=line_of(raw, kind), kind=kind, carrier=str(raw.get("agencyName") or ""),
                          origin=origin, destination=destination, departure=dep, arrival=arr)
            leg.inline_stops = [(origin, "", _hhmm(raw.get("startTime"))), *stops,
                                (destination, _hhmm(raw.get("endTime")), "")]
            legs.append(leg)
        if not legs:
            continue
        key = (legs[0].departure, legs[-1].arrival, legs[0].carrier.split(",")[0][:12],
               tuple(normalize(leg.destination)[:6] for leg in legs[:1]))
        if key in seen:
            continue
        seen.add(key)
        out.append(IdosConnection(legs))
    out.sort(key=lambda c: (c.departure, c.arrival))
    return out


def _hhmm(value: Any) -> str:
    when = _time(value)
    return when.strftime("%H:%M") if when else ""


#: Zastávka/obec smí ležet nejvýš tak daleko od polohy města z databáze.
NEAR_KM = 60.0


def pick_place(name: str, results: list[dict[str, Any]]) -> str | None:
    """Zastávka se stejným jménem (data země, kde místo leží – „Trnava“ je
    slovenská, ne obec u Třebíče; „Vídeň“ je Wien, ne česká obec Vídeň), jinak
    obec jako „lat,lon“."""
    from . import stations
    from .geo import country_of

    country = country_of(name) or "CZ"
    home = f"{country.lower()}-"
    city = stations.place(name)
    wanted = {normalize(name)} | ({normalize(city.local), normalize(city.name)} - {""} if city else set())
    if city is not None and city.lat is not None and city.lon is not None:
        # jen místa poblíž skutečného města: „Roma“ je i stanice metra v Lisabonu
        from .geo import km

        def near(r: dict[str, Any]) -> bool:
            try:
                return km((float(r["lat"]), float(r["lon"])), (city.lat, city.lon)) <= NEAR_KM
            except (KeyError, TypeError, ValueError):
                return False

        results = [r for r in results if near(r)]
    stops = [r for r in results if r.get("type") == "STOP" and r.get("id")]
    if country != "CZ":
        # zahraniční místo: české zastávky stejného jména (obec Vídeň) nebrat
        foreign = [r for r in stops if not str(r["id"]).startswith("cz-")]
        stops = foreign or stops
    stops.sort(key=lambda r: not str(r["id"]).startswith(home))
    exact = [r for r in stops if normalize(str(r.get("name") or "")) in wanted]
    exact.sort(key=lambda r: (not str(r["id"]).startswith(home), not str(r["id"]).startswith("cz-")))
    places = [r for r in results if r.get("type") == "PLACE" and r.get("lat") is not None]
    if exact:
        return str(exact[0]["id"])
    if places and normalize(str(places[0].get("name") or "")) in wanted | {normalize(name).split(" ")[0]}:
        return f"{places[0]['lat']},{places[0]['lon']}"
    if stops:
        return str(stops[0]["id"])
    if places:
        return f"{places[0]['lat']},{places[0]['lon']}"
    return None


def _query_text(name: str) -> str:
    """Zahraniční město se hledá místním jménem (Vídeň → Wien)."""
    from . import stations
    from .geo import country_of

    if (country_of(name) or "CZ") == "CZ":
        return name
    city = stations.place(name)
    if city is not None and city.local and normalize(city.name) == normalize(name):
        return city.local
    return name


async def search(client: Any, cache: Any, namespace: str, origin: str, destination: str,
                 when: dt.datetime, until: dt.datetime, pages: int = 3) -> list[IdosConnection]:
    """Spojení z Transitous od ``when`` do ``until`` (``client`` = náš HttpClient)."""
    headers = {"User-Agent": USER_AGENT}
    ends = []
    for name in (origin, destination):
        key = f"transitous-place4:{normalize(name)}"
        place = cache.get(namespace, key)
        if not place:
            found = await client.get_json(f"{API}/v1/geocode", params={"text": _query_text(name), "language": "cs"},
                                          headers=headers)
            place = pick_place(name, found or [])
            if place is None:
                from .errors import StationNotFoundError

                raise StationNotFoundError(namespace, f"místo {name!r} Transitous nezná")
            cache.set(namespace, key, place, ttl=30 * 24 * 3600)
        ends.append(place)
    connections: list[IdosConnection] = []
    params: dict[str, Any] = {"fromPlace": ends[0], "toPlace": ends[1], "numItineraries": 8,
                              "time": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for _ in range(pages):
        data = await client.get_json(f"{API}/v5/plan", params=params, headers=headers) or {}
        for conn in to_connections(data):
            if all((conn.departure, conn.arrival, conn.legs[0].line) !=
                   (o.departure, o.arrival, o.legs[0].line) for o in connections):
                connections.append(conn)
        cursor = data.get("nextPageCursor")
        if not cursor or not connections or max(c.departure for c in connections) > until:
            break
        params["pageCursor"] = cursor
    return [c for c in connections if when - dt.timedelta(minutes=1) <= c.departure <= until]
