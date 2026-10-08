"""Klient protokolu HAFAS (mgate.exe).

HAFAS pohání jízdní řády velké části evropských železnic (ÖBB, DB, SBB, …).
Pro nás je zásadní, že vrací **kompletní seznam zastávek** každého vlaku –
přesně to, co potřebuje algoritmus lomených jízdenek, a co se z HTML
stránek tahalo jen krkolomně.

Modul je záměrně obecný: nový dopravce na HAFASu = jen nová konfigurace
(endpoint + client + auth), žádný nový parser.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import ProviderError, StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, Journey, Leg, Mode, Stop

log = logging.getLogger(__name__)

# Třídy produktů HAFAS -> náš Mode. Bitové masky se liší profil od profilu,
# tady mapujeme podle názvu kategorie, což je stabilnější.
_CATEGORY_MODES = {
    "ice": Mode.TRAIN, "ic": Mode.TRAIN, "ec": Mode.TRAIN, "rj": Mode.TRAIN,
    "rjx": Mode.TRAIN, "nj": Mode.TRAIN, "en": Mode.TRAIN, "d": Mode.TRAIN,
    "r": Mode.TRAIN, "re": Mode.TRAIN, "rb": Mode.TRAIN, "s": Mode.TRAIN,
    "os": Mode.TRAIN, "sp": Mode.TRAIN, "ex": Mode.TRAIN, "zug": Mode.TRAIN,
    "bus": Mode.BUS, "b": Mode.BUS,
    "tram": Mode.TRAM, "str": Mode.TRAM,
    "u": Mode.METRO, "ubahn": Mode.METRO, "metro": Mode.METRO,
    "schiff": Mode.FERRY, "f": Mode.FERRY,
}


@dataclass
class HafasProfile:
    """Konfigurace jednoho HAFAS endpointu."""

    endpoint: str
    client: dict[str, Any]
    auth: dict[str, Any]
    version: str = "1.57"
    ext: str | None = None
    language: str = "de"
    timezone: Any = DEFAULT_TZ
    user_agent: str = "Dalvik/2.1.0 (Linux; U; Android 13)"
    extra: dict[str, Any] = field(default_factory=dict)


class HafasClient:
    """Minimalistický HAFAS klient – jen to, co potřebujeme."""

    def __init__(self, provider: str, profile: HafasProfile, http, cache=None) -> None:
        self.provider = provider
        self.profile = profile
        self.http = http
        self.cache = cache

    # ---- nízká úroveň --------------------------------------------------------
    async def call(self, method: str, req: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
        service: dict[str, Any] = {"meth": method, "req": req}
        if cfg:
            service["cfg"] = cfg

        body: dict[str, Any] = {
            "lang": self.profile.language,
            "svcReqL": [service],
            "client": self.profile.client,
            "ver": self.profile.version,
            "auth": self.profile.auth,
        }
        if self.profile.ext:
            body["ext"] = self.profile.ext
        body.update(self.profile.extra)

        payload = await self.http.post_json(
            self.profile.endpoint,
            body,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "User-Agent": self.profile.user_agent,
                "Accept": "application/json",
            },
        )
        if not isinstance(payload, dict):
            raise TransientProviderError(self.provider, "HAFAS vrátil neočekávanou odpověď")

        if payload.get("err") not in (None, "OK"):
            raise TransientProviderError(
                self.provider, f"HAFAS chyba {payload.get('err')}: {payload.get('errTxt', '')}"
            )
        services = payload.get("svcResL") or []
        if not services:
            raise TransientProviderError(self.provider, "HAFAS neposlal žádnou službu")
        first = services[0]
        err = first.get("err")
        if err not in (None, "OK"):
            # H9380/H9360 = nesmyslné zadání, ne výpadek – opakovat nemá smysl.
            raise ProviderError(
                self.provider, f"HAFAS {method} chyba {err}: {first.get('errTxt', '')}"
            )
        return first.get("res") or {}

    # ---- vysoká úroveň -------------------------------------------------------
    async def find_location(self, name: str) -> dict[str, Any]:
        """Najde stanici podle názvu, výsledek se cachuje."""
        key = f"hafas:loc:{name.lower()}"
        if self.cache is not None:
            cached = self.cache.get(self.provider, key)
            if cached:
                return cached

        res = await self.call(
            "LocMatch",
            {"input": {"loc": {"type": "S", "name": f"{name}?"}, "maxLoc": 8, "field": "S"}},
        )
        locations = ((res.get("match") or {}).get("locL")) or []
        if not locations:
            raise StationNotFoundError(self.provider, f"stanice {name!r} nenalezena")

        from ..normalize import best_match

        names = [str(loc.get("name", "")) for loc in locations]
        idx = best_match(name, names, threshold=0.3)
        chosen = locations[idx if idx is not None else 0]
        if self.cache is not None:
            self.cache.set(self.provider, key, chosen, ttl=30 * 24 * 3600)
        return chosen

    async def trip_search(
        self,
        origin: dict[str, Any],
        destination: dict[str, Any],
        when: dt.datetime,
        *,
        count: int = 6,
        passlist: bool = True,
        products: int | None = None,
        tariff: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "depLocL": [{"type": "S", "lid": origin["lid"]}],
            "arrLocL": [{"type": "S", "lid": destination["lid"]}],
            "outDate": when.strftime("%Y%m%d"),
            "outTime": when.strftime("%H%M%S"),
            "getPasslist": passlist,
            "getPolyline": False,
            "numF": count,
        }
        if products is not None:
            req["jnyFltrL"] = [{"type": "PROD", "mode": "INC", "value": str(products)}]
        if tariff:
            req["trfReq"] = tariff
        return await self.call("TripSearch", req, cfg={"polyEnc": "GPA"})


def parse_connections(res: dict[str, Any], provider: str, tz=DEFAULT_TZ) -> list[Journey]:
    """Převede HAFAS odpověď na naše ``Journey``."""
    common = res.get("common") or {}
    locations = common.get("locL") or []
    products = common.get("prodL") or []
    journeys: list[Journey] = []

    for conn in res.get("outConL") or []:
        base_date = _parse_date(conn.get("date"))
        if base_date is None:
            continue
        legs = _parse_sections(conn, base_date, locations, products, tz,
                               common.get("opL") or [])
        if not legs:
            continue
        journeys.append(
            Journey(
                provider=provider,
                legs=legs,
                journey_id=str(conn.get("cid") or conn.get("ctxRecon", ""))[:64],
                raw={"conn": conn},
            )
        )
    return journeys


def _parse_sections(
    conn: dict[str, Any],
    base_date: dt.date,
    locations: list[dict[str, Any]],
    products: list[dict[str, Any]],
    tz,
    operators: list[dict[str, Any]] | None = None,
) -> list[Leg]:
    legs: list[Leg] = []
    for section in conn.get("secL") or []:
        dep_node = section.get("dep") or {}
        arr_node = section.get("arr") or {}
        departure = _parse_time(base_date, dep_node.get("dTimeR") or dep_node.get("dTimeS"), tz)
        arrival = _parse_time(base_date, arr_node.get("aTimeR") or arr_node.get("aTimeS"), tz)
        if departure is None or arrival is None:
            continue
        # Přes půlnoc: příjezd nikdy nesmí být před odjezdem.
        if arrival < departure:
            arrival += dt.timedelta(days=1)

        origin = _loc_name(locations, dep_node.get("locX"))
        destination = _loc_name(locations, arr_node.get("locX"))
        sec_type = section.get("type")

        if sec_type == "WALK" or sec_type == "TRSF":
            legs.append(
                Leg(
                    origin=origin, destination=destination,
                    departure=departure, arrival=arrival,
                    mode=Mode.WALK, carrier="", line="přesun",
                    stops=[Stop(name=origin, departure=departure),
                           Stop(name=destination, arrival=arrival)],
                )
            )
            continue

        jny = section.get("jny") or {}
        product = _product(products, jny.get("prodX"))
        line = str(product.get("name") or product.get("line") or "").strip() or "vlak"
        category = str(
            (product.get("prodCtx") or {}).get("catOut")
            or product.get("catOutS")
            or ""
        ).strip().lower()
        mode = _CATEGORY_MODES.get(category, Mode.TRAIN)

        stops = _parse_stops(jny.get("stopL") or [], base_date, locations, tz)
        if not stops:
            stops = [Stop(name=origin, departure=departure),
                     Stop(name=destination, arrival=arrival)]

        legs.append(
            Leg(
                origin=origin,
                destination=destination,
                departure=departure,
                arrival=arrival,
                mode=mode,
                carrier=_operator(operators or [], product.get("oprX")),
                line=line,
                stops=stops,
            )
        )
    return legs


def _operator(operators: list[dict[str, Any]], index: Any) -> str:
    """Jméno provozovatele ("České dráhy", "ÖBB") – ne název vlaku.

    Dřív se sem dával název produktu ("EX 611"), takže filtr dopravců
    v rozhraní nabízel čísla vlaků.
    """
    if not (isinstance(index, int) and 0 <= index < len(operators)):
        return ""
    from ..normalize import normalize

    name = str(operators[index].get("name") or "").strip()
    key = normalize(re.sub(r",?\s*(a\.\s*s\.|s\.\s*r\.\s*o\.|gmbh|ag)\s*$", "", name, flags=re.I))
    return _OPERATOR_NAMES.get(key, name)


#: Sjednocená jména provozovatelů, ať filtr dopravců nemá tři podoby ČD.
_OPERATOR_NAMES = {
    "ceske drahy": "ČD",
    "nahreisezug": "ÖBB",
    "osterreichische bundesbahnen": "ÖBB",
    "obb personenverkehr": "ÖBB",
    "zeleznicna spolocnost slovensko": "ZSSK",
    "regiojet": "RegioJet",
    "leo express": "Leo Express",
    "db fernverkehr": "DB",
    "westbahn": "WESTbahn",
}


def _parse_stops(raw_stops, base_date: dt.date, locations, tz) -> list[Stop]:
    stops: list[Stop] = []
    for entry in raw_stops:
        name = _loc_name(locations, entry.get("locX"))
        if not name:
            continue
        arrival = _parse_time(base_date, entry.get("aTimeR") or entry.get("aTimeS"), tz)
        departure = _parse_time(base_date, entry.get("dTimeR") or entry.get("dTimeS"), tz)
        stops.append(
            Stop(
                name=name,
                arrival=arrival,
                departure=departure,
                platform=entry.get("aPlatfR") or entry.get("dPlatfR")
                or entry.get("aPlatfS") or entry.get("dPlatfS"),
            )
        )
    return stops


def _loc_name(locations: list[dict[str, Any]], index: Any) -> str:
    if not isinstance(index, int) or index < 0 or index >= len(locations):
        return ""
    return str(locations[index].get("name") or "")


def _product(products: list[dict[str, Any]], index: Any) -> dict[str, Any]:
    if not isinstance(index, int) or index < 0 or index >= len(products):
        return {}
    return products[index]


def _parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or len(value) != 8:
        return None
    try:
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return None


def _parse_time(base_date: dt.date, value: Any, tz) -> dt.datetime | None:
    """HAFAS čas je 'HHMMSS', u spojů přes půlnoc 'DDHHMMSS' s posunem dnů.

    Právě tenhle posun původní kód neznal – proto se noční spoje počítaly
    modulem 1440 a vycházely nesmysly.
    """
    if not isinstance(value, str) or not value.isdigit():
        return None
    day_offset = 0
    if len(value) == 8:
        day_offset = int(value[:2])
        value = value[2:]
    if len(value) != 6:
        return None
    try:
        moment = dt.datetime(
            base_date.year, base_date.month, base_date.day,
            int(value[:2]), int(value[2:4]), int(value[4:6]), tzinfo=tz,
        )
    except ValueError:
        return None
    return moment + dt.timedelta(days=day_offset)
