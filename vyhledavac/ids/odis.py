"""Tarif ODIS (Ostravský dopravní integrovaný systém, Moravskoslezský kraj).

Jednotlivé jízdné REGION se od 14. 12. 2025 počítá z tarifních kilometrů
(kodis.cz – „Plné jednotlivé jízdné“, „Jízdenky pro děti 6–15 let“, „pro
studenty od 15 do 26 let“, „pro občany od 65 / 70 let“; odis.cz –
„Informace o jednotlivém jízdném“):

* elektronická jízdenka (ODISka, ZETKA, ODISapka): plné 10 Kč + 1,50 Kč za
  každý tarifní km, zvýhodněné (děti 6–15, 15–18, studenti 15–26, 65+)
  5 Kč + 0,75 Kč/km; zaokrouhleno na desetihaléře;
* papírová: plné 18 Kč + 1,70 Kč/km, zvýhodněné 9 Kč + 0,85 Kč/km (dolů na koruny)
  – počítáme elektronickou, je levnější a koupí ji každý v aplikaci;
* děti do 6 let zdarma;
* platí v regionálních autobusech a ve vlacích ČD, GW Train Regio, MBM rail
  a RegioJet (linka R8) zařazených do ODIS.

Tarifní kilometry bereme z detailu spoje v IDOS (vzdálenost zastávek po
trase spoje) – u vlaku jsou to kilometry trati, které se s tarifními
kilometry ČD shodují; u autobusu kilometry z licenčního jízdního řádu.
Jinde (hack „jízdenky IDS“ nad spoji ČD bez kilometrů) se ODIS neocení.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..models import Passenger
from . import IdsFare

SYSTEM = "ODIS"
URL = "https://www.odis.cz/zakladni-informace-o-jizdnem"

FULL = (Decimal(10), Decimal("1.50"))
REDUCED = (Decimal(5), Decimal("0.75"))
#: Dopravci, ve kterých platí jízdenka ODIS ve vlaku.
TRAIN_CARRIERS = ("České dráhy", "GW Train", "MBM rail", "RegioJet")


def category(passenger: Passenger) -> str | None:
    if passenger.age < 6:
        return None
    if passenger.age < 18 or passenger.age >= 65 or (passenger.is_student and passenger.age <= 26):
        return "reduced"
    return "full"


def fare_for_km(km: float, passenger: Passenger, zone_from: str = "", zone_to: str = "") -> IdsFare | None:
    """Jednotlivá elektronická jízdenka REGION na ``km`` tarifních kilometrů."""
    if km is None or km < 0:
        return None
    kind = category(passenger)
    if kind is None:
        return IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)
    base, per_km = FULL if kind == "full" else REDUCED
    distance = Decimal(str(round(km)))
    price = (base + per_km * distance).quantize(Decimal("0.1"), ROUND_HALF_UP)
    label = "plné" if kind == "full" else "zvýhodněné"
    return IdsFare(SYSTEM, price, None, f"REGION {distance} km, {label}", zone_from, zone_to)
