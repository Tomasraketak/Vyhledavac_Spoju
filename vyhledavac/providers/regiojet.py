"""RegioJet přes veřejné API.

Původní verze otevírala Chromium, vyplňovala našeptávač po znacích a pak
četla DOM. To padalo na každé změně layoutu a bylo snadné ho zablokovat.
Frontend RegioJetu přitom volá obyčejné JSON API na ``brn-ybus-pubapi.sa.cz``
– stejné volání uděláme přímo. Žádný prohlížeč, řádově rychlejší, stabilní.

Endpointy:
* ``/restapi/consts/locations`` – kompletní seznam zemí, měst a stanic
* ``/restapi/routes/search/simple`` – vyhledání spojů
* ``/restapi/routes/{id}/simple`` – detail (zastávky, cenové třídy)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from ..errors import StationNotFoundError
from ..models import (
    FareCategory,
    FareStatus,
    Journey,
    Leg,
    Mode,
    Passenger,
    SearchQuery,
    Stop,
)
from ..normalize import best_match, score_match
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://brn-ybus-pubapi.sa.cz/restapi"
LOCATIONS_TTL = 7 * 24 * 3600   # seznam stanic se mění zřídka
ROUTE_TTL = 300                 # ceny se mění často

_VEHICLE_MODES = {
    "TRAIN": Mode.TRAIN,
    "BUS": Mode.BUS,
}


#: Lidské názvy tarifů podle ``/consts/tariffs`` – kódy samy klamou.
_TARIFF_NAMES = {
    "REGULAR": "dospělý",
    "ATTENDED_CHILD": "dítě 0–5",
    "CHILD_UNDER_12": "dítě 6–12",
    "CZECH_STUDENT_PASS_15": "mladistvý 13–17",
    "CZECH_STUDENT_PASS_26": "student 18–25",
    "ISIC": "senior 65+",
}


@PROVIDERS.register()
class RegioJetProvider(HttpProvider):
    name = "RegioJet"
    multi_passenger = True
    label = "RegioJet (vlaky i autobusy, CZ/SK/AT/DE/PL)"
    currency = "CZK"
    provides_prices = True
    provides_stops = True
    countries = ("CZ", "SK", "AT", "DE", "PL", "HU", "HR", "UA")
    czech_names = True
    referer = "https://regiojet.cz/"
    min_interval = 0.25

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._locations_lock = asyncio.Lock()

    # ---- pomocné -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "X-Currency": "CZK",
            "X-Lang": "cs",
            "Origin": "https://regiojet.cz",
            "Referer": "https://regiojet.cz/",
        }

    def _tariff(self, passenger: Passenger) -> str:
        """Namapuje cestujícího na tarif RegioJetu.

        Kódy jsou z ``/consts/tariffs`` a jejich názvy klamou: ``ISIC``
        je u RegioJetu **Senior od 65 let**, student s ISIC je
        ``CZECH_STUDENT_PASS_26``. Kód ``SENIOR`` neexistuje vůbec.
        """
        category = passenger.category
        if category is FareCategory.INFANT:
            return "ATTENDED_CHILD"                  # 0–5
        if category is FareCategory.CHILD and passenger.age < 13:
            return "CHILD_UNDER_12"                  # 6–12
        if category in (FareCategory.CHILD, FareCategory.YOUTH):
            return "CZECH_STUDENT_PASS_15"           # 13–17
        if category is FareCategory.STUDENT:
            return "CZECH_STUDENT_PASS_26"           # student 18–25
        if category is FareCategory.SENIOR:
            return "ISIC"                            # sic – senior 65+
        return "REGULAR"

    async def _locations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "locations")
        if cached:
            return cached
        # Zámek: obě strany dotazu (odkud/kam) se řeší paralelně a bez něj
        # by se 145 kB seznam stahoval dvakrát.
        async with self._locations_lock:
            cached = self.cache.get(self.name, "locations")
            if cached:
                return cached
            return await self._download_locations()

    async def _download_locations(self) -> list[dict[str, Any]]:
        data = await self.client.get_json(
            f"{API}/consts/locations", headers=self._headers(), referer=self.referer
        )
        if not isinstance(data, list):
            raise StationNotFoundError(self.name, "API vrátilo neočekávaný seznam lokalit")
        self.cache.set(self.name, "locations", data, ttl=LOCATIONS_TTL)
        return data

    async def resolve(self, place: str) -> tuple[str, str]:
        """Najde ID města (nebo stanice) podle názvu.

        Vrací ``(id, typ)`` kde typ je ``CITY`` nebo ``STATION``. Hledáme
        nejdřív mezi městy, pak mezi stanicemi – hraniční uzly typu
        "Břeclav" jsou totiž často jen stanice, ne město.
        """
        key = f"place:{place.lower()}"
        cached = self.cache.get(self.name, key)
        if cached:
            return tuple(cached)  # type: ignore[return-value]

        countries = await self._locations()
        city_names: list[str] = []
        city_ids: list[str] = []
        station_names: list[str] = []
        station_ids: list[str] = []

        for country in countries:
            for city in country.get("cities", []):
                for alias in [city.get("name", ""), *city.get("aliases", [])]:
                    if alias:
                        city_names.append(alias)
                        city_ids.append(str(city.get("id")))
                for station in city.get("stations", []):
                    for alias in [station.get("name", ""), station.get("fullname", "")]:
                        if alias:
                            station_names.append(alias)
                            station_ids.append(str(station.get("id")))

        idx = best_match(place, city_names, threshold=0.45)
        if idx is not None:
            result = (city_ids[idx], "CITY")
        else:
            idx = best_match(place, station_names, threshold=0.45)
            if idx is None:
                raise StationNotFoundError(self.name, f"stanice {place!r} nenalezena")
            result = (station_ids[idx], "STATION")

        self.cache.set(self.name, key, list(result), ttl=LOCATIONS_TTL)
        return result

    # ---- hlavní hledání ------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        (from_id, from_type), (to_id, to_type) = await asyncio.gather(
            self.resolve(query.origin), self.resolve(query.destination)
        )

        params: list[tuple[str, str]] = [
            ("fromLocationId", from_id),
            ("fromLocationType", from_type),
            ("toLocationId", to_id),
            ("toLocationType", to_type),
            ("departureDate", query.date.isoformat()),
        ]
        for passenger in query.passengers:
            params.append(("tariffs", self._tariff(passenger)))

        payload = await self.client.get_json(
            f"{API}/routes/search/simple",
            params=params,
            headers=self._headers(),
            referer=self.referer,
        )
        routes = payload.get("routes", []) if isinstance(payload, dict) else []
        if not routes:
            return []

        # Detaily (zastávky) tahá jen omezený počet spojů – jinak bychom na
        # API zbytečně tlačili. Berou se ty v časovém okně dotazu.
        in_window = [r for r in routes if _in_window(r, query)]
        detail_budget = int(self.options.get("detail_limit", 8))
        details = await self._fetch_details(in_window[:detail_budget])

        journeys: list[Journey] = []
        for route in routes:
            journey = self._to_journey(route, query, details.get(str(route.get("id"))))
            if journey is not None:
                journeys.append(journey)
        return journeys

    async def _fetch_details(self, routes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Stáhne detaily spojů paralelně; chyby jednotlivých detailů ignoruje.

        Endpoint vyžaduje i ID nástupní a výstupní stanice – bez nich vrací
        HTTP 400. Detail přináší cenové třídy a členění na úseky (přestupy).
        """
        if not routes:
            return {}

        async def one(route: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
            rid = str(route.get("id"))
            from_station = route.get("departureStationId")
            to_station = route.get("arrivalStationId")
            if from_station is None or to_station is None:
                return rid, None
            key = f"route:{rid}:{from_station}:{to_station}"
            cached = self.cache.get(self.name, key)
            if cached:
                return rid, cached
            try:
                data = await self.client.get_json(
                    f"{API}/routes/{rid}/simple",
                    params={"fromStationId": from_station, "toStationId": to_station},
                    headers=self._headers(),
                    referer=self.referer,
                )
            except Exception as exc:
                log.debug("%s: detail %s nedostupný (%s)", self.name, rid, exc)
                return rid, None
            self.cache.set(self.name, key, data, ttl=ROUTE_TTL)
            return rid, data

        pairs = await asyncio.gather(*(one(r) for r in routes))
        return {rid: data for rid, data in pairs if data}

    def _to_journey(
        self, route: dict[str, Any], query: SearchQuery, detail: dict[str, Any] | None
    ) -> Journey | None:
        departure = _parse_iso(route.get("departureTime"))
        arrival = _parse_iso(route.get("arrivalTime"))
        if departure is None or arrival is None:
            return None

        vehicles = route.get("vehicleTypes") or []
        mode = _VEHICLE_MODES.get(vehicles[0] if vehicles else "", Mode.BUS)

        legs = _detail_legs(detail)
        if not legs:
            # Bez detailu máme jen jeden úsek s koncovými stanicemi.
            legs = [
                Leg(
                    origin=_station_label(route, "departure") or query.origin,
                    destination=_station_label(route, "arrival") or query.destination,
                    departure=departure,
                    arrival=arrival,
                    mode=mode,
                    carrier="RegioJet",
                    line=_line_name(route, detail),
                    stops=[],
                    reservation_required=True,
                )
            ]

        fares = []
        status = FareStatus.UNKNOWN
        price_from = route.get("priceFrom")
        bookable = bool(route.get("bookable", True))
        free_seats = route.get("freeSeatsCount")
        # Vyprodaný spoj přijde s ``priceFrom: 0`` a ``pricesCount: 0`` –
        # ta nula není cena, ale "nic k prodeji". Brát ji jako cenu by
        # vyprodaný autobus postavilo na první místo jako nejlevnější.
        no_offer = route.get("pricesCount") == 0 or (not bookable and not price_from)

        if no_offer:
            status = FareStatus.SOLD_OUT if free_seats == 0 else FareStatus.NOT_BOOKABLE
        elif price_from is not None:
            fares.append(
                self.make_fare(
                    Decimal(str(price_from)),
                    "CZK",
                    label="od",
                    tariff=_TARIFF_NAMES.get(self._tariff(query.lead), ""),
                    bookable=bookable,
                    seats_left=free_seats if isinstance(free_seats, int) else None,
                )
            )
            status = FareStatus.PRICED if bookable else FareStatus.NOT_BOOKABLE
        elif free_seats == 0:
            status = FareStatus.SOLD_OUT

        # Více cenových tříd z detailu (např. low cost / standard / business).
        for cls in (detail or {}).get("priceClasses", []) or []:
            price = cls.get("price")
            if price is None:
                continue
            fares.append(
                self.make_fare(
                    Decimal(str(price)),
                    "CZK",
                    label=str(cls.get("seatClassKey") or cls.get("name") or "").strip(),
                    tariff=_TARIFF_NAMES.get(self._tariff(query.lead), ""),
                    bookable=bookable,
                )
            )

        return Journey(
            provider=self.name,
            legs=legs,
            fares=fares,
            fare_status=(status if fares or status in (FareStatus.SOLD_OUT, FareStatus.NOT_BOOKABLE)
                         else FareStatus.UNKNOWN),
            journey_id=str(route.get("id", "")),
            deep_link=_deep_link(route, query),
            raw={"route": route},
        )


def _format_line(code: Any) -> str:
    """Označení spoje. Kód už někdy prefix 'RJ' obsahuje – nepřidáváme ho dvakrát."""
    text = str(code or "").strip()
    if not text:
        return "RegioJet"
    return text if text.upper().startswith("RJ") else f"RJ {text}"


def _line_name(route: dict[str, Any], detail: dict[str, Any] | None) -> str:
    for section in (detail or {}).get("sections", []) or []:
        num = section.get("line", {}).get("lineNumber") or section.get("trainNumber")
        if num:
            return f"RJ {num}"
    vehicles = route.get("vehicleTypes") or []
    return "RegioJet vlak" if "TRAIN" in vehicles else "RegioJet bus"


def _station_label(node: dict[str, Any], side: str) -> str:
    """Složí čitelný název stanice, např. 'Praha hl.n.'.

    RegioJet dělí název na město a stanici ('Praha' + 'hl.n.'), takže
    samotné ``departureStationName`` by v UI bylo nesrozumitelné.
    """
    city = (node.get(f"{side}CityName") or "").strip()
    station = (node.get(f"{side}StationName") or "").strip()
    if city and station and station.lower() not in city.lower():
        return f"{city} {station}"
    return city or station


def _detail_legs(detail: dict[str, Any] | None) -> list[Leg]:
    """Z úseků detailu poskládá jednotlivé části jízdy včetně přestupů."""
    if not detail:
        return []
    legs: list[Leg] = []
    for section in detail.get("sections", []) or []:
        departure = _parse_iso(section.get("departureTime"))
        arrival = _parse_iso(section.get("arrivalTime"))
        if departure is None or arrival is None:
            continue
        origin = _station_label(section, "departure")
        destination = _station_label(section, "arrival")
        line = section.get("line") or {}
        code = line.get("code") or line.get("lineNumber") or section.get("id")
        vehicle = (section.get("vehicleType") or "").upper()
        stops = [
            Stop(name=origin, departure=departure,
                 platform=section.get("departurePlatform")),
            Stop(name=destination, arrival=arrival,
                 platform=section.get("arrivalPlatform")),
        ]
        legs.append(
            Leg(
                origin=origin,
                destination=destination,
                departure=departure,
                arrival=arrival,
                mode=_VEHICLE_MODES.get(vehicle, Mode.BUS),
                carrier="RegioJet",
                line=_format_line(code),
                stops=stops,
                reservation_required=True,
            )
        )
    return legs


def _deep_link(route: dict[str, Any], query: SearchQuery) -> str:
    return (
        "https://regiojet.cz/?departureDate="
        f"{query.date.isoformat()}&routeId={route.get('id', '')}"
    )


def _in_window(route: dict[str, Any], query: SearchQuery) -> bool:
    departure = _parse_iso(route.get("departureTime"))
    return departure is not None and query.within_window(departure)


def _parse_iso(value: Any) -> dt.datetime | None:
    """Rozparsuje ISO 8601 včetně zóny. RegioJet posílá '+02:00' i 'Z'."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        from ..models import DEFAULT_TZ

        parsed = parsed.replace(tzinfo=DEFAULT_TZ)
    return parsed
