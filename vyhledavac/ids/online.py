"""Tarify, jejichž ceny se za běhu ptají oficiálních krajských počítadel.

Cena v těchto systémech závisí jen na výchozí a cílové zóně, takže stačí jeden
dotaz na dvojici zón a výsledek se pamatuje po dobu běhu programu. Ceny se
stahují **předem** (``prefetch``) a souběžně; výpočet plánu jízdenek je pak
synchronní a jen čte, co už je v paměti.

* ``TimTariff`` – „Tarifní počítadlo“ od firmy ABIRAIL (tim.*): VDV
  (Vysočina) a IREDO (Královéhradecký a Pardubický kraj).
* ``IdolTariff`` – počítadlo IDOL (Liberecký kraj) na dopravnimapy.kraj-lbc.cz.

Zóny zastávek se stahují při sestavení dat (``tools/build_stations.py``).
"""

from __future__ import annotations

import asyncio
import gzip
import html as html_mod
import json
import logging
import re
from decimal import Decimal
from pathlib import Path

import httpx

from ..models import Passenger
from . import IdsFare

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}


def profile(passenger: Passenger) -> str | None:
    """Společné kategorie: zdarma (None), youth 6–18, student, senior 65+, full."""
    if passenger.age < 6:
        return None
    if passenger.age < 18:
        return "youth"
    if passenger.age >= 65:
        return "senior"
    if passenger.is_student:
        return "student"
    return "full"


def page_text(html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", text)))
    return re.sub(r"(\|\s*)+", "| ", text.replace("\xa0", " "))


class OnlineTariff:
    """Společný základ: paměť cen, prefetch a výběr nejlevnější jízdenky."""

    SYSTEM = ""
    URL = ""
    data_file = ""
    #: kategorie -> přesné názvy jízdenek, které pro ni počítadlo vrací
    names: dict[str, tuple[str, ...]] = {}

    def __init__(self) -> None:
        self._prices: dict[tuple[str, str], list[tuple[str, int | None, Decimal]]] = {}
        self._zones: dict[str, dict] | None = None

    # ---- data ---------------------------------------------------------------
    @property
    def zones(self) -> dict[str, dict]:
        if self._zones is None:
            try:
                with gzip.open(DATA_DIR / self.data_file, "rt", encoding="utf-8") as fh:
                    self._zones = json.load(fh)["zones"]
            except (OSError, ValueError, KeyError) as exc:
                log.warning("Zóny %s se nepodařilo načíst: %s", self.SYSTEM, exc)
                self._zones = {}
        return self._zones

    async def _fetch(self, client: httpx.AsyncClient, zone_from: str, zone_to: str) -> None:
        raise NotImplementedError

    async def prefetch(self, pairs: set[tuple[str, str]]) -> None:
        todo = {p for p in pairs if p not in self._prices
                and p[0] in self.zones and p[1] in self.zones}
        if not todo:
            return
        limit = asyncio.Semaphore(6)
        async with httpx.AsyncClient(follow_redirects=True, headers=HEADERS, timeout=20) as client:
            async def one(pair):
                async with limit:
                    try:
                        await self._fetch(client, *pair)
                    except httpx.HTTPError as exc:
                        log.info("%s: cenu %s→%s nelze zjistit (%s)", self.SYSTEM, *pair, exc)

            await asyncio.gather(*(one(p) for p in todo))

    # ---- ceny ---------------------------------------------------------------
    def fares(self, zone_from: str, zone_to: str, passenger: Passenger) -> list[IdsFare]:
        kind = profile(passenger)
        if kind is None:
            return [IdsFare(self.SYSTEM, Decimal(0), None, "dítě do 6 let zdarma", zone_from, zone_to)]
        wanted = self.names.get(kind, ())
        return [IdsFare(self.SYSTEM, price, minutes, name, zone_from, zone_to)
                for name, minutes, price in self._prices.get((zone_from, zone_to), ())
                if name in wanted]

    def best_fare(self, zone_from: str, zone_to: str, passenger: Passenger,
                  minutes_needed: float) -> IdsFare | None:
        usable = [f for f in self.fares(zone_from, zone_to, passenger)
                  if f.minutes is None or f.minutes >= minutes_needed]
        return min(usable, key=lambda f: f.price, default=None)

    def best_between(self, zones_from: tuple[str, ...], zones_to: tuple[str, ...],
                     passenger: Passenger, minutes_needed: float) -> IdsFare | None:
        options = [self.best_fare(a, b, passenger, minutes_needed) for a in zones_from for b in zones_to]
        return min((o for o in options if o is not None), key=lambda f: f.price, default=None)


class TimTariff(OnlineTariff):
    """Tarifní počítadlo ABIRAIL (VDV, IREDO): ``POST {api}/Home/Tarify``."""

    api = ""

    async def _fetch(self, client: httpx.AsyncClient, zone_from: str, zone_to: str) -> None:
        response = await client.post(f"{self.api}/Home/Tarify", data={
            "idZonyNeboZastavkyOdkud": self.zones[zone_from]["id"],
            "idZonyNeboZastavkyKam": self.zones[zone_to]["id"],
        })
        response.raise_for_status()
        self._prices[(zone_from, zone_to)] = self.parse(response.text)

    @staticmethod
    def parse(html: str) -> list[tuple[str, int | None, Decimal]]:
        """Řádky (název, platnost v min, nejnižší cena ze sloupců hotově/karta/aplikace)."""
        rows = []
        for m in re.finditer(r"(?<=\| )([^|]+?) \| (\d+) (min|d) \|((?: \d+(?:[ ,]\d+)* Kč \|)+)",
                             page_text(html)):
            prices = [Decimal(p.replace(" ", "").replace(",", "."))
                      for p in re.findall(r"(\d[\d ]*,\d{2}) Kč", m.group(4))]
            if prices:
                minutes = int(m.group(2)) * (1440 if m.group(3) == "d" else 1)
                rows.append((m.group(1).strip(), minutes, min(prices)))
        return rows


class Vdv(TimTariff):
    SYSTEM = "VDV"
    URL = "https://www.kr-vysocina.cz/spocitat-jizdne/ms-122447"
    api = "https://tim.abirun.eu/KrajVysocina/TarifniPocitadlo"
    data_file = "ids_vdv.json.gz"
    names = {
        "full": ("Plné jednotlivé VDV", "24 hodinová celosíťová VDV"),
        "youth": ("Dítě a mládež jednotlivé VDV", "24 hodinová celosíťová Dítě a mládež VDV"),
        "student": ("Student jednotlivé VDV", "24 hodinová celosíťová Student VDV"),
        "senior": ("Osoba 65+ jednotlivé VDV", "24 hodinová celosíťová 65+ VDV"),
    }


class Iredo(TimTariff):
    SYSTEM = "IREDO"
    URL = "https://iredo.info/tarifni-kalkulator/"
    api = "https://tim.oredo.cz/TarifniPocitadlo"
    data_file = "ids_iredo.json.gz"
    names = {
        "full": ("plná",),
        "youth": ("děti a mládež 6-18 let",),
        "student": ("student 18-26 let",),
        "senior": ("osoba 65+",),
    }


class IdolTariff(OnlineTariff):
    """Tarifní počítadlo IDOL: ``GET price_table.php?n=<zóna>&v=<zóna>``."""

    SYSTEM = "IDOL"
    URL = "https://www.iidol.cz/jizdne-a-tarify/tarifni-pocitadlo/"
    api = "https://dopravnimapy.kraj-lbc.cz/projects/idol"
    data_file = "ids_idol.json.gz"
    names = {"full": ("dospělý 18+",), "youth": ("dítě 6-18",),
             "student": ("student -26",), "senior": ("osoby 65+",)}

    async def _fetch(self, client: httpx.AsyncClient, zone_from: str, zone_to: str) -> None:
        response = await client.get(f"{self.api}/price_table.php", params={
            "n": self.zones[zone_from]["id"], "v": self.zones[zone_to]["id"]})
        response.raise_for_status()
        self._prices[(zone_from, zone_to)] = self.parse(response.text)

    @staticmethod
    def parse(html: str) -> list[tuple[str, int | None, Decimal]]:
        """Jen jednotlivé jízdné; cena = nejnižší ze sloupců (hotově, karta, IDOLKA)."""
        rows = []
        for m in re.finditer(r"(?<=\| )([^|]+?) \| Jednotlivé jízdné \| (\d+) min \|((?: \d[\d ]*,- \|)+)",
                             page_text(html)):
            prices = [Decimal(p.replace(" ", "")) for p in re.findall(r"(\d[\d ]*),-", m.group(3))]
            if prices:
                rows.append((m.group(1).strip(), int(m.group(2)), min(prices)))
        return rows


vdv = Vdv()
iredo = Iredo()
idol = IdolTariff()
