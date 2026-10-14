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

Slevy (Tarif PID od 1. 1. 2026, https://pid.cz/neplatvicnezmusis/):

* v regionu (cesta zasahuje do vnějšího pásma) – zvýhodněné jízdné pro děti
  6–15, juniory 15–18, studenty 18–26 a seniory 65+;
* jen po Praze – děti 6–15 a senioři 65+ jezdí zdarma, zvýhodněnou
  jednotlivou jízdenku mají jen senioři 60–65; junioři a studenti platí
  plné jízdné (slevu mají jen na předplatné).
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
    """Jízdné v regionu: 'full' / 'reduced', ``None`` = zdarma (dítě do 6 let)."""
    if passenger.age < 6:
        return None
    if passenger.age < 18 or passenger.age >= 65 or passenger.is_student:
        return "reduced"
    return "full"


def city_category(passenger: Passenger) -> str | None:
    """Jízdné jen po Praze: zdarma do 15 a od 65 let, sleva jen pro seniory 60–65."""
    if passenger.age < 15 or passenger.age >= 65:
        return None
    return "reduced" if passenger.age >= 60 else "full"


#: 24hodinové jízdenky PID od 1. 1. 2026 (elektronicky v PID Lítačce; pid.cz/neplatvicnezmusis):
#: (plné, zlevněné) Kč; ``None`` = zlevněná neexistuje (platí se plná).
DAY_PRAHA = (140, 70)               # P, 0, B; zlevněná jen senioři 60–65
DAY_OUTER_4 = (96, 48)              # libovolná 4 navazující vnější pásma
DAY_REGION = (168, 84)              # Krajská – všechna vnější pásma
DAY_NETWORK = (288, None)           # Celosíťová – Praha i všechna vnější pásma


def day_fares(zone_from: str, zone_to: str, kind: str) -> list[IdsFare]:
    """24hodinové jízdenky, které pokryjí cestu mezi pásmy (tam i zpět)."""
    def fare(prices: tuple[int, int | None], name: str) -> IdsFare:
        price = prices[1] if kind == "reduced" and prices[1] is not None else prices[0]
        label = "plnocenná" if price == prices[0] else "zlevněná"
        return IdsFare(SYSTEM, Decimal(price), 24 * 60, f"{name} 24 hodin, {label}", zone_from, zone_to)

    in_praha = [z in PRAHA for z in (zone_from, zone_to)]
    if all(in_praha):
        return [fare(DAY_PRAHA, "Praha"), fare(DAY_NETWORK, "Celosíťová")]
    if any(in_praha):
        return [fare(DAY_NETWORK, "Celosíťová")]
    out = [fare(DAY_REGION, "Krajská"), fare(DAY_NETWORK, "Celosíťová")]
    count = zone_count(zone_from, zone_to)
    if count is not None and count <= 4:
        out.append(fare(DAY_OUTER_4, "4 vnější pásma"))
    return out


def zone_count(zone_from: str, zone_to: str) -> int | None:
    if zone_from not in ORDER or zone_to not in ORDER:
        return None
    return abs(ORDER.index(zone_from) - ORDER.index(zone_to)) + 1


def fares(zone_from: str, zone_to: str, passenger: Passenger) -> list[IdsFare]:
    data = _data()
    if data is None:
        return []
    city = zone_from in PRAHA and zone_to in PRAHA
    kind = city_category(passenger) if city else category(passenger)
    if kind is None:
        if passenger.age < 6:
            return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)]
        # Přeprava za 0 Kč v Praze: děti 6–15 a senioři 65+ (s dokladem / aplikací v Lítačce)
        who = "dítě 6–15" if passenger.age < 15 else "senior 65+"
        return [IdsFare(SYSTEM, Decimal(0), None, f"{who} v Praze zdarma (doklad PID / Lítačka)",
                        zone_from, zone_to)]
    out: list[IdsFare] = []
    if city:
        for minutes, row in data["praha"].items():
            if row.get(kind) is not None:
                out.append(IdsFare(SYSTEM, Decimal(row[kind]), int(row["minutes"]),
                                   f"Praha {minutes} min", zone_from, zone_to))
    out += day_fares(zone_from, zone_to, kind)
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
