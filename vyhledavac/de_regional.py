"""Odhad ceny německých regionálních vlaků (RE, RB, S-Bahn) podle pevného ceníku.

Regionální vlaky v Německu prodává jen DB (a dopravní svazy), a DB z některých
adres dotazy odmítá. Jejich ceny ale nejsou dynamické – platí pevný ceník:

* **Deutschlandtarif** (celostátní, `data/de_nahverkehr.json.gz`): Normalpreis
  Nahverkehrszüge, 2. třída, jednoduchá jízda, pro každý kilometr 1–2000 km,
  stav 14. 12. 2025 (Preisliste des Deutschlandtarifs, Teil J). Vzdálenost je
  tarifní (po trati) – odhadneme ji ze vzdušných vzdáleností mezi zastávkami
  (+10 % na oblouky trati).
* **NRW** (Kolín, Duisburg, Cáchy, Mönchengladbach…): uvnitř Severního Porýní-
  Vestfálska platí i NRW-Tarif – digitální eezy.nrw (základ 1,88 € + cca 0,28 €
  za km vzdušnou čarou, strop 35,20 € za 24 h) a EinzelTicket NRW za 25,80 €
  na jakoukoli cestu po NRW (mobil.nrw, Fortschreibung NRW-Tarif 1. 1. 2026).
  Bere se nejlevnější z možností.

Děti 6–14 let polovic, do 6 let zdarma. Výsledek je vždy označený jako odhad.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
import re
from decimal import ROUND_FLOOR, Decimal
from functools import cache
from importlib import resources
from typing import Sequence

from . import geo
from .models import Fare, FareStatus, Journey, Leg, Mode, Passenger

#: Tarifní (traťová) vzdálenost ≈ vzdušná po zastávkách × tolik.
RAIL_FACTOR = 1.10
#: NRW-Tarif 2026 (mobil.nrw): eezy.nrw základ a strop, EinzelTicket NRW.
EEZY_BASE = Decimal("1.88")
EEZY_PER_KM = Decimal("0.28")          # tarifní prostory 0,25–0,30 €/km
EEZY_DAY_CAP = Decimal("35.20")
NRW_SINGLE = Decimal("25.80")
#: Hrubý obrys NRW (jih, sever, západ, východ) – jen pro rozhodnutí, zda zkusit NRW-Tarif.
NRW_BOX = (50.32, 52.53, 5.86, 9.46)
#: Zahraniční konečná regionálního vlaku (Venlo, Salzburg) smí ležet nejvýš tak daleko
#: od poslední německé zastávky – přeshraniční regionální vlak.
BORDER_KM = 60.0

_LONG = re.compile(r"\b(ICE|IC|EC|ECE|RJ|RJX|NJ|EN|FLX|TGV|THA|ES|EST|IR|D)\b", re.I)
_REGIONAL = re.compile(r"\b(RE|RB|S|IRE|MEX|RS|ERB|NWB|BRB|HLB|ALX|SE|RER)\s?\d|\bS\s*\d|\b(RE|RB)\d", re.I)
_REGIONAL_CARRIER = ("regio", "eurobahn", "national express", "vias", "nordwestbahn", "abellio",
                     "arverio", "rheinruhrbahn", "bayerische regiobahn", "agilis", "erixx", "metronom",
                     "odeg", "landerbahn", "laenderbahn", "länderbahn", "s bahn", "s-bahn", "trilex",
                     "hanseatische", "start ", "go-ahead", "mitteldeutsche regiobahn", "cantus", "vlexx",
                     "westfalenbahn", "db regio")
#: Náhradní autobusová doprava za vlak (Schienenersatzverkehr) – jízdenka jako na vlak.
_RAIL_REPLACEMENT = re.compile(r"\b(SEV|Bsv|Ersatz)", re.I)


@cache
def _table() -> list[int]:
    with resources.files("vyhledavac").joinpath("data/de_nahverkehr.json.gz").open("rb") as fh:
        return json.loads(gzip.decompress(fh.read()))["cents"]


def table_price(km: int) -> Decimal:
    """Normalpreis Deutschlandtarif, 2. třída, jedna dospělá osoba (€)."""
    cents = _table()
    km = max(1, min(int(km), len(cents)))
    return Decimal(cents[km - 1]) / 100


def is_regional(leg: Leg) -> bool:
    """Regionální vlak (RE/RB/S-Bahn…), ne dálkový (ICE, IC, EC, RJ, NJ, FLX) –
    i náhradní autobus železničního dopravce (ALEX Furth im Wald → Schwandorf)."""
    if leg.mode is Mode.BUS:
        carrier = (leg.carrier or "").lower()
        return (_RAIL_REPLACEMENT.search(leg.line or "") is not None
                or any(k in carrier for k in _REGIONAL_CARRIER if k not in ("regio", "start ")))
    if leg.mode is not Mode.TRAIN:
        return False
    line = leg.line or ""
    if _LONG.search(line):
        return False
    if _REGIONAL.search(line):
        return True
    carrier = (leg.carrier or "").lower()
    return any(k in carrier for k in _REGIONAL_CARRIER)


def _factor(passenger: Passenger) -> Decimal:
    if passenger.age < 6:
        return Decimal(0)
    return Decimal("0.5") if passenger.age <= 14 else Decimal(1)


def _round_down_5(value: Decimal) -> Decimal:
    return (value * 20).to_integral_value(rounding=ROUND_FLOOR) / 20


def _in_nrw(point: tuple[float, float]) -> bool:
    south, north, west, east = NRW_BOX
    return south <= point[0] <= north and west <= point[1] <= east


async def _points(run: Sequence[Leg]) -> list[tuple[str, tuple[float, float] | None]]:
    from .transfer import locate

    names: list[str] = []
    for leg in run:
        stops = [s.name for s in leg.stops] if len(leg.stops) >= 2 else [leg.origin, leg.destination]
        for name in stops:
            if not names or names[-1] != name:
                names.append(name)
    found = await asyncio.gather(*(locate(n) for n in names), return_exceptions=True)
    return [(n, None if isinstance(p, BaseException) else p) for n, p in zip(names, found)]


async def estimate(run: Sequence[Leg], passengers: Sequence[Passenger]) -> tuple[Decimal, str] | None:
    """Odhad ceny (€ za všechny cestující, popis tarifu) pro kus jen regionálními
    vlaky po Německu (konečná smí být kousek za hranicí: Venlo, Salzburg)."""
    rides = [leg for leg in run if leg.mode is not Mode.WALK]
    if not rides or not all(is_regional(leg) for leg in rides):
        return None
    points = await _points(rides)
    known = [(n, p) for n, p in points if p is not None]
    if len(known) < 2:
        return None
    countries = [geo.country_of(n) for n, _ in known]
    german = [p for (n, p), c in zip(known, countries) if c in (None, "DE")]
    if not german:
        return None
    for (name, point), country in zip(known, countries):
        if country not in (None, "DE") and min(geo.km(point, g) for g in german) > BORDER_KM:
            return None                       # vlak vede hlouběji do ciziny – jiný tarif
    air = sum(geo.km(a[1], b[1]) for a, b in zip(known, known[1:]))
    if air < 0.5:
        return None
    km = math.ceil(air * RAIL_FACTOR)
    adult = table_price(km)
    options = [(adult, f"Deutschlandtarif {km} km")]
    if all(_in_nrw(p) for _, p in known) and all(c in (None, "DE") for c in countries):
        line = math.ceil(geo.km(known[0][1], known[-1][1]))
        options.append((min(EEZY_BASE + EEZY_PER_KM * line, EEZY_DAY_CAP), f"eezy.nrw {line} km vzdušnou čarou"))
        options.append((NRW_SINGLE, "EinzelTicket NRW"))
    price, label = min(options, key=lambda o: o[0])
    total = sum((_round_down_5(price * _factor(p)) for p in passengers), Decimal(0))
    return total, label


def deep_link(origin: str, destination: str, when) -> str:
    """Hledání na bahn.de s vyplněnými stanicemi a časem – cenu si ověříte sami."""
    from urllib.parse import quote

    return (f"https://int.bahn.de/en/buchung/fahrplan/suche#sts=true&so={quote(origin)}&zo={quote(destination)}"
            f"&kl=2&hd={when:%Y-%m-%dT%H:%M:%S}&hza=D")


async def regional_journey(run: Sequence[Leg], passengers: Sequence[Passenger],
                           converter) -> Journey | None:
    """Kus cesty německými regionálními vlaky s odhadem ceny (jízdenka u DB / v aplikaci)."""
    found = await estimate(run, passengers)
    if found is None:
        return None
    eur, label = found
    czk = converter.to_czk(eur, "EUR") if converter is not None else None
    rides = [leg for leg in run if leg.mode is not Mode.WALK]
    first, last = rides[0], rides[-1]
    return Journey(
        provider="DB regionální (odhad)",
        legs=list(run),
        fares=[Fare(amount=eur, currency="EUR", amount_czk=czk, label="DB regionální vlaky – odhad",
                    tariff=f"odhad podle ceníku: {label}")],
        fare_status=FareStatus.PRICED,
        notes=[f"≈ Cena regionálních vlaků {first.origin} → {last.destination} je odhad podle pevného "
               f"ceníku ({label}, {eur:.2f} €). Jízdenku koupíte v automatu DB, v aplikaci DB Navigator "
               "nebo na bahn.de; s Deutschlandticketem (63 €/měsíc) jedete bez příplatku."],
        deep_link=deep_link(first.origin, last.destination, first.departure),
        raw={"estimate": True},
    )
