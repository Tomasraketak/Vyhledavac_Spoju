"""Tarif PID – jednotlivé jízdné podle počtu projetých pásem.

Ceník (``vyhledavac/data/ids_pid.json.gz``) se čte ze stránky
https://pid.cz/jizdne-a-tarif/ při sestavení dat (``tools/build_stations.py``).
Ceny jsou za jízdenku v aplikaci PID Lítačka (papírová bývá o pár korun dražší).

Pravidla z pid.cz („Tarifní pásma PID“):

* pásma na sebe navazují v pořadí P, 0, B, 1, 2 … 13 – jízdenka platí pro
  daný počet *navazujících* pásem, tedy od pásma nástupu po pásmo výstupu;
* stanice ve dvou pásmech se počítá podle výhodnějšího;
* jízdenka pro 2 pásma platí jen ve vnějších pásmech (1–13);
* cesta jen po Praze -> jízdenka pro Prahu (30 nebo 90 minut);
* jízdenky PID nelze použít na cesty jen po území jiného kraje
  (to hlídá hack – potřebuje kraj zastávek).
"""

from __future__ import annotations

import gzip
import json
import logging
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from ..models import Passenger
from . import IdsFare

log = logging.getLogger(__name__)

SYSTEM = "PID"
DATA = Path(__file__).resolve().parent.parent / "data" / "ids_pid.json.gz"
URL = "https://pid.cz/jizdne-a-tarif/"
ORDER = ["P", "0", "B"] + [str(i) for i in range(1, 14)]
PRAHA = {"P", "0", "B"}
#: Kraje, kde jízdenka PID platí i pro cestu jen po jejich území.
HOME_REGIONS = ("Hlavní město Praha", "Středočeský kraj")


@lru_cache(maxsize=1)
def _data() -> dict | None:
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Ceník PID se nepodařilo načíst: %s", exc)
        return None


def category(passenger: Passenger) -> str | None:
    """'full' / 'reduced', ``None`` = zdarma (dítě do 6 let)."""
    if passenger.age < 6:
        return None
    if passenger.age < 18 or passenger.age >= 65 or passenger.is_student:
        return "reduced"
    return "full"


def zone_count(zone_from: str, zone_to: str) -> int | None:
    if zone_from not in ORDER or zone_to not in ORDER:
        return None
    return abs(ORDER.index(zone_from) - ORDER.index(zone_to)) + 1


def fares(zone_from: str, zone_to: str, passenger: Passenger) -> list[IdsFare]:
    data = _data()
    if data is None:
        return []
    kind = category(passenger)
    if kind is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)]
    out: list[IdsFare] = []
    if zone_from in PRAHA and zone_to in PRAHA and (6 <= passenger.age < 15 or passenger.age >= 65):
        # Přeprava za 0 Kč v Praze: děti 6–15 a senioři 65+ (s dokladem / aplikací v Lítačce)
        who = "dítě 6–15" if passenger.age < 15 else "senior 65+"
        out.append(IdsFare(SYSTEM, Decimal(0), None, f"{who} v Praze zdarma (doklad PID / Lítačka)",
                           zone_from, zone_to))
    if zone_from in PRAHA and zone_to in PRAHA:
        for minutes, row in data["praha"].items():
            if row.get(kind) is not None:
                out.append(IdsFare(SYSTEM, Decimal(row[kind]), int(row["minutes"]),
                                   f"Praha {minutes} min", zone_from, zone_to))
    count = zone_count(zone_from, zone_to)
    if count is None:
        return out
    # 2pásmová jízdenka neplatí v Praze; kratší cesta ve vnějších pásmech = 2 pásma
    inside_praha = zone_from in PRAHA or zone_to in PRAHA
    count = max(count, 3 if inside_praha else 2)
    for zones, row in sorted(data["zones"].items(), key=lambda kv: int(kv[0])):
        if int(zones) >= count and row.get(kind) is not None:
            label = "plnocenné" if kind == "full" else "zvýhodněné"
            out.append(IdsFare(SYSTEM, Decimal(row[kind]), int(row["minutes"]),
                               f"{zones} pásem, {label}", zone_from, zone_to))
    return out


def best_fare(zone_from: str, zone_to: str, passenger: Passenger,
              minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares(zone_from, zone_to, passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    options = [best_fare(a, b, passenger, minutes_needed) for a in zones_from for b in zones_to]
    return min((o for o in options if o is not None), key=lambda f: f.price, default=None)
