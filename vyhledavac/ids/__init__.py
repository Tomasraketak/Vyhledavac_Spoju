"""Tarify integrovaných dopravních systémů (IDS).

Jízdenka IDS bývá na regionálních vlacích ČD výrazně levnější než
jízdenka ČD (ČD ji ostatně v e-shopu samo nabízí jako jednu z variant).
Tarify se počítají offline z dat, která systémy zveřejňují – žádné
dotazy za běhu.

Zatím je tu IDESKA (IDS Jihočeského kraje) včetně přesahů do sousedních
krajů. Další systém = další modul se stejným rozhraním ``fare()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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
