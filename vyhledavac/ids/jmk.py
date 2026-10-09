"""Tarif IDS JMK (Jihomoravský kraj) – jednorázové přestupní jízdenky.

Podle Tarifu IDS JMK platného od 1. 10. 2026
(https://content.idsjmk.cz/cenik/261001/Tarif.pdf, Příloha 1): cena a časová
platnost podle počtu projetých zón; Brno tvoří zóny 100 (centrum) a 101,
které se počítají jako dvě zóny, pokud jimi cesta vede. Ceny jsou z aplikace
POSEIDON (čl. 10) – o ~10 % nižší než papírové jízdenky a dostupné každému.
Senioři 70+ mají mimo Brno jízdenku 24 h za 40 Kč.

Zlevněná jízdenka A (polovina) platí pro děti do 15 let a – mimo cesty jen
po Brně linkami MHD – i pro mládež 15–18, studenty do 26 a osoby 65+. Vlak
není linka MHD, takže u vlaku platí A pro všechny tyto skupiny.

Zóny stanic jsou z GTFS IDS JMK (nejbližší zastávka u nádraží), cena se
počítá z pásem **všech** stanic na trase, jako u IDPK.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import Passenger
from . import IdsFare, zone_word
from .idpk import path_zones

SYSTEM = "IDS JMK"
URL = "https://www.idsjmk.cz/"

#: počet zón -> (platnost v min, základní, zlevněná A) – aplikace POSEIDON
TABLE = {
    2: (60, "22", "11"), 3: (90, "30", "15"), 4: (90, "37", "18.50"), 5: (120, "44", "22"),
    6: (120, "51", "25.50"), 7: (150, "58", "29"), 8: (150, "65", "32.50"),
    9: (180, "73", "36.50"), 10: (180, "80", "40"), 11: (210, "87", "43.50"),
    12: (210, "95", "47.50"),
}
ALL_ZONES = (240, "102", "51")
#: 24hodinová jízdenka pro seniory 70+ (všechny zóny)
SENIOR_70 = (1440, "40")


def reduced(passenger: Passenger) -> bool | None:
    """True = zlevněná A, False = základní, None = zdarma (do 6 let)."""
    if passenger.age < 6:
        return None
    return passenger.age < 18 or passenger.age >= 65 or passenger.is_student


def fares_for_path(zones: list[str], passenger: Passenger) -> list[IdsFare]:
    if not zones:
        return []
    first, last = zones[0], zones[-1]
    kind = reduced(passenger)
    if kind is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", first, last)]
    count = max(2, len(zones))
    label = "zlevněná A" if kind else "základní"
    out = []
    for zones_count, (minutes, base, half) in TABLE.items():
        if zones_count >= count:
            out.append(IdsFare(SYSTEM, Decimal(half if kind else base), minutes,
                               f"{zones_count} {zone_word(zones_count)}, {label}", first, last))
    minutes, base, half = ALL_ZONES
    out.append(IdsFare(SYSTEM, Decimal(half if kind else base), minutes,
                       f"všechny zóny, {label}", first, last))
    if passenger.age >= 70:
        minutes, price = SENIOR_70
        out.append(IdsFare(SYSTEM, Decimal(price), minutes, "24 h senior 70+", first, last))
    return out


def best_for_route(stops: list[tuple[str, ...]], passenger: Passenger,
                   minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares_for_path(path_zones(stops), passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    return best_for_route([zones_from, zones_to], passenger, minutes_needed)
