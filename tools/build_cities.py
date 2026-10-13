"""Sestaví databázi evropských měst pro našeptávač (``vyhledavac/data/cities.json.gz``).

Spouští se ručně, když je potřeba data obnovit (města se mění málo)::

    python tools/build_cities.py            # stáhne zdroje a sestaví
    python tools/build_cities.py --cache D  # zdroje uloží/hledá v adresáři D

Zdroj: GeoNames (download.geonames.org, CC BY 4.0) – ``cities1000.zip``
(všechna sídla nad 1 000 obyvatel) a ``alternateNamesV2.zip`` (názvy
v jednotlivých jazycích). U každého města:

* zobrazované jméno česky, pokud ho GeoNames zná (Mnichov, Lublaň, Kodaň),
  jinak místní;
* místní jméno latinkou (München, Ljubljana, København) – to program posílá
  dopravcům, protože české exonymum („Lublaň“) jejich vyhledávání nezná;
  u zemí s cyrilicí/řečtinou anglický přepis (Sofia, Beograd → Belgrade);
* další podoby pro hledání (anglicky, německy, místní jazyky);
* počet obyvatel = důležitost v našeptávači;
* souřadnice středu (pro plánovač tras po Evropě – Transitous).

Česko se vynechává – to pokrývá ``tools/build_stations.py`` z CIS JŘ.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vyhledavac.normalize import normalize  # noqa: E402

OUT = ROOT / "vyhledavac" / "data" / "cities.json.gz"
BASE = "https://download.geonames.org/export/dump/"

#: Evropské země (kód ISO -> český název). Rusko a Turecko jen evropská část.
COUNTRIES = {
    "AD": "Andorra", "AL": "Albánie", "AT": "Rakousko", "BA": "Bosna a Hercegovina", "BE": "Belgie",
    "BG": "Bulharsko", "BY": "Bělorusko", "CH": "Švýcarsko", "CY": "Kypr", "DE": "Německo",
    "DK": "Dánsko", "EE": "Estonsko", "ES": "Španělsko", "FI": "Finsko", "FR": "Francie",
    "GB": "Spojené království", "GI": "Gibraltar", "GR": "Řecko", "HR": "Chorvatsko", "HU": "Maďarsko",
    "IE": "Irsko", "IS": "Island", "IT": "Itálie", "LI": "Lichtenštejnsko", "LT": "Litva",
    "LU": "Lucembursko", "LV": "Lotyšsko", "MC": "Monako", "MD": "Moldavsko", "ME": "Černá Hora",
    "MK": "Severní Makedonie", "MT": "Malta", "NL": "Nizozemsko", "NO": "Norsko", "PL": "Polsko",
    "PT": "Portugalsko", "RO": "Rumunsko", "RS": "Srbsko", "RU": "Rusko", "SE": "Švédsko",
    "SI": "Slovinsko", "SK": "Slovensko", "SM": "San Marino", "UA": "Ukrajina", "XK": "Kosovo",
    "TR": "Turecko",
}
#: Jazyky, ve kterých se v zemi město jmenuje (první = úřední, z něj místní jméno).
LANGS = {
    "AD": "ca", "AL": "sq", "AT": "de", "BA": "bs hr sr", "BE": "nl fr de", "BG": "bg", "BY": "be ru",
    "CH": "de fr it rm", "CY": "el tr", "DE": "de", "DK": "da", "EE": "et", "ES": "es ca gl eu",
    "FI": "fi sv", "FR": "fr", "GB": "en cy gd", "GI": "en", "GR": "el", "HR": "hr", "HU": "hu",
    "IE": "en ga", "IS": "is", "IT": "it de", "LI": "de", "LT": "lt", "LU": "fr de lb", "LV": "lv",
    "MC": "fr", "MD": "ro", "ME": "sr", "MK": "mk", "MT": "mt en", "NL": "nl fy", "NO": "no nb nn",
    "PL": "pl", "PT": "pt", "RO": "ro hu", "RS": "sr", "RU": "ru", "SE": "sv", "SI": "sl",
    "SK": "sk hu", "SM": "it", "UA": "uk", "XK": "sq sr", "TR": "tr",
}
#: Vícejazyčné země: jazyk podle regionu (kód admin1 v GeoNames).
REGION_LANG = {("CH", "GE"): "fr", ("CH", "VD"): "fr", ("CH", "NE"): "fr", ("CH", "JU"): "fr",
               ("CH", "TI"): "it", ("BE", "WAL"): "fr", ("BE", "BRU"): "fr"}
#: Zažitá česká jména, která GeoNames nemá: (země, jméno GeoNames) -> česky.
CZECH = {("BG", "Sofia"): "Sofie", ("AT", "Salzburg"): "Salcburk", ("DE", "Passau"): "Pasov",
         ("DE", "Regensburg"): "Řezno", ("GR", "Athens"): "Atény", ("PL", "Opole"): "Opolí"}
#: Jazyky jiným písmem – jejich „latinské“ varianty v GeoNames bývají nahodilé přepisy (Athínai).
CYRILLIC_GREEK = {"el", "bg", "ru", "uk", "be", "mk"}
#: Části sídel (PPLX) a zaniklá sídla vynecháme – jen by plevelila nabídku.
SKIP_CODES = {"PPLX", "PPLH", "PPLQ", "PPLW", "PPLCH"}
#: Obvody velkoměst vedené jako sídla („Paris 15 Vaugirard“, „Lyon 03“).
DISTRICT = re.compile(r"^\S+ \d{2}\b")
MAX_KEYS = 8


def latin(text: str) -> bool:
    return all(ord(c) < 0x250 or not c.isalpha() for c in text)


def fetch(name: str, cache: Path) -> Path:
    path = cache / name
    if not path.exists():
        print(f"stahuji {name} …", file=sys.stderr)
        with httpx.stream("GET", BASE + name, timeout=600, follow_redirects=True) as resp:
            resp.raise_for_status()
            with path.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
    return path


def in_europe(code: str, lat: float, lon: float) -> bool:
    if code == "RU":
        return lon < 60
    if code == "TR":
        return lon < 29.95 and lat > 40.7        # Thrákie a evropský Istanbul
    return True


def load_cities(path: Path) -> dict[int, dict]:
    cities: dict[int, dict] = {}
    with zipfile.ZipFile(path) as zf, zf.open("cities1000.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            code, feature = f[8], f[7]
            if code not in COUNTRIES or feature in SKIP_CODES or DISTRICT.match(f[1]):
                continue
            lat, lon = float(f[4]), float(f[5])
            if not in_europe(code, lat, lon):
                continue
            cities[int(f[0])] = {"name": f[1], "ascii": f[2], "country": code, "pop": int(f[14] or 0),
                                 "lat": round(lat, 4), "lon": round(lon, 4),
                                 "region": f[10],
                                 "capital": feature == "PPLC", "names": defaultdict(list)}
    return cities


def load_names(path: Path, cities: dict[int, dict]) -> None:
    wanted = {"cs", "en", "de"} | {lang for v in LANGS.values() for lang in v.split()}
    with zipfile.ZipFile(path) as zf, zf.open("alternateNamesV2.txt") as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            f = line.split("\t")
            gid = int(f[1])
            city = cities.get(gid)
            if city is None or f[2] not in wanted or f[7] == "1" or f[6] == "1":   # historické, hovorové
                continue
            preferred = f[4] == "1"
            names = city["names"][f[2]]
            names.insert(0, f[3]) if preferred else names.append(f[3])


def describe(city: dict) -> tuple[str, str, list[str]]:
    langs = LANGS[city["country"]].split()
    region = REGION_LANG.get((city["country"], city["region"]))
    if region:
        langs = [region, *(lang for lang in langs if lang != region)]
    names = city["names"]
    local = next((n for lang in langs if lang not in CYRILLIC_GREEK for n in names.get(lang, []) if latin(n)), "")
    if not local:      # cyrilice, řečtina: přepis z GeoNames (Sofia, Athens, Kyiv)
        local = city["name"] if latin(city["name"]) else city["ascii"]
    czech = CZECH.get((city["country"], city["name"])) or next(iter(names.get("cs", [])), "") or local
    keys = [czech, local, city["name"], city["ascii"], *names.get("en", [])[:2], *names.get("de", [])[:1]]
    for lang in langs:
        keys += [n for n in names.get(lang, [])[:2] if latin(n)]
    seen: list[str] = []
    for key in keys:
        n = normalize(key)
        if n and n not in seen:
            seen.append(n)
    return czech, local, seen[:MAX_KEYS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "vyhledavac-build")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    cities = load_cities(fetch("cities1000.zip", args.cache))
    load_names(fetch("alternateNamesV2.zip", args.cache), cities)
    codes = sorted(COUNTRIES)
    rows = []
    for city in sorted(cities.values(), key=lambda c: -c["pop"]):
        czech, local, keys = describe(city)
        alt = "|".join(k for k in keys if k != normalize(czech))
        rows.append([czech, codes.index(city["country"]), local if local != czech else "",
                     city["pop"], alt, 1 if city["capital"] else 0, city["lat"], city["lon"]])
    data = {"meta": {"built": dt.date.today().isoformat(), "source": "GeoNames cities1000 (CC BY 4.0)",
                     "count": len(rows)},
            "countries": [[c, COUNTRIES[c]] for c in codes], "places": rows}
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    by_country = defaultdict(int)
    for row in rows:
        by_country[codes[row[1]]] += 1
    print(f"{len(rows)} měst, {OUT.stat().st_size / 1024:.0f} kB → {OUT}")
    print(" ".join(f"{c}:{n}" for c, n in sorted(by_country.items())))


if __name__ == "__main__":
    main()
