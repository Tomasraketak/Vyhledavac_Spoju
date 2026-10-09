"""Tarif VDV (Veřejná doprava Vysočiny) – ceny z tarifního počítadla kraje.

Počítadlo https://tim.abirun.eu/KrajVysocina/TarifniPocitadlo/ (odkazuje na něj
kr-vysocina.cz) vrací pro dvojici zón tabulku jízdenek: plné, dítě a mládež,
student, 65+ … s dobou platnosti. Cena závisí jen na výchozí a cílové zóně
(„tarifní jednice“), proto stačí jeden dotaz na dvojici zón; výsledek se
pamatuje po dobu běhu programu.

Ceny se dotazují **předem** (``prefetch``) – výpočet plánu jízdenek je
synchronní a jen čte, co už je stažené.
"""

from __future__ import annotations

import asyncio
import gzip
import html as html_mod
import json
import logging
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import httpx

from ..models import Passenger
from . import IdsFare

log = logging.getLogger(__name__)

SYSTEM = "VDV"
URL = "https://www.kr-vysocina.cz/spocitat-jizdne/ms-122447"
API = "https://tim.abirun.eu/KrajVysocina/TarifniPocitadlo"
DATA = Path(__file__).resolve().parent.parent / "data" / "ids_vdv.json.gz"

#: (zóna, zóna) -> [(název tarifu, minuty, cena)]
_prices: dict[tuple[str, str], list[tuple[str, int | None, Decimal]]] = {}
_lock = asyncio.Lock()

#: Profil cestujícího -> začátek názvu jednotlivé jízdenky v počítadle.
PROFILES = {"full": "Plné jednotlivé", "youth": "Dítě a mládež jednotlivé",
            "student": "Student jednotlivé", "senior": "Osoba 65+ jednotlivé"}


@lru_cache(maxsize=1)
def _zones() -> dict[str, dict]:
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            return json.load(fh)["zones"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("Zóny VDV se nepodařilo načíst: %s", exc)
        return {}


def profile(passenger: Passenger) -> str | None:
    if passenger.age < 6:
        return None
    if passenger.age < 18:
        return "youth"
    if passenger.age >= 65:
        return "senior"
    if passenger.is_student:
        return "student"
    return "full"


def parse(html: str) -> list[tuple[str, int | None, Decimal]]:
    """Řádky tabulky: (název, platnost v minutách, cena v hotovosti)."""
    text = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", text)))
    text = re.sub(r"(\|\s*)+", "| ", text.replace("\xa0", " "))
    rows = []
    for m in re.finditer(r"\| ([^|]+?) \| (\d+) (min|d) \| (\d+),(\d{2}) Kč", text):
        minutes = int(m.group(2)) * (1440 if m.group(3) == "d" else 1)
        rows.append((m.group(1).strip(), minutes, Decimal(f"{m.group(4)}.{m.group(5)}")))
    return rows


async def _fetch(client: httpx.AsyncClient, zone_from: str, zone_to: str) -> None:
    zones = _zones()
    if zone_from not in zones or zone_to not in zones:
        return
    try:
        response = await client.post(f"{API}/Home/Tarify", timeout=20, data={
            "idZonyNeboZastavkyOdkud": zones[zone_from]["id"],
            "idZonyNeboZastavkyKam": zones[zone_to]["id"],
        }, headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("VDV: cenu %s→%s nelze zjistit (%s)", zone_from, zone_to, exc)
        return
    _prices[(zone_from, zone_to)] = parse(response.text)


async def prefetch(pairs: set[tuple[str, str]]) -> None:
    """Stáhne ceny pro dvojice zón, které ještě nejsou v paměti (souběžně, max. 6)."""
    todo = {p for p in pairs if p not in _prices}
    if not todo:
        return
    async with _lock, httpx.AsyncClient(follow_redirects=True) as client:
        limit = asyncio.Semaphore(6)

        async def one(pair):
            async with limit:
                await _fetch(client, *pair)

        await asyncio.gather(*(one(p) for p in todo))


def fares(zone_from: str, zone_to: str, passenger: Passenger) -> list[IdsFare]:
    kind = profile(passenger)
    if kind is None:
        return [IdsFare(SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)]
    rows = _prices.get((zone_from, zone_to)) or []
    want = PROFILES[kind]
    out = [IdsFare(SYSTEM, price, minutes, name, zone_from, zone_to)
           for name, minutes, price in rows
           if name.startswith(want) and (minutes is None or minutes <= 1440)]
    # 24hodinová celosíťová bývá na dlouhé cesty levnější
    network = {"full": "24 hodinová celosíťová VDV", "youth": "24 hodinová celosíťová Dítě",
               "student": "24 hodinová celosíťová Student", "senior": "24 hodinová celosíťová 65+"}[kind]
    out += [IdsFare(SYSTEM, price, minutes, name, zone_from, zone_to)
            for name, minutes, price in rows if name.startswith(network)]
    return out


def best_fare(zone_from: str, zone_to: str, passenger: Passenger,
              minutes_needed: float) -> IdsFare | None:
    usable = [f for f in fares(zone_from, zone_to, passenger)
              if f.minutes is None or f.minutes >= minutes_needed]
    return min(usable, key=lambda f: f.price, default=None)


def best_between(zones_from: tuple[str, ...], zones_to: tuple[str, ...], passenger: Passenger,
                 minutes_needed: float) -> IdsFare | None:
    options = [best_fare(a, b, passenger, minutes_needed) for a in zones_from for b in zones_to]
    return min((o for o in options if o is not None), key=lambda f: f.price, default=None)
