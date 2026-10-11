"""OUIGO – nízkonákladové rychlovlaky SNCF ve Francii (ouigo.com) a ve Španělsku (ouigo.com/es).

Obě země mají vlastní e-shop i vlastní API (stejný „middleware“, jiná data):

* Francie: web ``ventes.ouigo.com/fr-FR``, API ``https://mdw.api-fr.ouigo.com/api``;
* Španělsko: web ``ventas.ouigo.com/es-ES``, API ``https://mdw02.api-es.ouigo.com/api``
  (``mdw01`` používá vyhledávací widget na ouigo.com/es).

Postup (stejný pro obě země):

* ``POST Token/login`` s přihlašovacími údaji **webové aplikace** – stojí
  přímo v JS bundlu e-shopu (``VITE__API_USERNAME`` / ``VITE__API_PASSWORD``),
  nejde o uživatelský účet. Vrací JWT (platí 2–3 hodiny), dál se posílá jako
  ``Authorization: Bearer …`` spolu s ``Content-Language: fr-FR`` / ``es-ES``;
* ``GET Data/GetStations`` – všechny stanice (Francie ~100 vč. Bruselu,
  Španělsko 17): kód ``_u_i_c_station_code`` (UIC, nebo skupina města
  „PT1“ = Paris toutes gares, „LY1“, „MT1“ = Madrid – Todas las estaciones),
  ``synonyms`` a ``connected_stations`` (kam se odtud dá dojet);
* ``POST Sale/journeysearch`` – spoje celého dne
  ``{"outbound_date": "2026-10-14", "origin", "destination", "passengers": [{"type": …}]}``;
  ``price`` je **cena nejlevnější nabídky za celou skupinu** (OUIGO Essentiel /
  Esencial), ``packages`` dražší balíčky (Plus, Full), ``full`` = vyprodáno.

Cestující (ověřeno na odpovědích API):

* Francie: ``A`` od 12 let, ``C`` dítě do 11 let s vlastním místem (8 €),
  děti do 4 let jako „Toupti“ bez místa zdarma (``nb_babies``, cenu nemění);
* Španělsko: ``A`` od 14 let, ``C`` dítě 4–13 let (7 €), ``B`` batole 0–3 roky
  bez místa (0 €).

Na co si dát pozor:

* OUIGO jezdí jen uvnitř své sítě – Francie (+ Brusel) a Španělsko jsou
  oddělené, mezi nimi OUIGO nejezdí. Bez spojení podle ``connected_stations``
  se hledání vůbec neposílá. Spoj může mít i přestup mezi dvěma vlaky OUIGO
  (Barcelona → Sevilla přes Madrid, ``segments``); cena je za celou cestu.
* API vrací spoje celého dne bez ohledu na čas – filtrujeme sami.
* Přihlašovací údaje aplikace se mohou s novou verzí webu změnit – pak je
  potřeba přečíst je znovu z bundlu (``/assets/index-*.js`` na ventas,
  ``core-chunk-*.js`` na ventes).
* Na ventas.ouigo.com je Cloudflare Turnstile, ale jen u nákupu – vyhledání
  přes API ho nepotřebuje.
* Bez dospělého OUIGO neprodá – nejstaršího cestujícího počítáme jako dospělého.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Francii nebo ve
Španělsku.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

#: země → (API, web e-shopu, jazyk)
SITES = {
    "FR": ("https://mdw.api-fr.ouigo.com/api", "https://ventes.ouigo.com", "fr-FR"),
    "ES": ("https://mdw02.api-es.ouigo.com/api", "https://ventas.ouigo.com", "es-ES"),
}
#: Přihlašovací údaje webové aplikace z JS bundlu e-shopu (stejné pro obě země, říjen 2026).
APP_USER, APP_PASSWORD = "ouigo.web", "SquirelWeb!2020"
STATIONS_TTL = 7 * 24 * 3600
#: Země stanic v seznamu FR (country_code) → náš kód.
COUNTRY_CODES = {"FRA": "FR", "BEL": "BE", "ESP": "ES"}
#: Česká a jiná jména → název v seznamech OUIGO.
ALIASES = {"pariz": "paris", "valencie": "valencia", "seville": "sevilla", "saragossa": "zaragoza",
           "brusel": "bruxelles", "brussel": "bruxelles", "brussels": "bruxelles", "marsej": "marseille",
           "strasburk": "strasbourg", "cordova": "cordoba"}
#: Věkové hranice: (dospělý od, dítě od) – mladší jsou batolata bez místa.
AGES = {"FR": (12, 4), "ES": (14, 4)}


@PROVIDERS.register()
class OuigoProvider(HttpProvider):
    name = "OUIGO"
    label = "OUIGO – nízkonákladové TGV (jen po Francii a po Španělsku)"
    multi_passenger = True
    currency = "EUR"
    countries = ("FR", "ES", "BE")
    international_only = True
    referer = "https://ventes.ouigo.com/"
    min_interval = 0.4

    # ---- přístup k API --------------------------------------------------------------
    async def token(self, country: str) -> str:
        key = f"token:{country}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        api, web, _ = SITES[country]
        data = await self.client.post_json(f"{api}/Token/login",
                                           {"username": APP_USER, "password": APP_PASSWORD},
                                           referer=web + "/")
        token = (data or {}).get("token")
        if not token:
            raise TransientProviderError(self.name, "OUIGO nevydalo přístupový token")
        self.cache.set(self.name, key, token, ttl=token_ttl(token))
        return token

    async def _call(self, country: str, method: str, path: str, body: Any = None) -> Any:
        api, web, lang = SITES[country]
        headers = {"Authorization": f"Bearer {await self.token(country)}", "Content-Language": lang}
        if method == "GET":
            return await self.client.get_json(f"{api}/{path}", referer=web + "/", headers=headers)
        return await self.client.post_json(f"{api}/{path}", body, referer=web + "/", headers=headers)

    async def stations(self, country: str) -> list[dict[str, Any]]:
        key = f"stations:{country}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        data = await self._call(country, "GET", "Data/GetStations")
        items = compact_stations(data or [], country)
        if not items:
            raise TransientProviderError(self.name, "seznam stanic OUIGO je prázdný")
        self.cache.set(self.name, key, items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str, country: str) -> dict[str, Any] | None:
        return pick_station(place, await self.stations(country))

    # ---- hledání ---------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and not {"FR", "ES"} & set(known):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Francii či Španělsku")
        if {"FR", "ES"} <= set(known) or ("ES" in known and "BE" in known):
            raise StationNotFoundError(self.name, "OUIGO nejezdí mezi Francií a Španělskem")
        network = "ES" if "ES" in known else "FR" if {"FR", "BE"} & set(known) else None
        country, origin, destination = None, None, None
        for candidate in ([network] if network else ["FR", "ES"]):
            origin = await self.resolve(query.origin, candidate)
            destination = await self.resolve(query.destination, candidate)
            if origin and destination:
                country = candidate
                break
        if country is None or origin is None or destination is None:
            missing = query.origin if origin is None else query.destination
            raise StationNotFoundError(self.name, f"stanici {missing!r} OUIGO nezná")
        if origin["code"] == destination["code"] or not connected(origin, destination):
            raise StationNotFoundError(self.name, f"OUIGO mezi {origin['name']} a {destination['name']} "
                                                  "nejezdí")
        passengers, babies = passenger_types(query, country)
        body: dict[str, Any] = {
            "outbound_date": query.departure.astimezone(DEFAULT_TZ).strftime("%Y-%m-%d"),
            "inbound_date": None, "origin": origin["code"], "destination": destination["code"],
            "passengers": [{"type": t, "disability_type": "NH", "discount_cards": []} for t in passengers]}
        if country == "FR":
            body.update({"nb_babies": babies, "nb_animal": 0})
        data = await self._call(country, "POST", "Sale/journeysearch", body)
        if isinstance(data, dict) and data.get("error") and not data.get("outbound"):
            raise TransientProviderError(self.name, f"OUIGO hlásí chybu: {str(data['error'])[:120]}")
        link = deep_link(country, origin["code"], destination["code"], body["outbound_date"], passengers)
        journeys = []
        for item in (data or {}).get("outbound") or []:
            journey = self.to_journey(item, link)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def to_journey(self, item: dict[str, Any], link: str | None = None) -> Journey | None:
        legs = parse_legs(item)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN,
                          deep_link=link, journey_id=str(item.get("id") or ""))
        if item.get("full"):
            journey.fare_status = FareStatus.SOLD_OUT
            return journey
        price = item.get("price")
        if price is None:
            return journey
        amount = Decimal(str(price))
        tariff = next((str(p.get("name") or "") for p in item.get("packages") or []
                       if p.get("price") is not None and Decimal(str(p["price"])) == amount), "")
        seats = item.get("remaining_seats")
        journey.fares = [self.make_fare(amount, "EUR", label="OUIGO", tariff=tariff or "OUIGO",
                                        seats_left=int(seats) if isinstance(seats, (int, float)) else None,
                                        deep_link=link)]
        journey.fare_status = FareStatus.PRICED
        return journey


# ---- pomocné funkce (bez sítě, kvůli testům) ------------------------------------------
def token_ttl(token: str) -> int:
    """Doba platnosti JWT v sekundách (s rezervou 5 minut)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return max(60, int(claims["exp"]) - int(time.time()) - 300)
    except Exception:  # noqa: BLE001 - neznámý formát, krátká platnost
        return 30 * 60


def compact_stations(items: list[dict[str, Any]], country: str) -> list[dict[str, Any]]:
    out = []
    for s in items:
        code = str(s.get("_u_i_c_station_code") or "")
        if not code or not s.get("name"):
            continue
        group = bool(s.get("is_agglomeration")) or not code.isdigit()
        out.append({"code": code, "name": str(s["name"]),
                    "synonyms": [str(x) for x in s.get("synonyms") or []],
                    "connected": [str(x) for x in s.get("connected_stations") or []],
                    "parent": s.get("parent"), "group": group,
                    "country": COUNTRY_CODES.get(str(s.get("country_code") or ""), country)})
    return out


def pick_station(place: str, stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Skupina „toutes gares / Todas las estaciones“ pro město, jinak nejpodobnější stanice."""
    wanted = " ".join(w for w in (ALIASES.get(w, w) for w in normalize(place).split()) if w)
    if not wanted:
        return None
    for s in stations:
        if normalize(s["name"]) == wanted:
            return s
    city = [s for s in stations
            if wanted in (normalize(x) for x in s["synonyms"]) or normalize(s["name"]).startswith(wanted + " ")]
    if city:
        return min(city, key=lambda s: (not s["group"], bool(s.get("parent")), s["name"]))
    idx = best_match(wanted, [s["name"] for s in stations], threshold=0.45)
    return stations[idx] if idx is not None else None


def connected(origin: dict[str, Any], destination: dict[str, Any]) -> bool:
    if not origin.get("connected") and not destination.get("connected"):
        return True          # bez údajů se raději zeptáme
    return destination["code"] in origin.get("connected", []) or origin["code"] in destination.get("connected", [])


def passenger_types(query: SearchQuery, country: str) -> tuple[list[str], int]:
    """Typy cestujících pro API a počet batolat bez místa (FR je posílá zvlášť)."""
    adult_from, child_from = AGES[country]
    ages = sorted((p.age for p in query.passengers), reverse=True)
    types, babies = [], 0
    for age in ages:
        if age >= adult_from:
            types.append("A")
        elif age >= child_from:
            types.append("C")
        elif country == "FR":
            babies += 1
        else:
            types.append("B")
    if "A" not in types:
        # bez dospělého OUIGO neprodá – nejstarší jede jako dospělý
        if types:
            types[0] = "A"
        else:
            types, babies = ["A"], max(0, babies - 1)
    return types, babies


def deep_link(country: str, origin: str, destination: str, date: str, passengers: list[str]) -> str:
    _, web, lang = SITES[country]
    params = [("target", "calendar"), ("origin", origin), ("destination", destination),
              ("outboundDate", date)]
    for i, t in enumerate(passengers):
        params += [(f"passengers[{i}][type]", t), (f"passengers[{i}][disability_type]", "NH")]
    return f"{web}/{lang}/_deeplink/journey-search?{urlencode(params)}"


def parse_legs(item: dict[str, Any]) -> list[Leg]:
    legs = []
    for seg in item.get("segments") or []:
        dep_st, arr_st = seg.get("departure_station") or {}, seg.get("arrival_station") or {}
        dep, arr = _time(dep_st.get("departure_timestamp")), _time(arr_st.get("arrival_timestamp"))
        if dep is None or arr is None:
            continue
        service = seg.get("service_type") or {}
        number = str(seg.get("service_name") or "").lstrip("0")
        bus = "BUS" in str(service.get("modality") or "").upper()
        origin, destination = str(dep_st.get("name") or ""), str(arr_st.get("name") or "")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN,
                        carrier=str(service.get("name") or "OUIGO"), line=f"OUIGO {number}".strip(),
                        reservation_required=True,
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
