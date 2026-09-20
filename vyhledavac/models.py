"""Datový model společný pro všechny dopravce.

Klíčové rozhodnutí: časy jsou vždy ``datetime`` s časovou zónou, nikoliv
řetězce ``"HH:MM"``. Původní kód počítal s minutami od půlnoci a modulem
1440, což rozbíjelo spoje přes půlnoc, přes změnu času i vícedenní jízdy.
Ceny jsou ``Decimal`` – u peněz se float nepoužívá.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Sequence

# Evropa/Praha používáme jako výchozí zónu pro vstupy bez zóny.
try:  # Python 3.9+
    from zoneinfo import ZoneInfo

    DEFAULT_TZ = ZoneInfo("Europe/Prague")
except Exception:  # pragma: no cover - fallback pro exotická prostředí
    DEFAULT_TZ = dt.timezone(dt.timedelta(hours=1))


class Mode(str, Enum):
    """Druh dopravního prostředku."""

    TRAIN = "train"
    BUS = "bus"
    TRAM = "tram"
    METRO = "metro"
    FERRY = "ferry"
    WALK = "walk"
    UNKNOWN = "unknown"


class DiscountCard(str, Enum):
    """Slevové karty a nároky, které umíme předat dopravcům."""

    NONE = "none"
    ISIC = "isic"
    CZ_STUDENT = "cz_student"
    CZ_SENIOR = "cz_senior"
    CZ_CHILD = "cz_child"
    SK_FREE = "sk_free"          # slovenská 100% sleva (studenti/senioři)
    AT_VORTEILSCARD = "at_vorteilscard"
    DE_BAHNCARD_25 = "de_bahncard_25"
    DE_BAHNCARD_50 = "de_bahncard_50"


@dataclass(frozen=True, slots=True)
class Passenger:
    """Jeden cestující. Dopravce si z toho odvodí svůj tarif."""

    age: int = 26
    cards: tuple[DiscountCard, ...] = ()

    def has(self, card: DiscountCard) -> bool:
        return card in self.cards

    @property
    def is_child(self) -> bool:
        return self.age < 15

    @property
    def is_youth(self) -> bool:
        return 15 <= self.age < 26

    @property
    def is_senior(self) -> bool:
        return self.age >= 65


@dataclass(frozen=True, slots=True)
class Place:
    """Místo zadané uživatelem nebo vrácené dopravcem.

    ``provider_ids`` drží ID podle jednotlivých dopravců, aby se stanice
    nemusela dohledávat opakovaně.
    """

    name: str
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    provider_ids: dict[str, str] = field(default_factory=dict, compare=False)

    def __str__(self) -> str:  # pragma: no cover - jen pro výpis
        return self.name


@dataclass(slots=True)
class Stop:
    """Zastávka na trase spoje."""

    name: str
    arrival: dt.datetime | None = None
    departure: dt.datetime | None = None
    platform: str | None = None
    lat: float | None = None
    lon: float | None = None

    @property
    def time(self) -> dt.datetime | None:
        """Jakýkoli známý čas – hodí se pro řazení."""
        return self.departure or self.arrival


@dataclass(slots=True)
class Leg:
    """Jeden úsek jízdy jedním vozidlem (mezi přestupy)."""

    origin: str
    destination: str
    departure: dt.datetime
    arrival: dt.datetime
    mode: Mode = Mode.UNKNOWN
    carrier: str = ""
    line: str = ""
    stops: list[Stop] = field(default_factory=list)
    reservation_required: bool = False

    @property
    def duration(self) -> dt.timedelta:
        return self.arrival - self.departure

    def stop_names(self) -> list[str]:
        """Názvy zastávek včetně koncových, i když chybí seznam mezizastávek."""
        if self.stops:
            return [s.name for s in self.stops]
        return [self.origin, self.destination]

    def serves(self, station: str) -> bool:
        """Zastavuje tento úsek v dané stanici? Porovnává se bez diakritiky."""
        from .normalize import matches_station

        return any(matches_station(station, name) for name in self.stop_names())


@dataclass(slots=True)
class Fare:
    """Jedna cenová nabídka ke spoji."""

    amount: Decimal
    currency: str
    label: str = ""
    tariff: str = ""
    bookable: bool = True
    flexible: bool = False
    seats_left: int | None = None
    deep_link: str | None = None
    amount_czk: Decimal | None = None

    @property
    def sort_value(self) -> Decimal:
        """Hodnota pro porovnávání – vždy v CZK, když je převod k dispozici."""
        return self.amount_czk if self.amount_czk is not None else self.amount


class FareStatus(str, Enum):
    """Proč u spoje chybí cena. Důležité pro poctivé zobrazení v UI."""

    PRICED = "priced"            # cena známá
    SOLD_OUT = "sold_out"        # vyprodáno
    UNKNOWN = "unknown"          # dopravce cenu nevrací (např. jen jízdní řád)
    NOT_BOOKABLE = "not_bookable"  # nelze koupit online


@dataclass(slots=True)
class Journey:
    """Kompletní spojení z A do B, jak ho vrátil jeden dopravce."""

    provider: str
    legs: list[Leg]
    fares: list[Fare] = field(default_factory=list)
    fare_status: FareStatus = FareStatus.UNKNOWN
    journey_id: str = ""
    deep_link: str | None = None
    # Stopa po travel hacku – prázdné u běžných spojů.
    notes: list[str] = field(default_factory=list)
    segments: list["Journey"] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ---- odvozené vlastnosti -------------------------------------------------
    @property
    def departure(self) -> dt.datetime:
        return self.legs[0].departure

    @property
    def arrival(self) -> dt.datetime:
        return self.legs[-1].arrival

    @property
    def origin(self) -> str:
        return self.legs[0].origin

    @property
    def destination(self) -> str:
        return self.legs[-1].destination

    @property
    def duration(self) -> dt.timedelta:
        return self.arrival - self.departure

    @property
    def transfers(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def modes(self) -> set[Mode]:
        return {leg.mode for leg in self.legs}

    @property
    def carriers(self) -> list[str]:
        seen: list[str] = []
        for leg in self.legs:
            if leg.carrier and leg.carrier not in seen:
                seen.append(leg.carrier)
        return seen

    @property
    def is_hack(self) -> bool:
        return bool(self.segments)

    @property
    def cheapest_fare(self) -> Fare | None:
        bookable = [f for f in self.fares if f.bookable]
        pool = bookable or self.fares
        return min(pool, key=lambda f: f.sort_value) if pool else None

    @property
    def price(self) -> Decimal | None:
        """Nejnižší cena v CZK, nebo ``None`` když cenu neznáme.

        ``None`` je záměrně odlišené od nuly i od nekonečna – volající se
        musí rozhodnout, jak s neznámou cenou naložit.
        """
        fare = self.cheapest_fare
        return fare.sort_value if fare else None

    @property
    def price_label(self) -> str:
        fare = self.cheapest_fare
        if fare is not None:
            value = fare.sort_value
            suffix = " Kč" if (fare.amount_czk is not None or fare.currency == "CZK") else f" {fare.currency}"
            return f"{value:.0f}{suffix}"
        if self.fare_status is FareStatus.SOLD_OUT:
            return "Vyprodáno"
        if self.fare_status is FareStatus.NOT_BOOKABLE:
            return "Nelze koupit online"
        return "Cena neznámá"

    def stop_names(self) -> list[str]:
        """Všechny zastávky po celé trase, v pořadí."""
        names: list[str] = []
        for leg in self.legs:
            for name in leg.stop_names():
                if not names or names[-1] != name:
                    names.append(name)
        return names

    def serves(self, station: str) -> bool:
        from .normalize import matches_station

        return any(matches_station(station, name) for name in self.stop_names())

    def with_notes(self, *notes: str) -> "Journey":
        return replace(self, notes=[*self.notes, *notes])

    def signature(self) -> tuple:
        """Identita spoje pro deduplikaci napříč dopravci."""
        return (
            self.departure,
            self.arrival,
            tuple(leg.line for leg in self.legs),
        )


@dataclass(slots=True)
class SearchQuery:
    """Vstup vyhledávání. Nemění se v průběhu – hacky si dělají kopie."""

    origin: str
    destination: str
    departure: dt.datetime
    passengers: tuple[Passenger, ...] = (Passenger(),)
    currency: str = "CZK"
    language: str = "cs"
    max_results: int = 60
    # Jak daleko za zadaný čas se ještě hledá.
    window: dt.timedelta = dt.timedelta(hours=12)

    def __post_init__(self) -> None:
        if self.departure.tzinfo is None:
            self.departure = self.departure.replace(tzinfo=DEFAULT_TZ)
        if not self.passengers:
            self.passengers = (Passenger(),)

    @property
    def date(self) -> dt.date:
        return self.departure.date()

    @property
    def lead(self) -> Passenger:
        """Hlavní cestující – podle něj se volí tarif tam, kde dopravce
        neumí více typů najednou."""
        return self.passengers[0]

    @property
    def window_end(self) -> dt.datetime:
        return self.departure + self.window

    def within_window(self, when: dt.datetime) -> bool:
        return self.departure <= when <= self.window_end

    def replace(self, **changes: Any) -> "SearchQuery":
        return replace(self, **changes)

    def leg_query(self, origin: str, destination: str, departure: dt.datetime | None = None) -> "SearchQuery":
        """Odvozený dotaz pro dílčí úsek (používají travel hacky)."""
        return replace(
            self,
            origin=origin,
            destination=destination,
            departure=departure or self.departure,
        )


@dataclass(slots=True)
class ProviderResult:
    """Výsledek jednoho dopravce včetně diagnostiky.

    Engine nikdy nespolkne chybu potichu – když dopravce selže, je to vidět.
    """

    provider: str
    journeys: list[Journey] = field(default_factory=list)
    error: str | None = None
    blocked: bool = False
    elapsed: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class SearchResult:
    """Agregovaný výstup vyhledávání."""

    query: SearchQuery
    journeys: list[Journey] = field(default_factory=list)
    provider_results: list[ProviderResult] = field(default_factory=list)

    @property
    def failed_providers(self) -> list[ProviderResult]:
        return [r for r in self.provider_results if not r.ok]

    # ---- cenový přehled ------------------------------------------------------
    @property
    def priced(self) -> list[Journey]:
        """Spoje se známou cenou – jen s nimi jde optimalizovat podle ceny."""
        return [j for j in self.journeys if j.price is not None]

    @property
    def unpriced(self) -> list[Journey]:
        return [j for j in self.journeys if j.price is None]

    @property
    def price_coverage(self) -> float:
        """Podíl spojů, u kterých známe cenu (0..1).

        Nízké číslo znamená, že se podle ceny rozhodujete jen z části
        nabídky – uživatel to musí vědět.
        """
        if not self.journeys:
            return 0.0
        return len(self.priced) / len(self.journeys)

    @property
    def cheapest(self) -> Journey | None:
        pool = self.priced
        return min(pool, key=lambda j: (j.price, j.duration)) if pool else None

    @property
    def fastest(self) -> Journey | None:
        return min(self.journeys, key=lambda j: j.duration) if self.journeys else None

    @property
    def best_value(self) -> Journey | None:
        """Nejlepší kompromis mezi cenou a časem.

        Skóre normalizuje cenu i dobu jízdy na rozsah nabídky a sečte je –
        vyhrává spoj, který není ani nejdražší, ani nejpomalejší.
        """
        pool = self.priced
        if len(pool) < 2:
            return pool[0] if pool else None

        prices = [float(j.price) for j in pool]
        durations = [j.duration.total_seconds() for j in pool]
        p_min, p_max = min(prices), max(prices)
        d_min, d_max = min(durations), max(durations)
        p_span = (p_max - p_min) or 1.0
        d_span = (d_max - d_min) or 1.0

        def score(journey: Journey) -> float:
            price_part = (float(journey.price) - p_min) / p_span
            time_part = (journey.duration.total_seconds() - d_min) / d_span
            return price_part + time_part

        return min(pool, key=score)

    def savings_vs_fastest(self) -> Decimal | None:
        """Kolik ušetříte, když místo nejrychlejšího pojedete nejlevnějším."""
        cheap, fast = self.cheapest, self.fastest
        if cheap is None or fast is None or fast.price is None:
            return None
        diff = fast.price - cheap.price
        return diff if diff > 0 else None

    def provider_prices(self) -> dict[str, Decimal]:
        """Nejnižší cena od každého dopravce – rychlé srovnání, kdo je levnější."""
        best: dict[str, Decimal] = {}
        for journey in self.priced:
            current = best.get(journey.provider)
            if current is None or journey.price < current:
                best[journey.provider] = journey.price
        return dict(sorted(best.items(), key=lambda kv: kv[1]))

    @property
    def blocked_providers(self) -> list[str]:
        return [r.provider for r in self.provider_results if r.blocked]

    def extend(self, results: Iterable[ProviderResult]) -> None:
        for res in results:
            self.provider_results.append(res)
            self.journeys.extend(res.journeys)


def sort_journeys(journeys: Sequence[Journey], by: str = "departure") -> list[Journey]:
    """Seřadí spoje. Spoje bez ceny končí až za těmi s cenou."""
    if by == "price":
        return sorted(
            journeys,
            key=lambda j: (j.price is None, j.price or Decimal(0), j.departure),
        )
    if by == "duration":
        return sorted(journeys, key=lambda j: (j.duration, j.departure))
    return sorted(journeys, key=lambda j: (j.departure, j.arrival))
