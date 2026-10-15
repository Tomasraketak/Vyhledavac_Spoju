"""Cesty MHD po jednom městě („Praha, Anděl“ → „Praha, Letňany“, „Wien
Stephansplatz“ → „Wien Schönbrunn“).

Dálkoví dopravci (RegioJet, FlixBus …) na takovou cestu nic rozumného
nenabídnou – „Praha → Praha“ jim splyne v jedno město. Engine se proto
u cesty po městě ptá jen zdrojů MHD: IDOS (česká města, cena podle tarifu
IDS – PID, IDS JMK, ODIS, IDSOK …) a plánovače MHD (Transitous) s cenou
místní jízdenky v zahraničí (``FARES``).

Ceny jednotlivých jízdenek (ověřeno v říjnu 2026):

* Vídeň – Wiener Linien od 1. 1. 2026: jízdenka 3,20 € (v aplikaci 3,00 €),
  platí na celou cestu po Vídni s přestupy; děti 6–15 let 1,60 €.
* Bratislava – IDS BK od 1. 6. 2025, zóny 100+101: 30 min 1,20 €, 60 min
  1,80 € (papírová; elektronická 1,09 € / 1,60 €); poloviční pro děti 6–15.
* Budapešť – BKK od 1. 6. 2025: jízdenka na jednu linku (bez přestupu)
  500 Ft, 30 min 600 Ft, 90 min 850 Ft; děti do 14 let a občané EU od 65
  let zdarma.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from decimal import Decimal

from . import stations
from .models import Leg, Mode
from .normalize import normalize


@dataclasses.dataclass(frozen=True)
class CityTrip:
    city: str              # jméno města z databáze („Praha“, „Vídeň“)
    code: str              # země ("" = Česko)
    origin_stop: str       # zastávka bez jména města („Anděl“; "" = město samo)
    destination_stop: str


def city_of(name: str) -> tuple[str, str] | None:
    """(město, země) místa – zastávka „Praha, Anděl“ i „Wien Stephansplatz“."""
    place = stations.place(name)
    if place is None:
        return None
    if place.kind == "c":
        return place.name, place.code
    if place.obec:
        return place.obec, place.code
    # zahraniční stanice („Bratislava hl.st.“) obec nemá – město ze začátku jména
    words = [w for w in re.split(r"[,\s/]+|-(?=\w)", name) if w]
    for k in range(len(words) - 1, 0, -1):
        head = " ".join(words[:k])
        found = stations.place(head)
        if found is not None and found.kind == "c" and found.code == place.code \
                and normalize(head) in {normalize(x) for x in (found.name, found.local, *found.keys) if x}:
            return found.name, found.code
    return None


def stop_part(name: str, city: str) -> str:
    """Co ve jménu zbude bez města: „Praha, Anděl“ → „Anděl“, „Vídeň“ → ""."""
    place = stations.place(city)
    words = {normalize(w) for w in (city, *(place.keys if place else ()),
                                    place.local if place else "") if w}
    text = name.strip()
    for sep in (",", " - ", "-", " "):
        head, _, rest = text.partition(sep)
        if rest and normalize(head) in words:
            return rest.strip(" ,")
    return "" if normalize(text) in words else text


def trip(origin: str, destination: str) -> CityTrip | None:
    """Je to cesta po jednom městě? Obě místa ve stejném městě a aspoň jedno
    z nich je konkrétní zastávka (ne „Praha → Praha“)."""
    if normalize(origin) == normalize(destination):
        return None
    a, b = city_of(origin), city_of(destination)
    if a is None or a != b:
        return None
    stops = stop_part(origin, a[0]), stop_part(destination, a[0])
    if not any(stops):
        return None
    return CityTrip(a[0], a[1], *stops)


@dataclasses.dataclass(frozen=True)
class Ticket:
    minutes: int | None        # platnost (None = celá cesta po městě, s přestupy)
    price: Decimal
    label: str
    one_ride: bool = False     # jen jedna jízda bez přestupu (vonaljegy)


#: Jízdenky MHD v zahraničí: (země, město) → měna, jízdenky od nejlevnější.
FARES: dict[tuple[str, str], tuple[str, list[Ticket]]] = {
    ("AT", "Vídeň"): ("EUR", [Ticket(None, Decimal("3.20"), "jízdenka Wiener Linien (v aplikaci 3,00 €)")]),
    ("SK", "Bratislava"): ("EUR", [Ticket(30, Decimal("1.20"), "30 min IDS BK, zóny 100+101"),
                                   Ticket(60, Decimal("1.80"), "60 min IDS BK, zóny 100+101")]),
    ("HU", "Budapešť"): ("HUF", [Ticket(None, Decimal(500), "jízdenka BKK na jednu linku (vonaljegy)",
                                        one_ride=True),
                                 Ticket(30, Decimal(600), "30 min BKK"),
                                 Ticket(90, Decimal(850), "90 min BKK")]),
}
#: Kdo jezdí zdarma / za polovic (věk): město → (zdarma do, polovic do, zdarma od).
DISCOUNTS = {"Vídeň": (6, 15, None), "Bratislava": (6, 15, None), "Budapešť": (14, 14, 65)}


def ticket_for(trip: CityTrip, rides: int, minutes: float) -> tuple[str, Ticket] | None:
    """Nejlevnější jízdenka na cestu (počet jízd, doba od nástupu do výstupu)."""
    fares = FARES.get((trip.code, trip.city))
    if fares is None:
        return None
    currency, tickets = fares
    for ticket in tickets:
        if ticket.one_ride and rides > 1:
            continue
        if ticket.minutes is None or minutes <= ticket.minutes:
            return currency, ticket
    return None


def passenger_factor(city: str, ages: list[int]) -> Decimal:
    """Kolik plných jízdenek skupina potřebuje (děti zdarma / za polovic)."""
    free_until, half_until, free_from = DISCOUNTS.get(city, (6, 6, None))
    total = Decimal(0)
    for age in ages:
        if age < free_until or (free_from is not None and age >= free_from):
            continue
        total += Decimal("0.5") if age < half_until else Decimal(1)
    return total


#: Zkratky, které IDOS používá ve jménech zastávek („Senovážné nám.“).
_ABBREVIATIONS = [(re.compile(r"\bnáměstí\b", re.I), "nám."), (re.compile(r"\bnámestie\b", re.I), "nám."),
                  (re.compile(r"\bnádraží\b", re.I), "nádr."), (re.compile(r"\bulice\b", re.I), "ul.")]


def abbreviated(text: str) -> str:
    for pattern, short in _ABBREVIATIONS:
        text = pattern.sub(short, text)
    return text


#: MHD na konci cesty: nejdéle tolik minut (vaporetto, městský autobus) …
CITY_MINUTES = 30
#: … a vlakem jen kousek (S-Bahn z hlavního nádraží do centra).
CITY_TRAIN_MINUTES = 15
#: … příměstskou železnicí (RER, S-Bahn, S-tog) i déle.
SUBURBAN_MINUTES = 45
_SUBURBAN = re.compile(r"\b(RER|S-?Bahn|S-tog|SKM|Cercan[ií]as|Rodalies)\b", re.IGNORECASE)
#: … a vlakem z letiště do centra (Kraków Lotnisko → Główny, 17 min).
_AIRPORT = re.compile(r"airport|letiště|lotnisko|flughafen|a[ée]roport|aeropuerto|aeroporto|lufthavn|"
                      r"flygplats|repülőtér|schiphol", re.IGNORECASE)


def city_leg(leg: Leg) -> bool:
    """Může úsek být jen městskou dopravou (tramvaj, metro, krátký autobus/loď/S-Bahn)?"""
    if leg.mode in (Mode.WALK, Mode.TRAM, Mode.METRO):
        return True
    minutes = leg.duration.total_seconds() / 60
    if leg.mode is Mode.TRAIN:
        # příměstská železnice po městě i delší (RER B z letiště CDG do centra Paříže)
        suburban = (_SUBURBAN.search(f"{leg.carrier} {leg.line}") is not None
                    or _AIRPORT.search(f"{leg.origin} {leg.destination}") is not None)
        return minutes <= (SUBURBAN_MINUTES if suburban else CITY_TRAIN_MINUTES)
    if leg.mode in (Mode.BUS, Mode.FERRY):
        return "flix" not in (leg.carrier or "").lower() and minutes <= CITY_MINUTES
    return False


def _whole_city(name: str) -> tuple[str, str] | None:
    """Město, když je zadané celé („Benátky“, „Praha“), ne konkrétní zastávka."""
    found = city_of(name)
    return found if found is not None and not stop_part(name, found[0]) else None


def main_part(legs: list[Leg], origin: str, destination: str) -> tuple[int, int]:
    """Rozsah úseků bez MHD na koncích: když vlak dojede do hledaného města
    (Venezia Santa Lucia = Benátky), vaporetto/tramvaj/S-Bahn dál po městě
    k cestě nepatří. Zadaná konkrétní zastávka („Praha, Anděl“) se neořezává."""
    start, end = 0, len(legs)
    here = _whole_city(origin)
    k = 0
    while k < end - 1 and city_leg(legs[k]):
        k += 1
    if k and here is not None and city_of(legs[k].origin) == here:
        start = k
    there = _whole_city(destination)
    j = end
    while j - 1 > start and city_leg(legs[j - 1]):
        j -= 1
    if j < end and there is not None and city_of(legs[j - 1].destination) == there:
        end = j
    return start, end
