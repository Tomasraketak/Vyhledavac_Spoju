"""Tarify integrovaných dopravních systémů (IDS).

Jízdenka IDS bývá na regionálních vlacích ČD výrazně levnější než
jízdenka ČD (ČD ji ostatně v e-shopu samo nabízí jako jednu z variant).
Tarify se počítají offline z dat, která systémy zveřejňují – žádné
dotazy za běhu.

Systémy: IDESKA, PID, IDPK, IDS JMK a IDSOK počítají cenu z tarifu offline;
VDV, IREDO, IDOL, DÚK a IDZK se ptají krajských počítadel (``online``).
Všechny včetně přesahů do sousedních krajů. Skupinu cestujících sčítá
``group_fare``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Sequence


@dataclass(frozen=True)
class IdsFare:
    system: str          # zkratka systému ("IDESKA")
    price: Decimal
    minutes: int | None  # doba platnosti jízdenky
    name: str            # název tarifu ("Plné jízdné")
    zone_from: str
    zone_to: str

    @property
    def label(self) -> str:
        zones = self.zone_from if self.zone_from == self.zone_to else f"{self.zone_from}→{self.zone_to}"
        return f"{self.system} {self.name}, zóny {zones}"


def group_fare(passengers: Sequence, fare_for: Callable[[object], IdsFare | None]) -> IdsFare | None:
    """Jízdenky pro skupinu: pro každého cestujícího jeho nejlevnější, sečtené.

    ``fare_for(passenger)`` vrací jízdenku jednoho cestujícího. Chybí-li
    jízdenka pro kohokoli, skupina jízdenku IDS nemá (``None``). Název
    shrnuje druhy („2× Plné jízdné + Žáci/studenti …“), platnost je
    nejkratší z nich.
    """
    fares = []
    for passenger in passengers:
        fare = fare_for(passenger)
        if fare is None:
            return None
        fares.append(fare)
    if not fares:
        return None
    if len(fares) == 1:
        return fares[0]
    counts = Counter(f.name for f in fares)
    name = " + ".join(f"{n}× {k}" if n > 1 else k for k, n in counts.items())
    minutes = [f.minutes for f in fares if f.minutes is not None]
    first = fares[0]
    return IdsFare(first.system, sum((f.price for f in fares), Decimal(0)),
                   min(minutes) if minutes else None, name, first.zone_from, first.zone_to)


def zone_word(count: int) -> str:
    """„2 zóny“, „5 zón“."""
    return "zóny" if 2 <= count <= 4 else ("zóna" if count == 1 else "zón")
