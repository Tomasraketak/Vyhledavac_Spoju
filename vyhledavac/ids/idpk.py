"""Tarif IDPK (Integrovaná doprava Plzeňského kraje) – jednotlivé jízdné.

Podle „Tarif a tarifní zásady IDPK“ (příloha č. 4 Smlouvy PET IDPK, platnost
od 1. 7. 2026), kap. Jízdné a časová platnost:

* cena = součet přestupného jízdného za každou projetou tarifní zónu:
  1. vnější zóna 14 Kč, 2. vnější 12 Kč, 3.–6. vnější po 10 Kč,
  zóna 001 Plzeň v kombinaci s vnějšími 18 Kč;
* 7 a více vnějších zón (nebo Plzeň + 6 a více) = síťová jízdenka 84 Kč na 240 min;
* jen po Plzni: 24 Kč / 30 min, 28 Kč / 60 min, 50 Kč / 3 h;
* zvýhodněné jízdné 50 % (děti 6–15, 15–18, žáci a studenti 18–26, 65+):
  vnější zóny 7, 6, 5, 5, 5, 5 Kč, Plzeň 12/14/25 Kč;
* platnost podle počtu zón (Plzeň se počítá jako zóna): 1–2 zóny 60 min,
  3 zóny 90, 4 zóny 120, 5 zón 150, 6 zón 180 min.

Zóny zastávek pocházejí z jízdních řádů linek IDPK (CIS JŘ, ``Zaslinky.txt``);
nádraží má pásmo autobusové zastávky u nádraží. Na rozdíl od IDESKA a PID se
cena počítá z pásem **všech** stanic na trase, ne jen z koncových.

Zjednodušení: zóny se zvláštními parametry (Bavorsko 180–191, Zvíkovec,
Žihobce, Čerchov) se počítají jako běžné vnější zóny.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import Passenger
from . import IdsFare

SYSTEM = "IDPK"
URL = "https://www.idpk.cz/kalkulator-jizdneho/"
PLZEN = "001"

FULL_OUTER = [14, 12, 10, 10, 10, 10]
REDUCED_OUTER = [7, 6, 5, 5, 5, 5]
FULL_PLZEN_COMBO, REDUCED_PLZEN_COMBO = 18, 12
FULL_NETWORK, REDUCED_NETWORK = 84, 42
PLZEN_ONLY = {"full": [(24, 30), (28, 60), (50, 180)],
              "reduced": [(12, 30), (14, 60), (25, 180)]}
VALIDITY = {1: 60, 2: 60, 3: 90, 4: 120, 5: 150, 6: 180}


def category(passenger: Passenger) -> str | None:
    if passenger.age < 6:
        return None
    if passenger.age < 18 or passenger.age >= 65 or passenger.is_student:
        return "reduced"
    return "full"


def path_zones(stops: list[tuple[str, ...]]) -> list[str]:
    """Pořadí projetých zón s nejmenším počtem změn (zastávka může ležet ve dvou)."""
    # Stanice v Plzni leží v zóně 001; vnější zóna u ní je jen pro autobusy z okolí.
    stops = [(PLZEN,) if PLZEN in s else s for s in stops if s]
    if not stops:
        return []
    # dynamické programování: cena = počet zón, stav = zóna aktuální zastávky
    best: dict[str, list[str]] = {z: [z] for z in stops[0]}
    for options in stops[1:]:
        nxt: dict[str, list[str]] = {}
        for zone in options:
            candidates = [seq if seq[-1] == zone else seq + [zone] for seq in best.values()]
            nxt[zone] = min(candidates, key=len)
        best = nxt
    seq = min(best.values(), key=len)
    out: list[str] = []
    for zone in seq:
        if zone not in out:
            out.append(zone)
    return out


def fares_for_path(zones: list[str], passenger: Passenger) -> list[IdsFare]:
    if not zones:
        return []
    first, last = zones[0], zones[-1]
    kind = category(passenger)
    if kind is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", first, last)]
    label = "plné" if kind == "full" else "zvýhodněné"
    outer = [z for z in zones if z != PLZEN]
    has_plzen = PLZEN in zones
    if not outer:
        return [IdsFare(SYSTEM, Decimal(price), minutes, f"Plzeň {minutes} min, {label}", first, last)
                for price, minutes in PLZEN_ONLY[kind]]
    network = FULL_NETWORK if kind == "full" else REDUCED_NETWORK
    out = [IdsFare(SYSTEM, Decimal(network), 240, f"síťová 240 min, {label}", first, last)]
    if len(outer) >= 7 or (has_plzen and len(outer) >= 6):
        return out
    table = FULL_OUTER if kind == "full" else REDUCED_OUTER
    price = sum(table[: len(outer)])
    if has_plzen:
        price += FULL_PLZEN_COMBO if kind == "full" else REDUCED_PLZEN_COMBO
    count = len(outer) + (1 if has_plzen else 0)
    out.append(IdsFare(SYSTEM, Decimal(price), VALIDITY.get(count, 180),
                       f"{count} zón, {label}", first, last))
    return out


def best_for_route(stops: list[tuple[str, ...]], passenger: Passenger,
                   minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares_for_path(path_zones(stops), passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    """Jen z koncových zón (bez trasy) – použije se, když trasa chybí."""
    return best_for_route([zones_from, zones_to], passenger, minutes_needed)
