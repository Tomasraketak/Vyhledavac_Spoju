"""Databáze zastávek a obcí pro našeptávač a detail spoje.

Zahraniční města (všechna evropská sídla nad 5 000 obyvatel) přidává
``data/cities.json.gz`` z GeoNames (``tools/build_cities.py``) – česky
(Mnichov, Lublaň), s místním jménem pro dopravce (München, Ljubljana).

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
CITIES = Path(__file__).parent / "data" / "cities.json.gz"

KIND_LABEL = {"c": "obec", "t": "vlak", "b": "autobus"}

# Zahraniční nádraží, která dopravci od nás obsluhují (města jsou v cities.json.gz).
_FOREIGN = [
    ("Wien Hbf", "AT", "viden hl n"), ("Salzburg Hbf", "AT", "salcburk hl n"),
    ("Linz Hbf", "AT", "linec hl n"), ("Graz Hbf", "AT", "styrsky hradec hl n"),
    ("Bratislava hl.st.", "SK", "bratislava hlavna stanica"),
]
_FOREIGN_SCORE = 150_000
#: Zahraniční město = obyvatelé / CITY_DIVISOR (české obce mají obyvatele + spoje),
#: takže „Ber“ nabídne Berlín, ale Beroun před Bernem.
CITY_DIVISOR = 10


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
    country: str = ""             # jen u zahraničí – česky („Německo“)
    code: str = ""                # kód země u zahraničí („DE“)
    local: str = ""               # místní jméno pro dopravce („München“), je-li jiné
    lat: float | None = None      # souřadnice (jen zahraniční města)
    lon: float | None = None
    # Všechny integrované systémy, do kterých místo patří, i přesahy
    # z jiných krajů: {"IDESKA": ("932",), "PID": ("5",)}.
    systems: dict[str, tuple[str, ...]] = field(default_factory=dict, compare=False)
    keys: tuple[str, ...] = field(default=(), compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "obec": self.obec or None,
            "okres": self.okres or None, "kraj": self.kraj or None,
            "ids": self.ids or None, "idsName": self.ids_name or None,
            "zones": list(self.zones), "country": self.country or None, "code": self.code or None,
            "local": self.local or None,
            "systems": {k: list(v) for k, v in self.systems.items() if k != self.ids},
        }


class StationDB:
    def __init__(self, places: list[Place]):
        self.places = places
        self.exact: dict[str, list[Place]] = {}
        #: první slovo klíče -> (slova klíče, místo); pro zkratky „n.“ = „nad“
        self.by_first: dict[str, list[tuple[list[str], Place]]] = {}
        for place in places:
            for key in place.keys:
                self.exact.setdefault(key, []).append(place)
                parts = key.split()
                if parts:
                    self.by_first.setdefault(parts[0], []).append((parts, place))

    # ---- načtení ---------------------------------------------------------------
    @classmethod
    def from_json(cls, data: dict[str, Any], cities: dict[str, Any] | None = None) -> StationDB:
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
        if cities:
            places += _cities(cities)
            names = {code: name for code, name in cities["countries"]}
        else:
            names = {}
        for name, code, alt in _FOREIGN:
            keys = (normalize(name), *(normalize(a) for a in alt.split("|") if a))
            places.append(Place(name=name, kind="t", country=names.get(code, code), code=code,
                                score=_FOREIGN_SCORE, keys=keys))
        places.sort(key=lambda p: -p.score)
        return cls(places)

    @classmethod
    def load(cls, path: Path = DATA, cities: Path | None = CITIES) -> StationDB:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        extra = None
        if cities is not None:
            try:
                with gzip.open(cities, "rt", encoding="utf-8") as fh:
                    extra = json.load(fh)
            except (OSError, ValueError) as exc:
                log.warning("Databázi zahraničních měst se nepodařilo načíst: %s", exc)
        return cls.from_json(data, extra)

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
                if best > 1 and words[0] in key and _all_words(words, key):
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
        head, slash, river = name.partition("/")
        if slash and river.strip():
            # rakouský zápis „Braunau/Inn“, „Neumarkt/Wallersee“ = „Braunau am Inn“ …
            # (samotné „Neumarkt“ je v databázi italská Egna)
            river = river.split()[0]
            for joint in ("am", "an der", "in", "im", "ob der", "bei"):
                hits = self.exact.get(normalize(f"{head} {joint} {river}"))
                if hits:
                    return max(hits, key=lambda p: (p.kind != "b", p.score))
        words = [_alias(w) for w in normalize(name).split()]
        for variant in (words, _folded(words)):
            for end in range(len(variant), 0, -1):
                hits = self.exact.get(" ".join(variant[:end])) or self._abbreviated(variant[:end])
                if hits:
                    # stejné jméno: rozhodne důležitost (Vídeň u Žďáru < Vídeň v Rakousku)
                    return max(hits, key=lambda p: (p.kind != "b", p.score))
            if _folded(words) == words:
                break
        return None

    def _abbreviated(self, words: list[str]) -> list[Place]:
        """Shoda se zkratkami, jak je píše jízdní řád: „Veselí n.Lužnicí“ =
        „Veselí nad Lužnicí“, „Ústí n.L.“, „Lázně Bohdaneč“ … Každé slovo musí
        sedět celé, nebo být krátkým začátkem (nejvýš 4 písmena) slova názvu."""
        if len(words) < 2:
            return []
        out = []
        for parts, place in self.by_first.get(words[0], ()):
            if len(parts) == len(words) and all(
                    w == p or (len(w) <= 4 and p.startswith(w)) for w, p in zip(words[1:], parts[1:])):
                out.append(place)
        return out


#: České exonymy, které v databázi měst (z Wikidat) chybí: „Segedín“ = Szeged.
EXONYMS: dict[str, tuple[str, ...]] = {
    "Szeged": ("Segedín",), "Pécs": ("Pětikostelí",), "Stockholm": ("Štokholm",), "Innsbruck": ("Inšpruk",),
    "Klagenfurt": ("Celovec",), "Klagenfurt am Wörthersee": ("Celovec",), "Miskolc": ("Miškovec",),
    "Szombathely": ("Sombathej",), "Sibiu": ("Sibiň",), "İstanbul": ("Cařihrad",), "Istanbul": ("Cařihrad",),
}


#: Asijská část Turecka (databáze měst z GeoNames má jen evropské země a Thrákii):
#: hlavní města na železnici TCDD – jméno, místní jméno, obyvatel, hlavní město, šířka, délka.
EXTRA_CITIES: list[tuple[str, str, str, int, bool, float, float]] = [
    ("Ankara", "TR", "Ankara", 5700000, True, 39.9334, 32.8597),
    ("Izmir", "TR", "İzmir", 3000000, False, 38.4237, 27.1428),
    ("Bursa", "TR", "Bursa", 2000000, False, 40.1828, 29.0667),
    ("Adana", "TR", "Adana", 1800000, False, 37.0000, 35.3213),
    ("Gaziantep", "TR", "Gaziantep", 2100000, False, 37.0662, 37.3833),
    ("Konya", "TR", "Konya", 1300000, False, 37.8746, 32.4932),
    ("Kayseri", "TR", "Kayseri", 1100000, False, 38.7312, 35.4787),
    ("Mersin", "TR", "Mersin", 1000000, False, 36.8121, 34.6415),
    ("Diyarbakır", "TR", "Diyarbakır", 1100000, False, 37.9144, 40.2306),
    ("Eskişehir", "TR", "Eskişehir", 800000, False, 39.7767, 30.5206),
    ("Samsun", "TR", "Samsun", 700000, False, 41.2928, 36.3313),
    ("Denizli", "TR", "Denizli", 600000, False, 37.7765, 29.0864),
    ("Malatya", "TR", "Malatya", 500000, False, 38.3552, 38.3095),
    ("Adapazarı", "TR", "Adapazarı", 500000, False, 40.7569, 30.3781),
    ("Van", "TR", "Van", 500000, False, 38.5012, 43.3729),
    ("Erzurum", "TR", "Erzurum", 400000, False, 39.9043, 41.2679),
    ("Sivas", "TR", "Sivas", 400000, False, 39.7477, 37.0179),
    ("Elazığ", "TR", "Elazığ", 400000, False, 38.6743, 39.2232),
    ("Balıkesir", "TR", "Balıkesir", 330000, False, 39.6484, 27.8826),
    ("Kütahya", "TR", "Kütahya", 250000, False, 39.4167, 29.9833),
    ("Afyonkarahisar", "TR", "Afyonkarahisar", 250000, False, 38.7507, 30.5567),
    ("Karaman", "TR", "Karaman", 150000, False, 37.1759, 33.2287),
    ("Kars", "TR", "Kars", 110000, False, 40.6013, 43.0975),
    ("Zonguldak", "TR", "Zonguldak", 100000, False, 41.4564, 31.7987),
    ("Tatvan", "TR", "Tatvan", 70000, False, 38.5066, 42.2816),
    ("Kapıkule", "TR", "Kapıkule", 1000, False, 41.7167, 26.3500),
]


def _cities(data: dict[str, Any]) -> list[Place]:
    countries = data["countries"]
    out = []
    rows = list(data["places"])
    index = {code: i for i, (code, _) in enumerate(countries)}
    for name, code, local, population, capital, lat, lon in EXTRA_CITIES:
        if code in index:
            rows.append([name, index[code], local if local != name else "", population, "", capital, lat, lon])
    for row in rows:
        name, country_i, local, population, alt, capital = row[:6]
        lat, lon = (row[6], row[7]) if len(row) > 7 else (None, None)
        code, country = countries[country_i]
        keys = (normalize(name), *(a for a in alt.split("|") if a),
                *(normalize(x) for x in EXONYMS.get(local or name, ())))
        score = population // CITY_DIVISOR * (2 if capital else 1)
        out.append(Place(name=name, kind="c", country=country, code=code, local=local, score=score, keys=keys,
                         lat=lat, lon=lon))
    return out


def _folded(words: list[str]) -> list[str]:
    """Přepis bez přehlásek, jak ho píší cizí systémy: „Koebenhavn“ = København,
    „Muenchen“ = München, „Goeteborg“ = Göteborg."""
    return [w.replace("oe", "o").replace("ae", "a").replace("ue", "u") for w in words]


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


def provider_name(name: str) -> str:
    """Jméno, kterému rozumí zahraniční dopravce: české exonymum zahraničního
    města přeloží na místní jméno („Lublaň“ → „Ljubljana“), jinak beze změny."""
    db = database()
    if db is None or not name:
        return name
    best = db.info(name)
    if best is None or best.kind != "c" or not best.code:
        return name
    if best.local and normalize(best.name) == normalize(name):
        return best.local
    if not best.local and normalize(name) in {normalize(x) for x in EXONYMS.get(best.name, ())}:
        return best.name              # „Segedín“ → „Szeged“ (v databázi je jen místní jméno)
    return name


#: Jak daleko smí stanice dopravce ležet od zadaného města, aby to bylo ono.
NEAR_KM = 60.0


def far_from(name: str, lat: float | None, lon: float | None, km: float = NEAR_KM) -> bool:
    """Leží stanice dopravce (souřadnice) prokazatelně jinde než zadané město?
    „Brašov“ → „Braunau/Inn“ je 1000 km vedle. Neznámé souřadnice = nevíme (False)."""
    from . import geo

    found = place(name)
    if found is None or found.lat is None or found.lon is None or lat is None or lon is None:
        return False
    return geo.km((found.lat, found.lon), (lat, lon)) > km


def countries_for(name: str) -> set[str]:
    """Všechny země, kde leží místo toho jména („Jesenice“: Česko i Slovinsko)."""
    db = database()
    if db is None or not name:
        return set()
    words = [_alias(w) for w in normalize(name).split()]
    for variant in (words, _folded(words)):
        for end in range(len(variant), 0, -1):
            hits = db.exact.get(" ".join(variant[:end])) or db._abbreviated(variant[:end])
            if hits:
                return {p.code or "CZ" for p in hits}
    return set()


@lru_cache(maxsize=None)
def country_name(code: str) -> str:
    """Český název státu podle kódu („DE“ → „Německo“); neznámý = ""."""
    db = database()
    for p in db.places if db else ():
        if p.code == code and p.country:
            return p.country
    return ""


def is_domestic(*names: str) -> bool:
    """Leží všechna místa v Česku? (Neznámé místo = nevíme, tedy ne.)"""
    found = [place(n) for n in names]
    return all(p is not None and not p.country for p in found)


@lru_cache(maxsize=4096)
def info(name: str) -> dict[str, Any] | None:
    db = database()
    place = db.info(name) if db and name else None
    return place.to_dict() if place else None
