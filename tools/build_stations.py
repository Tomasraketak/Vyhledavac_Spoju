"""Sestaví databázi zastávek pro našeptávač (``vyhledavac/data/stations.json.gz``).

Spouští se ručně, když je potřeba data obnovit (jednou za čas – jízdní řády
se mění, zastávky jen málo)::

    python tools/build_stations.py            # stáhne zdroje a sestaví
    python tools/build_stations.py --cache D  # zdroje uloží/hledá v adresáři D

Zdroje (všechny veřejné a bez registrace):

* CIS JŘ – JDF: všechny linky veřejné autobusové dopravy v ČR
  (portal.cisjr.cz/pub/jdf/JDF.zip). Názvy zastávek, okres a počet spojů,
  které na zastávce staví – to je měřítko důležitosti.
* CIS JŘ – CZPTT: celostátní jízdní řád vlaků (portal.cisjr.cz/pub/draha/…).
  Názvy stanic a zastávek a počet vlaků, které v nich zastavují.
* OpenStreetMap (Overpass API): kraj a poloha každé železniční stanice.
* PID – seznam zastávek s tarifními pásmy (data.pid.cz, CC BY 4.0).
* IDS JMK – GTFS s tarifními zónami (kordis-jmk.cz).

Ostatní integrované systémy zóny volně nezveřejňují – u nich databáze uvádí
kraj a systém, zónu ne.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vyhledavac.normalize import normalize  # noqa: E402

OUTPUT = ROOT / "vyhledavac" / "data" / "stations.json.gz"

URLS = {
    "JDF.zip": "https://portal.cisjr.cz/pub/jdf/JDF.zip",
    "JR.zip": "https://portal.cisjr.cz/pub/draha/celostatni/szdc/{year}/JR{year}.zip",
    "pid_stops.json": "https://data.pid.cz/stops/json/stops.json",
    "jmk_gtfs.zip": "https://kordis-jmk.cz/gtfs/gtfs.zip",
}
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_QUERY = (
    '[out:json][timeout:170];area["ISO3166-2"~"^CZ-[0-9]{2}$"]->.k;'
    'foreach.k->.a(.a out tags; node(area.a)["railway"~"^(station|halt)$"]; out body;);'
)
OVERPASS_PLACES = (
    '[out:json][timeout:170];area["ISO3166-2"~"^CZ-[0-9]{2}$"]->.k;'
    'foreach.k->.a(.a out tags; node(area.a)["place"~"^(city|town|village)$"]["population"]; out tags;);'
)

# ---------------------------------------------------------------------------
#  Kraje, okresy, integrované systémy
# ---------------------------------------------------------------------------
KRAJE = [
    "Hlavní město Praha", "Středočeský kraj", "Jihočeský kraj", "Plzeňský kraj",
    "Karlovarský kraj", "Ústecký kraj", "Liberecký kraj", "Královéhradecký kraj",
    "Pardubický kraj", "Kraj Vysočina", "Jihomoravský kraj", "Olomoucký kraj",
    "Zlínský kraj", "Moravskoslezský kraj",
]
ISO_KRAJ = {
    "CZ-10": 0, "CZ-20": 1, "CZ-31": 2, "CZ-32": 3, "CZ-41": 4, "CZ-42": 5, "CZ-51": 6,
    "CZ-52": 7, "CZ-53": 8, "CZ-63": 9, "CZ-64": 10, "CZ-71": 11, "CZ-72": 12, "CZ-80": 13,
}

# Integrovaný dopravní systém kraje: zkratka, celý název.
IDS = [
    ("PID", "Pražská integrovaná doprava"),
    ("IDS JK", "Integrovaný dopravní systém Jihočeského kraje"),
    ("IDPK", "Integrovaná doprava Plzeňského kraje"),
    ("IDS KK", "Integrovaný dopravní systém Karlovarského kraje"),
    ("DÚK", "Doprava Ústeckého kraje"),
    ("IDOL", "Integrovaná doprava Libereckého kraje"),
    ("IREDO", "Integrovaná regionální doprava (Královéhradecký a Pardubický kraj)"),
    ("VDV", "Veřejná doprava Vysočiny"),
    ("IDS JMK", "Integrovaný dopravní systém Jihomoravského kraje"),
    ("IDSOK", "Integrovaný dopravní systém Olomouckého kraje"),
    ("IDZK", "Integrovaná doprava Zlínského kraje"),
    ("ODIS", "Ostravský dopravní integrovaný systém"),
]
KRAJ_IDS = [0, 0, 1, 2, 3, 4, 5, 6, 6, 7, 8, 9, 10, 11]
PID, JMK = 0, 8

# Okresy podle kódů CIS JŘ: kód -> (název, kraj).
OKRESY = {
    "AB": ("Praha", 0),
    "BN": ("Benešov", 1), "BE": ("Beroun", 1), "KD": ("Kladno", 1), "KO": ("Kolín", 1),
    "KH": ("Kutná Hora", 1), "ME": ("Mělník", 1), "MB": ("Mladá Boleslav", 1),
    "NB": ("Nymburk", 1), "PH": ("Praha-východ", 1), "PZ": ("Praha-západ", 1),
    "PB": ("Příbram", 1), "RA": ("Rakovník", 1),
    "CB": ("České Budějovice", 2), "CK": ("Český Krumlov", 2), "JH": ("Jindřichův Hradec", 2),
    "PI": ("Písek", 2), "PT": ("Prachatice", 2), "ST": ("Strakonice", 2), "TA": ("Tábor", 2),
    "DO": ("Domažlice", 3), "KT": ("Klatovy", 3), "PM": ("Plzeň-město", 3),
    "PJ": ("Plzeň-jih", 3), "PS": ("Plzeň-sever", 3), "RO": ("Rokycany", 3), "TC": ("Tachov", 3),
    "CH": ("Cheb", 4), "KV": ("Karlovy Vary", 4), "SO": ("Sokolov", 4),
    "DC": ("Děčín", 5), "CV": ("Chomutov", 5), "LT": ("Litoměřice", 5), "LN": ("Louny", 5),
    "MO": ("Most", 5), "TP": ("Teplice", 5), "UL": ("Ústí nad Labem", 5),
    "CL": ("Česká Lípa", 6), "JN": ("Jablonec nad Nisou", 6), "LI": ("Liberec", 6),
    "SM": ("Semily", 6),
    "HK": ("Hradec Králové", 7), "JC": ("Jičín", 7), "NA": ("Náchod", 7),
    "RK": ("Rychnov nad Kněžnou", 7), "TU": ("Trutnov", 7),
    "CR": ("Chrudim", 8), "PU": ("Pardubice", 8), "PA": ("Pardubice", 8), "SY": ("Svitavy", 8),
    "UO": ("Ústí nad Orlicí", 8),
    "HB": ("Havlíčkův Brod", 9), "JI": ("Jihlava", 9), "PE": ("Pelhřimov", 9),
    "TR": ("Třebíč", 9), "ZR": ("Žďár nad Sázavou", 9),
    "BK": ("Blansko", 10), "BM": ("Brno-město", 10), "BO": ("Brno-venkov", 10),
    "BI": ("Brno-venkov", 10), "BV": ("Břeclav", 10), "HO": ("Hodonín", 10),
    "VY": ("Vyškov", 10), "ZN": ("Znojmo", 10),
    "JE": ("Jeseník", 11), "OL": ("Olomouc", 11), "PV": ("Prostějov", 11),
    "PR": ("Přerov", 11), "SU": ("Šumperk", 11),
    "KM": ("Kroměříž", 12), "UH": ("Uherské Hradiště", 12), "VS": ("Vsetín", 12),
    "ZL": ("Zlín", 12),
    "BR": ("Bruntál", 13), "FM": ("Frýdek-Místek", 13), "KI": ("Karviná", 13),
    "NJ": ("Nový Jičín", 13), "OP": ("Opava", 13), "OV": ("Ostrava-město", 13),
}


# ---------------------------------------------------------------------------
#  Stažení
# ---------------------------------------------------------------------------
def fetch(cache: Path, name: str, url: str, *, refresh: bool) -> Path:
    target = cache / name
    if target.exists() and target.stat().st_size and not refresh:
        return target
    print(f"  stahuji {url}", flush=True)
    for attempt in range(4):
        try:
            with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
                r.raise_for_status()
                tmp = target.with_suffix(".part")
                with tmp.open("wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        fh.write(chunk)
            tmp.replace(target)
            return target
        except httpx.HTTPError as exc:
            print(f"    pokus {attempt + 1} selhal: {exc}", flush=True)
    raise SystemExit(f"Nepodařilo se stáhnout {url}")


def fetch_overpass(cache: Path, name: str, query: str, *, refresh: bool) -> Path:
    target = cache / name
    if target.exists() and target.stat().st_size and not refresh:
        return target
    for attempt in range(3):
        for url in OVERPASS:
            print(f"  Overpass {url}", flush=True)
            try:
                r = httpx.post(url, data={"data": query}, timeout=200)
                r.raise_for_status()
                if r.json().get("elements"):
                    target.write_bytes(r.content)
                    return target
            except (httpx.HTTPError, ValueError) as exc:
                print(f"    selhalo: {exc}", flush=True)
    raise SystemExit("Overpass API neodpovídá – zkuste to později.")


# ---------------------------------------------------------------------------
#  Autobusy – JDF
# ---------------------------------------------------------------------------
def _jdf_rows(raw: bytes):
    try:
        text = raw.decode("cp1250")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    text = text.lstrip("﻿")
    for row in csv.reader(io.StringIO(text.replace(";\r\n", "\n").replace(";\n", "\n"))):
        if row:
            yield [c.strip() for c in row]


def _find(names: dict[str, str], wanted: str) -> str | None:
    return names.get(wanted.lower())


def bus_display_name(obec: str, cast: str, misto: str) -> str:
    return ", ".join(p for p in (obec, cast, misto) if p)


def parse_jdf(path: Path) -> dict[tuple[str, str], dict]:
    """(jméno, okres) -> {obec, okres, departures}."""
    stops: dict[tuple[str, str], dict] = {}
    with zipfile.ZipFile(path) as outer:
        names = outer.namelist()
        for n, inner_name in enumerate(names):
            if n % 2000 == 0:
                print(f"  JDF {n}/{len(names)}", flush=True)
            try:
                inner = zipfile.ZipFile(io.BytesIO(outer.read(inner_name)))
            except zipfile.BadZipFile:
                continue
            files = {f.lower(): f for f in inner.namelist()}
            zas, zasspoje = _find(files, "zastavky.txt"), _find(files, "zasspoje.txt")
            if not zas:
                continue
            local: dict[str, tuple[str, str]] = {}
            for row in _jdf_rows(inner.read(zas)):
                if len(row) < 6:
                    continue
                number, obec, cast, misto, okres, stat = row[:6]
                if stat and stat != "CZ":
                    continue
                okres = okres.upper()
                if okres not in OKRESY or not obec:
                    continue
                name = bus_display_name(obec, cast, misto)
                key = (name, okres)
                stops.setdefault(key, {"obec": obec, "okres": okres, "departures": 0})
                local[number] = key
            if not zasspoje:
                continue
            for row in _jdf_rows(inner.read(zasspoje)):
                if len(row) < 12:
                    continue
                key = local.get(row[3])
                if key is None:
                    continue
                times = row[10:12]
                if any(t[:1].isdigit() for t in times):
                    stops[key]["departures"] += 1
    return stops


# ---------------------------------------------------------------------------
#  Vlaky – CZPTT
# ---------------------------------------------------------------------------
def parse_czptt(path: Path) -> dict[str, dict]:
    """kód stanice -> {name, trains}. Jen tuzemské stanice, kde vlak staví pro cestující."""
    stations: dict[str, dict] = {}
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".xml")]
        for n, name in enumerate(names):
            if n % 2000 == 0:
                print(f"  CZPTT {n}/{len(names)}", flush=True)
            try:
                root = ET.fromstring(z.read(name))
            except ET.ParseError:
                continue
            seen: set[str] = set()
            for loc in root.iter("CZPTTLocation"):
                acts = {a.text for a in loc.iter("TrainActivityType")}
                if "0001" not in acts:
                    continue
                location = loc.find("Location")
                if location is None or location.findtext("CountryCodeISO") != "CZ":
                    continue
                code = location.findtext("LocationPrimaryCode") or ""
                label = (location.findtext("PrimaryLocationName") or "").strip()
                if not code or not label or code in seen:
                    continue
                seen.add(code)
                entry = stations.setdefault(code, {"name": label, "trains": 0})
                entry["trains"] += 1
    return stations


# ---------------------------------------------------------------------------
#  Zóny – PID, IDS JMK
# ---------------------------------------------------------------------------
_ZONE_OK = re.compile(r"^[0-9PB]{1,3}$")


def _zones(values) -> str:
    out: list[str] = []
    for value in values:
        for z in (value or "").split(","):
            z = z.strip()
            if _ZONE_OK.match(z) and z not in out:
                out.append(z)
    order = {"P": -2, "0": -1, "B": 0}
    return ",".join(sorted(out, key=lambda z: (order.get(z, 1), int(z) if z.isdigit() else 0)))


def parse_pid(path: Path):
    """Vrací (zastávky podle (jméno, okres), stanice podle kódu CZPTT)."""
    groups = json.loads(path.read_text("utf-8"))["stopGroups"]
    by_name: dict[tuple[str, str], str] = {}
    by_code: dict[str, str] = {}
    for g in groups:
        zones = _zones(s.get("zone") for s in g.get("stops", []))
        if not zones:
            continue
        name, town = g.get("name", ""), g.get("municipality", "")
        okres = (g.get("districtCode") or "").upper()
        full = name if normalize(name).startswith(normalize(town)) else f"{town}, {name}"
        for candidate in {name, full, g.get("idosName") or name}:
            by_name.setdefault((normalize(candidate), okres), zones)
        if g.get("mainTrafficType") == "train" or g.get("idosCategory") == 600003:
            cis = str(g.get("cis") or "")
            if len(cis) == 7 and cis.startswith("54"):
                by_code[cis[2:]] = zones
    return by_name, by_code


def parse_jmk(path: Path):
    """Vrací (jméno -> zóny, seznam (lat, lon, zóna))."""
    by_name: dict[str, Counter] = defaultdict(Counter)
    points: list[tuple[float, float, str]] = []
    with zipfile.ZipFile(path) as z:
        text = z.read("stops.txt").decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        zone = _zones([row.get("zone_id")])
        if not zone or row.get("location_type") not in ("0", "", None):
            continue
        name = row["stop_name"]
        by_name[normalize(name)][zone] += 1
        # brněnské zastávky jsou bez "Brno, " ("Královo Pole, nádraží")
        by_name[normalize("Brno, " + name)][zone] += 1
        try:
            points.append((float(row["stop_lat"]), float(row["stop_lon"]), zone))
        except (KeyError, ValueError):
            pass
    return {k: c.most_common(1)[0][0] for k, c in by_name.items()}, points


def _nearest_zone(points, lat: float, lon: float, limit_m: float = 700) -> str | None:
    best, best_d = None, limit_m
    k = math.cos(math.radians(lat))
    for plat, plon, zone in points:
        if abs(plat - lat) > 0.01:
            continue
        d = math.hypot((plat - lat) * 111_320, (plon - lon) * 111_320 * k)
        if d < best_d:
            best, best_d = zone, d
    return best


# ---------------------------------------------------------------------------
#  Sestavení
# ---------------------------------------------------------------------------
def parse_osm(path: Path) -> dict[str, dict]:
    """kód stanice (5 číslic SR70) -> {kraj, lat, lon, name}."""
    out: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    kraj = None
    for el in json.loads(path.read_text("utf-8"))["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "area":
            kraj = ISO_KRAJ.get(tags.get("ISO3166-2", ""))
            continue
        if kraj is None:
            continue
        info = {"kraj": kraj, "lat": el.get("lat"), "lon": el.get("lon"), "name": tags.get("name", "")}
        ref = tags.get("railway:ref", "")
        uic = tags.get("uic_ref", "")
        code = ref[:5] if len(ref) >= 5 else (uic[2:7] if uic.startswith("54") else "")
        if code:
            out.setdefault(code, info)
        if info["name"]:
            by_name.setdefault(normalize(info["name"]), info)
    out["__by_name__"] = by_name  # type: ignore[assignment]
    return out


def short_key(name: str) -> str:
    """Klíč odolný vůči zkratkám CIS: 'Ústí n.L.' i 'Ústí nad Labem' -> 'usti n l'."""
    words = normalize(name).split()
    return " ".join(words[:1] + [w[0] for w in words[1:]])


def parse_places(path: Path) -> dict[int, dict[str, tuple[str, int]]]:
    """kraj -> {klíč názvu: (plný název, počet obyvatel)} z OSM."""
    out: dict[int, dict[str, tuple[str, int]]] = defaultdict(dict)
    kraj = None
    for el in json.loads(path.read_text("utf-8"))["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "area":
            kraj = ISO_KRAJ.get(tags.get("ISO3166-2", ""))
            continue
        name = tags.get("name")
        if kraj is None or not name:
            continue
        try:
            pop = int(re.sub(r"\D", "", tags.get("population", "")) or 0)
        except ValueError:
            continue
        for key in (normalize(name), "~" + short_key(name)):
            if pop > out[kraj].get(key, ("", -1))[1]:
                out[kraj][key] = (name, pop)
    return out


def _with_prague(zones: str, kraj: int) -> str:
    """Celá Praha leží v pásmu P (stanice na okraji navíc i v 0/B)."""
    if kraj != 0:
        return zones
    parts = [z for z in zones.split(",") if z]
    return ",".join(["P"] + [z for z in parts if z != "P"])


def build(cache: Path, *, refresh: bool, year: int) -> dict:
    cache.mkdir(parents=True, exist_ok=True)
    jdf = fetch(cache, "JDF.zip", URLS["JDF.zip"], refresh=refresh)
    rail = fetch(cache, f"JR{year}.zip", URLS["JR.zip"].format(year=year), refresh=refresh)
    pid = fetch(cache, "pid_stops.json", URLS["pid_stops.json"], refresh=refresh)
    jmk = fetch(cache, "jmk_gtfs.zip", URLS["jmk_gtfs.zip"], refresh=refresh)
    osm = fetch_overpass(cache, "osm_rail.json", OVERPASS_QUERY, refresh=refresh)
    osm_places = fetch_overpass(cache, "osm_places.json", OVERPASS_PLACES, refresh=refresh)

    print("Zpracovávám autobusy (JDF)…", flush=True)
    bus = parse_jdf(jdf)
    print(f"  {len(bus)} autobusových zastávek", flush=True)
    print("Zpracovávám vlaky (CZPTT)…", flush=True)
    trains = parse_czptt(rail)
    print(f"  {len(trains)} železničních stanic a zastávek", flush=True)
    pid_names, pid_codes = parse_pid(pid)
    jmk_names, jmk_points = parse_jmk(jmk)
    osm_codes = parse_osm(osm)
    osm_names = osm_codes.pop("__by_name__")

    # Obce: název -> okresy (s počtem spojů), pro přiřazení vlakových stanic.
    towns: dict[str, Counter] = defaultdict(Counter)
    for (_, okres), s in bus.items():
        towns[s["obec"]][okres] += s["departures"] + 1
    town_index: dict[str, list[str]] = defaultdict(list)
    for town in towns:
        town_index[normalize(town).split(" ")[0]].append(town)

    def town_of(station: str) -> str | None:
        """Obec, ve které stanice leží: nejdelší název obce, kterým jméno začíná."""
        norm = normalize(station)
        best = None
        for town in town_index.get(norm.split(" ")[0], ()):
            nt = normalize(town)
            if (norm == nt or norm.startswith(nt + " ")) and (best is None or len(nt) > len(normalize(best))):
                best = town
        return best

    def zone_for(name: str, okres: str, kraj: int) -> tuple[int, str]:
        key = normalize(name)
        if (key, okres) in pid_names:
            return PID, pid_names[(key, okres)]
        if kraj == 10 and key in jmk_names:
            return JMK, jmk_names[key]
        return KRAJ_IDS[kraj], ""

    population = parse_places(osm_places)

    def town_full(town: str, kraj: int) -> tuple[str, int]:
        """Plný název obce (CIS zkracuje: 'Ústí n.L.') a počet obyvatel z OSM."""
        known = population.get(kraj, {})
        hit = known.get(normalize(town))
        if hit is None and "." in town:    # jen zkratky typu 'n.L.'
            hit = known.get("~" + short_key(town))
        return hit if hit else (town, 0)

    def alt_for(name: str, town: str, full: str) -> str:
        """Hledací podoba jména s rozepsanou obcí ('usti nad labem hl n')."""
        norm, nt = normalize(name), normalize(town)
        if not town or full == town or not (norm == nt or norm.startswith(nt + " ")):
            return ""
        return normalize(full) + norm[len(nt):]

    places: list[dict] = []
    # ---- autobusové zastávky
    for (name, okres), s in bus.items():
        kraj = OKRESY[okres][1]
        ids, zones = zone_for(name, okres, kraj)
        full = town_full(s["obec"], kraj)[0]
        places.append({"name": name, "kind": "b", "obec": full, "okres": okres,
                       "town": s["obec"], "alt": alt_for(name, s["obec"], full),
                       "kraj": kraj, "ids": ids, "zones": _with_prague(zones, kraj),
                       "score": s["departures"]})

    # ---- vlakové stanice (vlak váží víc než autobus – jezdí dál a vozí víc lidí)
    for code, st in trains.items():
        osm_info = osm_codes.get(code) or osm_names.get(normalize(st["name"]))
        town = town_of(st["name"])
        okres = ""
        if town:
            okres = towns[town].most_common(1)[0][0]
            if osm_info is not None:
                same = [o for o in towns[town] if OKRESY[o][1] == osm_info["kraj"]]
                okres = max(same, key=lambda o: towns[town][o]) if same else ""
                if not okres:
                    town = None
        if osm_info is not None:
            kraj = osm_info["kraj"]
        elif okres:
            kraj = OKRESY[okres][1]
        else:
            continue  # nevíme, kde leží – raději vynechat
        if code in pid_codes:
            ids, zones = PID, pid_codes[code]
        elif kraj == 10 and osm_info is not None and osm_info["lat"] is not None:
            zones = _nearest_zone(jmk_points, osm_info["lat"], osm_info["lon"]) or ""
            ids = JMK
        else:
            ids, zones = KRAJ_IDS[kraj], ""
        full = town_full(town, kraj)[0] if town else ""
        # CZPTT zkracuje ('Brno-Kr. Pole'), OSM má plný název
        alts = [alt_for(st["name"], town or "", full)]
        if osm_info is not None and osm_info["name"]:
            alts.append(normalize(osm_info["name"]))
        alt = "|".join(dict.fromkeys(a for a in alts if a and a != normalize(st["name"])))
        places.append({"name": st["name"], "kind": "t", "obec": full, "okres": okres,
                       "town": town or "", "alt": alt,
                       "kraj": kraj, "ids": ids, "zones": _with_prague(zones, kraj),
                       "score": st["trains"] * 10})

    # ---- obce: obyvatelé + provoz všech jejich zastávek
    totals: Counter = Counter()
    zone_votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for p in places:
        if p["town"] and p["okres"]:
            totals[(p["town"], p["okres"])] += p["score"]
            for z in p["zones"].split(","):
                if z:
                    zone_votes[(p["town"], p["okres"])][(p["ids"], z)] += p["score"] + 1
    existing = {(p["name"], p["okres"]): p for p in places if p["kind"] == "b"}
    for (town, okres), total in totals.items():
        kraj = OKRESY[okres][1]
        full, pop = town_full(town, kraj)
        votes = zone_votes.get((town, okres))
        ids, zones = KRAJ_IDS[kraj], ""
        if votes:
            ids = votes.most_common(1)[0][0][0]
            weight = sum(n for (i, _), n in votes.items() if i == ids)
            if ids != KRAJ_IDS[kraj] and weight < (total + 1) / 2:
                # do obce jen zajíždí linka sousedního systému
                ids, votes = KRAJ_IDS[kraj], None
            # pásma, do kterých patří podstatná část provozu obce
        if votes:
            zones = _zones([",".join(z for (i, z), n in votes.items() if i == ids and n >= weight * 0.15)])
        if kraj == 0:
            zones = "P"
        entry = {"name": full, "kind": "c", "obec": full, "okres": okres, "town": town,
                 "alt": normalize(town) if full != town else "", "kraj": kraj, "ids": ids,
                 "zones": zones, "score": pop + total // 3}
        stop = existing.get((town, okres))
        if stop is not None:       # zastávka pojmenovaná jen obcí = obec
            stop.update(entry)
        else:
            places.append(entry)

    # Seřadit podle důležitosti; stejnojmenné obce rozliší okres a kraj.
    # CIS občas píše jednu obec dvěma způsoby – po rozepsání zkratek splynou.
    places.sort(key=lambda p: (-p["score"], p["name"]))
    seen: set[tuple[str, str, str]] = set()
    unique = []
    for p in places:
        key = (p["name"], p["kind"], p["okres"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    places = unique
    okresy = sorted({p["okres"] for p in places if p["okres"]})
    okres_idx = {o: i for i, o in enumerate(okresy)}
    return {
        "version": 1,
        "generated": dt.date.today().isoformat(),
        "sources": [
            "CIS JŘ – jízdní řády autobusů (JDF) a vlaků (CZPTT), portal.cisjr.cz",
            "Pražská integrovaná doprava – zastávky a pásma, data.pid.cz (CC BY 4.0)",
            "KORDIS JMK – GTFS IDS JMK, kordis-jmk.cz",
            "© přispěvatelé OpenStreetMap (ODbL) – kraj železničních stanic, názvy a počty obyvatel obcí",
        ],
        "kraje": KRAJE,
        "ids": [list(x) for x in IDS],
        "okresy": [[OKRESY[o][0], OKRESY[o][1]] for o in okresy],
        # [jméno, druh (c/t/b), obec, okres_idx (-1 = neznámý), kraj_idx, ids_idx, zóny,
        #  skóre, hledací aliasy (normalizované, oddělené "|", jinak "")]
        "places": [[p["name"], p["kind"], p["obec"] if p["obec"] != p["name"] else "",
                    okres_idx.get(p["okres"], -1), p["kraj"], p["ids"], p["zones"], p["score"],
                    p["alt"]]
                   for p in places],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache" / "stations",
                    help="adresář pro stažené zdroje")
    ap.add_argument("--refresh", action="store_true", help="stáhnout zdroje znovu")
    ap.add_argument("--year", type=int, default=dt.date.today().year,
                    help="rok jízdního řádu vlaků (výchozí: letošní)")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args(argv)

    data = build(args.cache, refresh=args.refresh, year=args.year)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(args.output, "wb", compresslevel=9, mtime=0) as fh:
        fh.write(raw)
    kinds = Counter(p[1] for p in data["places"])
    zoned = sum(1 for p in data["places"] if p[6])
    print(f"Hotovo: {len(data['places'])} míst (obce {kinds['c']}, vlak {kinds['t']}, "
          f"bus {kinds['b']}), se zónou {zoned}; {args.output} "
          f"({args.output.stat().st_size // 1024} kB)")
    for p in data["places"][:25]:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
