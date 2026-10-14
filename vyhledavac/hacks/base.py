"""Rozhraní travel hacků.

Hack je strategie, jak dostat stejnou (nebo skoro stejnou) cestu levněji.
Dostane výsledky běžného hledání a funkci, kterou si může doptat dílčí
úseky. Vrací nové ``Journey`` označené v ``notes`` a s vyplněnými
``segments`` (dílčí jízdenky, které je potřeba koupit).

Přidání nového hacku = jedna třída::

    @HACKS.register()
    class MujHack(TravelHack):
        name = "muj_hack"

        async def find(self, query, journeys, search):
            ...
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable, ClassVar, Sequence

from ..models import FareStatus, Journey, Leg, SearchQuery

log = logging.getLogger(__name__)

#: Funkce, kterou hack hledá dílčí úseky.
#: ``search(query, allow_browser=False)`` – dílčí úsek u dopravců. Hledání
#: přes prohlížeč (ČD) je pomalé, hack si ho musí vyžádat výslovně.
SearchFn = Callable[..., Awaitable[list[Journey]]]


class TravelHack(abc.ABC):
    """Strategie hledání levnější varianty."""

    name: ClassVar[str] = "hack"
    label: ClassVar[str] = ""
    #: Kolik procent musí hack ušetřit, aby se vůbec nabídl.
    default_min_saving_pct: ClassVar[float] = 3.0

    def __init__(self, **options: Any) -> None:
        self.options = options

    @property
    def min_saving_pct(self) -> float:
        return float(self.options.get("min_saving_pct", self.default_min_saving_pct))

    @abc.abstractmethod
    async def find(
        self, query: SearchQuery, journeys: Sequence[Journey], search: SearchFn
    ) -> list[Journey]:
        """Vrátí nalezené levnější varianty (může být prázdné)."""

    # ---- servis pro potomky -------------------------------------------------
    def is_worth_it(self, hacked_price: Decimal, baseline: Decimal | None) -> bool:
        """Má smysl uživateli komplikovat cestu kvůli téhle úspoře?"""
        if baseline is None:
            return True
        if hacked_price >= baseline:
            return False
        saving = (baseline - hacked_price) / baseline * 100
        return saving >= self.min_saving_pct

    @staticmethod
    def baseline_price(journeys: Sequence[Journey]) -> Decimal | None:
        """Nejnižší cena mezi přímými spoji – proti té se hack poměřuje."""
        prices = [j.price for j in journeys if j.price is not None and not j.is_hack]
        return min(prices) if prices else None


def combine(
    provider_label: str,
    segments: Sequence[Journey],
    *,
    notes: Sequence[str] = (),
) -> Journey | None:
    """Slepí dílčí jízdenky do jednoho výsledku.

    Cena je součtem dílčích cen; když u některého úseku cena chybí,
    výsledek se označí jako neúplný, ale nezahodí se – uživatel aspoň vidí,
    že varianta existuje.
    """
    if not segments:
        return None

    legs: list[Leg] = []
    for segment in segments:
        legs.extend(segment.legs)
    if not legs:
        return None

    prices = [s.price for s in segments]
    known = [p for p in prices if p is not None]
    total = sum(known, Decimal(0)) if known else None
    complete = len(known) == len(prices)

    note_list = list(notes)
    if not complete:
        note_list.append("⚠️ U některého úseku se nepodařilo zjistit cenu")
    estimated = [s for s in segments if s.raw.get("estimate") and s.price is not None]
    if estimated:
        # odhad (regionální vlaky v Německu podle ceníku) – ať je vidět, která část
        note_list.extend(n for s in estimated for n in s.notes if n.startswith("≈"))

    journey = Journey(
        provider=provider_label,
        legs=legs,
        fares=[],
        fare_status=FareStatus.PRICED if complete and total is not None else FareStatus.UNKNOWN,
        notes=note_list,
        segments=list(segments),
    )

    if total is not None:
        from ..models import Fare

        journey.fares = [
            Fare(amount=total, currency="CZK", amount_czk=total,
                 label="součet dílčích jízdenek" + (" (část odhadem)" if estimated else ""),
                 bookable=complete)
        ]
    return journey


def connects(first: Journey, second: Journey, min_transfer: dt.timedelta) -> tuple[bool, bool]:
    """Navazuje ``second`` na ``first``?

    Vrací ``(navazuje, je to tentýž spoj)``. Pokud jde o tentýž vlak, není
    potřeba žádná rezerva na přestup – jen se nevystupuje. Právě tuhle
    situaci chceme u lomených jízdenek nejčastěji.
    """
    if not first.legs or not second.legs:
        return False, False

    last, nxt = first.legs[-1], second.legs[0]
    same_train = bool(last.line) and last.line == nxt.line

    if same_train:
        # Tentýž vlak: časy musí sedět (tolerance na zaokrouhlení jízdního řádu).
        drift = abs((nxt.departure - last.arrival).total_seconds())
        return drift <= 180, True

    gap = second.departure - first.arrival
    # Horní hranice: čekání přes tři hodiny už není přestup, ale výlet.
    return min_transfer <= gap <= dt.timedelta(hours=3), False
