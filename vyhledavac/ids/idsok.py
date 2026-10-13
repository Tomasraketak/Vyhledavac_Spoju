"""Tarif IDSOK (Olomoucký kraj) – jednotlivé jízdenky podle počtu projetých zón.

Podle Tarifu IDSOK platného od 1. 10. 2026 (Část 6, Ceník A a B):

* **Ceník A** – trasa mimo zónu 71 Olomouc; cena podle počtu projetých zón
  (každá zóna jen jednou, včetně nástupní a výstupní).
* **Ceník B** – trasa vede zónou 71 Olomouc (s MHD Olomouc); počítá se
  „71 + počet ostatních zón“.
* Zóna **710** pokrývá totéž území jako 71, ale jen pro příměstskou dopravu
  (bez MHD Olomouc) a platí pro ni Ceník A. Pro cestu vlakem přes Olomouc
  tedy stačí levnější jízdenka s 710.

Ceny jsou elektronické (MobilOK, Můj vlak) – papírové jsou o něco dražší.
Zvýhodněné jízdné: junioři 6–18, žáci a studenti 18–26, senioři 65+; děti do
6 let zdarma. Platnost: hodnota pro pracovní dny (je kratší nebo stejná).
Jízdenka jen pro jednu zónu s MHD (1, 11, 41, 51, 61, 71) se v Ceníku A
neprodává – tam platí ceník MHD; pro Olomouc (zóna 71) ho známe: 25 Kč,
zlevněná 12 Kč, od 65 let zdarma (DPMO od 1. 6. 2025).
"""

from __future__ import annotations

from decimal import Decimal

from ..models import Passenger
from . import IdsFare, zone_word
from .idpk import path_zones

SYSTEM = "IDSOK"
URL = "https://www.idsok.cz/"

OLOMOUC, OLOMOUC_REGIONAL = "71", "710"
#: Jízdenka MHD Olomouc (DPMO, ceník od 1. 6. 2025, předprodej / aplikace).
MHD_FULL, MHD_REDUCED = Decimal(25), Decimal(12)
MHD_ZONES = {"1", "11", "41", "51", "61", "71", "710"}

# Ceník A: počet zón 1..24 -> plné, zvýhodněné (elektronicky), platnost v pracovní dny
A_FULL = [10, 18, 25, 32, 40, 48, 55, 62, 71, 80, 87, 96, 104, 112, 120, 128, 139,
          154, 154, 154, 154, 154, 154, 154]
A_REDUCED = [2, 4, 6, 8, 10, 12, 13, 15, 17, 20, 21, 24, 26, 28, 30, 32, 34,
             38, 38, 38, 38, 38, 38, 38]
A_MINUTES = [40, 45, 70, 85, 100, 115, 130, 145, 190, 190] + [250] * 14

# Ceník B: 71 + počet ostatních zón 1..23
B_FULL = [35, 43, 50, 57, 65, 73, 80, 87, 96, 105, 112, 121, 129, 137, 145, 153, 164,
          179, 179, 179, 179, 179, 179]
B_JUNIOR = [14, 16, 18, 20, 22, 24, 25, 27, 29, 32, 33, 36, 38, 40, 42, 44, 46,
            50, 50, 50, 50, 50, 50]
B_STUDENT = [27, 29, 31, 33, 35, 37, 38, 40, 42, 45, 46, 49, 51, 53, 55, 57, 59,
             63, 63, 63, 63, 63, 63]
B_SENIOR = [2, 4, 6, 8, 10, 12, 13, 15, 17, 20, 21, 24, 26, 28, 30, 32, 34,
            38, 38, 38, 38, 38, 38]
B_MINUTES = [60, 75, 90, 105, 120, 135, 150, 165, 210, 210] + [270] * 13


def category(passenger: Passenger) -> str | None:
    """None = zdarma (do 6 let), jinak junior / student / senior / full."""
    if passenger.age < 6:
        return None
    if passenger.age < 18:
        return "junior"
    if passenger.age >= 65:
        return "senior"
    if passenger.is_student:
        return "student"
    return "full"


def fares_for_path(zones: list[str], passenger: Passenger) -> list[IdsFare]:
    if not zones:
        return []
    first, last = zones[0], zones[-1]
    kind = category(passenger)
    if kind is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", first, last)]
    label = "plné" if kind == "full" else "zvýhodněné"
    out: list[IdsFare] = []
    # Ceník A – s regionální zónou 710 místo 71 (bez MHD Olomouc)
    regional = list(dict.fromkeys(OLOMOUC_REGIONAL if z == OLOMOUC else z for z in zones))
    count = len(regional)
    if count <= len(A_FULL) and not (count == 1 and regional[0] in MHD_ZONES):
        table = A_FULL if kind == "full" else A_REDUCED
        name = f"{count} {zone_word(count)}, {label}" + (" (zóna 710 bez MHD Olomouc)" if OLOMOUC in zones else "")
        out.append(IdsFare(SYSTEM, Decimal(table[count - 1]), A_MINUTES[count - 1], name, first, last))
    # Ceník B – včetně MHD Olomouc
    if OLOMOUC in zones and 1 <= len(zones) - 1 <= len(B_FULL):
        others = len(zones) - 1
        table = {"full": B_FULL, "junior": B_JUNIOR, "student": B_STUDENT, "senior": B_SENIOR}[kind]
        out.append(IdsFare(SYSTEM, Decimal(table[others - 1]), B_MINUTES[others - 1],
                           f"71+{others} zón, {label}", first, last))
    return out


def city_fare(stops: list[tuple[str, ...]], passenger: Passenger) -> IdsFare | None:
    """Jízda MHD jen po Olomouci (zóna 71, bez vlaku): jízdenka MHD Olomouc
    (DPMO od 1. 6. 2025: 25 Kč, zlevněná 12 Kč; do 6 a od 65 let zdarma)."""
    zones = path_zones(stops)
    if set(zones) != {OLOMOUC}:
        return None
    kind = category(passenger)
    if kind is None or kind == "senior":
        return IdsFare(SYSTEM, Decimal(0), None, "MHD Olomouc zdarma (do 6 / od 65 let)", OLOMOUC, OLOMOUC)
    price, label = (MHD_FULL, "plné") if kind == "full" else (MHD_REDUCED, "zlevněné")
    return IdsFare(SYSTEM, price, None, f"jízdenka MHD Olomouc, {label}", OLOMOUC, OLOMOUC)


def best_for_route(stops: list[tuple[str, ...]], passenger: Passenger,
                   minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares_for_path(path_zones(stops), passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    return best_for_route([zones_from, zones_to], passenger, minutes_needed)
