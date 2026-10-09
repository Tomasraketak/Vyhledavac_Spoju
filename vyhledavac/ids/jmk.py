"""Tarif IDS JMK (Jihomoravský kraj) – jednorázové přestupní jízdenky.

Podle Ceníku jízdného IDS JMK (https://content.idsjmk.cz/cenik/Cenik.pdf):
cena a časová platnost podle počtu projetých zón; Brno tvoří zóny 100
(centrum) a 101, které se počítají jako dvě zóny, pokud jimi cesta vede.

Zlevněná jízdenka A (polovina) platí pro děti do 15 let a – mimo cesty jen
po Brně linkami MHD – i pro mládež 15–18, studenty do 26 a osoby 65+. Vlak
není linka MHD, takže u vlaku platí A pro všechny tyto skupiny.

Zóny stanic jsou z GTFS IDS JMK (nejbližší zastávka u nádraží), cena se
počítá z pásem **všech** stanic na trase, jako u IDPK.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import Passenger
from . import IdsFare
from .idpk import path_zones

SYSTEM = "IDS JMK"
URL = "https://www.idsjmk.cz/"

#: počet zón -> (platnost v min, základní, zlevněná A)
TABLE = {
    2: (60, 25, 12), 3: (90, 33, 16), 4: (90, 41, 20), 5: (120, 49, 24), 6: (120, 57, 28),
    7: (150, 65, 32), 8: (150, 73, 36), 9: (180, 81, 40), 10: (180, 89, 44),
    11: (210, 97, 48), 12: (210, 105, 52),
}
ALL_ZONES = (240, 113, 56)


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
                               f"{zones_count} zón, {label}", first, last))
    minutes, base, half = ALL_ZONES
    out.append(IdsFare(SYSTEM, Decimal(half if kind else base), minutes,
                       f"všechny zóny, {label}", first, last))
    return out


def best_for_route(stops: list[tuple[str, ...]], passenger: Passenger,
                   minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares_for_path(path_zones(stops), passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    return best_for_route([zones_from, zones_to], passenger, minutes_needed)
