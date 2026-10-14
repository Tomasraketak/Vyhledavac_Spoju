"""Tarif IDESKA – jednotlivé jízdné podle oficiální kalkulačky.

Data (``vyhledavac/data/ids_ideska.json.gz``) jsou výtah z
https://kalkulacka.ideska.info/jikord.json, který sestavuje
``tools/build_stations.py``. Výpočet věrně kopíruje kalkulačku:

1. vzdálenost dvou zón v tarifních jednotkách (``tariffUnits``),
2. podle ní tarifní pásmo (``tariffZones``; cesta v rámci městské zóny
   má vlastní pásmo),
3. cena a doba platnosti pro profil cestujícího (plné, 6–18 let,
   student 18–26, senior 65+),
4. u dlouhých relací i síťová jízdenka na 5 hodin, je-li levnější.
"""

from __future__ import annotations

import gzip
import json
import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..models import Passenger
from . import IdsFare

log = logging.getLogger(__name__)

SYSTEM = "IDESKA"
DATA = Path(__file__).resolve().parent.parent / "data" / "ids_ideska.json.gz"
URL = "https://kalkulacka.ideska.info/"


@lru_cache(maxsize=1)
def _data() -> dict[str, Any] | None:
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Tarif IDESKA se nepodařilo načíst: %s", exc)
        return None
    data["_relations"] = set(data.get("singleNetworkRelations", ()))
    return data


def profile(passenger: Passenger) -> str | None:
    """Profil cestujícího v tarifu IDESKA; ``None`` = cestuje zdarma (do 6 let)."""
    if passenger.age < 6:
        return None
    if passenger.age < 18:
        return "2"
    if passenger.age >= 65:
        return "50"
    if passenger.is_student:
        return "3"
    return "1"


def _key(a: str, b: str) -> str:
    x, y = int(a), int(b)
    return f"{min(x, y)}_{max(x, y)}"


def _tariff_zone(data: dict, zone_from: str, zone_to: str) -> tuple[str | None, str]:
    """Tarifní pásmo pro dvojici zón (kopie ``calculatePricesLocal``)."""
    key = _key(zone_from, zone_to)
    zones = data["tariffZones"]
    if zone_from == zone_to:
        for tz_id, tz in zones.items():
            if tz.get("zoneFrom") == zone_from and tz.get("zoneTo") == zone_to:
                return tz_id, key
        units = 0
    else:
        units = data["tariffUnits"].get(key)
        if units is None:
            return None, key
    best, best_to = None, None
    for tz_id, tz in zones.items():
        limit = tz.get("unitCountTo")
        if tz.get("zoneFrom") is None and limit is not None and units <= limit:
            if best_to is None or limit < best_to:
                best, best_to = tz_id, limit
    return best, key


def fares(zone_from: str, zone_to: str, passenger: Passenger) -> list[IdsFare]:
    """Všechny jednotlivé jízdenky IDESKA mezi dvěma zónami pro daného cestujícího."""
    data = _data()
    if data is None:
        return []
    cp = profile(passenger)
    if cp is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)]
    if zone_from not in data["zoneCountries"] or zone_to not in data["zoneCountries"]:
        return []
    countries, areas = data["zoneCountries"], data["zoneAreas"]
    c1, c2 = countries.get(zone_from, "CZ"), countries.get(zone_to, "CZ")
    a1, a2 = areas.get(zone_from, "0"), areas.get(zone_to, "0")
    # Mezi dvěma zónami přesahů (obě mimo vlastní území systému) zónová jízdenka neplatí.
    invalid_zonal = (c1 != "CZ" and c2 != "CZ") or (c1 == c2 == "CZ" and a1 != "0" and a2 != "0")

    tz_id, key = _tariff_zone(data, zone_from, zone_to)
    if tz_id is None:
        return []
    single_network = data["tariffZones"][tz_id].get("singleNetworkTariff", False)
    network_zone = data.get("singleNetworkExceptions", {}).get(key, "200")
    has_network = key in data["_relations"]

    out: list[IdsFare] = []
    for t in data["tariffs"]:
        if t["cp"] != cp:
            continue
        if not t["networkTariff"]:
            info = None if (single_network or invalid_zonal) else t["prices"].get(tz_id)
        else:
            # síťová na 5 h má cenu u relace (200/206/207) a kalkulačka ji nabízí jen
            # na delší relace; Jihočeská 24/72/120 (201–204) platí po celé síti vždy –
            # ta s Bavorskem/Rakouskem jen na cestu do té země
            info = t["prices"].get(network_zone) if has_network else None
            if info is None and not set(t["prices"]) & {"200", "206", "207"} \
                    and _network_fits(t["name"], {c1, c2} - {"CZ"}):
                info = next(iter(t["prices"].values()), None)
        if not info or info.get("price") is None or float(info["price"]) <= 1:
            continue    # kalkulačka cenu 1 Kč značí jako chybu v datech
        minutes = info.get("time")
        if minutes is None and t.get("dayValidity"):
            minutes = int(t["dayValidity"]) * 24 * 60
        out.append(IdsFare(SYSTEM, Decimal(str(info["price"])), minutes, t["name"], zone_from, zone_to))
    return out


def _network_fits(name: str, abroad: set[str]) -> bool:
    """Síťová jízdenka „+ Bavorsko / + Rakousko“ jen na cestu do té země."""
    lower = name.lower()
    wants = {c for word, c in (("bavorsko", "DE"), ("rakousko", "AT")) if word in lower}
    return wants == abroad if wants else not abroad


def best_fare(zone_from: str, zone_to: str, passenger: Passenger,
              minutes_needed: float) -> IdsFare | None:
    """Nejlevnější jízdenka, která platí aspoň ``minutes_needed`` minut."""
    usable = [f for f in fares(zone_from, zone_to, passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    """Místo může ležet ve dvou zónách (hranice) – vybere se výhodnější kombinace."""
    options = [best_fare(a, b, passenger, minutes_needed) for a in zones_from for b in zones_to]
    return min((o for o in options if o is not None), key=lambda f: f.price, default=None)
