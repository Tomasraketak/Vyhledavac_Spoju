"""Databáze zastávek a obcí pro našeptávač a detail spoje.

Data sestavuje ``tools/build_stations.py`` z jízdních řádů CIS JŘ (všechny
vlaky a linkové autobusy v ČR), zastávek PID a IDS JMK a OpenStreetMap.
U každého místa je kraj, integrovaný dopravní systém a – kde ho systém
zveřejňuje – tarifní pásmo/zóna. Místa jsou seřazená podle důležitosti
(obyvatelé obce + počet spojů), takže „Pra“ nabídne Prahu, ne zastávku Pražák.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .normalize import _ALIASES, normalize

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data" / "stations.json.gz"

KIND_LABEL = {"c": "obec", "t": "vlak", "b": "autobus"}

# Zahraniční cíle, které dopravci obsluhují – CIS JŘ je nezná.
_FOREIGN = [
    ("Vídeň", "Rakousko", "wien|vienna"), ("Wien Hbf", "Rakousko", "viden hl n"),
    ("Salzburg Hbf", "Rakousko", "salcburk"), ("Linz Hbf", "Rakousko", "linec"),
    ("Graz Hbf", "Rakousko", "styrsky hradec"), ("Bratislava", "Slovensko", "pressburg"),
    ("Bratislava hl.st.", "Slovensko", "bratislava hlavna stanica"),
    ("Košice", "Slovensko", "kassa"), ("Žilina", "Slovensko", ""),
    ("Mnichov", "Německo", "munchen|munich"), ("Berlín", "Německo", "berlin"),
    ("Drážďany", "Německo", "dresden"), ("Norimberk", "Německo", "nurnberg|nuremberg"),
    ("Pasov", "Německo", "passau"), ("Budapešť", "Maďarsko", "budapest"),
    ("Krakov", "Polsko", "krakow"), ("Varšava", "Polsko", "warszawa|warsaw"),
]
_FOREIGN_SCORE = 150_000


@dataclass(frozen=True)
class Place:
    name: str
    kind: str                     # c = obec, t = vlak, b = autobus
    obec: str = ""
    okres: str = ""
    kraj: str = ""
    ids: str = ""                 # zkratka integrovaného systému (PID, IDS JMK…)
    ids_name: str = ""
    zones: tuple[str, ...] = ()
    score: int = 0
    country: str = ""             # jen u zahraničí
    # Všechny integrované systémy, do kterých místo patří, i přesahy
    # z jiných krajů: {"IDESKA": ("932",), "PID": ("5",)}.
    systems: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)
    keys: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "obec": self.obec or None,
            "okres": self.okres or None, "kraj": self.kraj or None,
            "ids": self.ids or None, "idsName": self.ids_name or None,
            "zones": list(self.zones), "country": self.country or None,
            "systems": {k: list(v) for k, v in self.systems.items() if k != self.ids},
        }


class StationDB:
    def __init__(self, places: list[Place]):
        self.places = places
        self.exact: dict[str, list[Place]] = {}
        for place in places:
            for key in place.keys:
                self.exact.setdefault(key, []).append(place)

    # ---- načtení ---------------------------------------------------------------
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StationDB:
        kraje = data["kraje"]
        ids = data["ids"]
        okresy = data["okresy"]
        places: list[Place] = []
        for row in data["places"]:
            name, kind, obec, okres_i, kraj_i, ids_i, zones, score = row[:8]
            alt = row[8] if len(row) > 8 else ""
            keys = (normalize(name), *(a for a in alt.split("|") if a))
            systems: dict[str, tuple[str, ...]] = {}
            for part in (row[9] if len(row) > 9 else "").split(";"):
                if ":" in part:
                    sys_i, sys_zones = part.split(":", 1)
                    systems[ids[int(sys_i)][0]] = tuple(z for z in sys_zones.split(",") if z)
            places.append(Place(
                name=name, kind=kind, obec=obec, okres=okresy[okres_i][0] if okres_i >= 0 else "",
                kraj=kraje[kraj_i], ids=ids[ids_i][0], ids_name=ids[ids_i][1],
                zones=tuple(z for z in zones.split(",") if z), score=score, keys=keys,
                systems=systems,
            ))
        for name, country, alt in _FOREIGN:
            keys = (normalize(name), *(normalize(a) for a in alt.split("|") if a))
            places.append(Place(name=name, kind="c", country=country, score=_FOREIGN_SCORE, keys=keys))
        places.sort(key=lambda p: -p.score)
        return cls(places)

    @classmethod
    def load(cls, path: Path = DATA) -> StationDB:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))

    # ---- našeptávač -------------------------------------------------------------
    def suggest(self, query: str, limit: int = 10) -> list[Place]:
        """Místa odpovídající rozepsanému textu, nejdůležitější první.

        Pořadí: shoda od začátku názvu, pak shoda všech slov dotazu se
        začátky slov názvu („hl brno“ najde „Brno hl. n.“). V rámci skupiny
        rozhoduje důležitost.
        """
        q = normalize(query)
        if not q:
            return []
        words = q.split()
        starts: list[Place] = []
        others: list[Place] = []
        for place in self.places:           # seřazeno podle důležitosti
            best = 2
            for key in place.keys:
                if key.startswith(q):
                    best = 0
                    break
                if best > 1 and _all_words(words, key):
                    best = 1
            if best == 0:
                starts.append(place)
                if len(starts) >= limit:
                    break
            elif best == 1 and len(others) < limit:
                others.append(place)
        return (starts + others)[:limit]

    # ---- jméno od dopravce -> místo ----------------------------------------------
    def info(self, name: str) -> Place | None:
        """Najde místo podle jména, jak ho píše dopravce („Praha hl.n.“…).

        Nejdřív přesná shoda, pak čím dál kratší začátek jména – „Brno, ÚAN
        Zvonařka (stan. 5)“ tak skončí aspoň u obce Brno.
        """
        words = [_alias(w) for w in normalize(name).split()]
        for end in range(len(words), 0, -1):
            hits = self.exact.get(" ".join(words[:end]))
            if hits:
                return max(hits, key=lambda p: (p.kind != "b", p.score))
        return None


def _all_words(words: list[str], key: str) -> bool:
    parts = key.split()
    return all(any(p.startswith(w) for p in parts) for w in words)


@lru_cache(maxsize=None)
def _alias_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, variants in _ALIASES.items():
        for variant in variants:
            out[normalize(variant)] = normalize(canonical)
    return out


def _alias(word: str) -> str:
    """Cizojazyčné jméno české obce -> české ('prague' -> 'praha')."""
    target = _alias_map().get(word)
    return target if target in ("praha", "brno") else word


@lru_cache(maxsize=1)
def database() -> StationDB | None:
    """Sdílená databáze; ``None``, pokud datový soubor chybí nebo je vadný."""
    try:
        return StationDB.load()
    except (OSError, ValueError, KeyError, IndexError) as exc:
        log.warning("Databázi zastávek se nepodařilo načíst: %s", exc)
        return None


def suggest(query: str, limit: int = 10) -> list[dict[str, Any]]:
    db = database()
    return [p.to_dict() for p in db.suggest(query, limit)] if db else []


def place(name: str) -> Place | None:
    db = database()
    return db.info(name) if db and name else None


def rail_place(name: str) -> Place | None:
    """Jako ``place``, ale u shody jména dá přednost železniční stanici před obcí.

    „Beroun“ je obec i nádraží – pro výpočet zón vlaku chceme nádraží.
    """
    db = database()
    if db is None or not name:
        return None
    hits = db.exact.get(normalize(name), [])
    trains = [p for p in hits if p.kind == "t"]
    return max(trains, key=lambda p: p.score) if trains else db.info(name)


def is_domestic(*names: str) -> bool:
    """Leží všechna místa v Česku? (Neznámé místo = nevíme, tedy ne.)"""
    found = [place(n) for n in names]
    return all(p is not None and not p.country for p in found)


@lru_cache(maxsize=4096)
def info(name: str) -> dict[str, Any] | None:
    db = database()
    place = db.info(name) if db and name else None
    return place.to_dict() if place else None
