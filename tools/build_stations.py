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
* IDESKA (IDS JK) – oficiální mapa zón (kalkulacka.ideska.info/zones.xml),
  zóny jako mnohoúhelníky. Zastávka dostane zónu podle polohy: zastávka
  z OSM, jinak část obce, jinak střed obce (zóny skládají celé obce).

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
    ("IDESKA", "IDS JK – Integrovaný dopravní systém Jihočeského kraje"),
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
PID, IDSJK, IDPK, VDV, JMK = 0, 1, 2, 7, 8
PLZENSKY = 3      # index kraje


def idpk_zones(zones: set[str], okres: str) -> str:
    """Pásma IDPK z pásem linek v JDF.

    IDPK píše pásma třímístně s nulou na začátku (001 Plzeň, 052 Horažďovice…).
    Kódy 1xx se kryjí s IDESKA (100 České Budějovice), proto se berou jen
    u zastávek v Plzeňském kraji; vyšší kódy jsou zóny jiných systémů.
    """
    plzen = OKRESY.get(okres, ("", -1))[1] == PLZENSKY
    keep = sorted(z for z in zones if re.fullmatch(r"0\d\d", z)
                  or (plzen and re.fullmatch(r"1\d\d", z) and int(z) <= 191))
    return ",".join(keep)

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
                obec = re.sub(r"\s*\[[A-Z]{2}\]$", "", obec)      # "Tábor [TA]"
                if okres not in OKRESY or not obec:
                    continue
                name = bus_display_name(obec, cast, misto)
                key = (name, okres)
                stops.setdefault(key, {"obec": obec, "okres": okres, "departures": 0,
                                       "zones": set()})
                local[number] = key
            # Tarifní pásma integrovaných linek (IDPK, IDESKA, PID…) ze Zaslinky.txt:
            # linka, tarifní číslo, pásma ("052,053"), číslo zastávky, …
            zaslinky = _find(files, "zaslinky.txt")
            if zaslinky:
                for row in _jdf_rows(inner.read(zaslinky)):
                    if len(row) >= 4 and row[2] and row[3] in local:
                        stops[local[row[3]]]["zones"].update(
                            z.strip() for z in row[2].split(",") if z.strip())
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
RAIL_EDGES: dict[tuple[str, str], int] = {}     # dvojice stanic -> nejkratší jízda v minutách


def _minutes(loc, qualifier: str) -> int | None:
    for timing in loc.iter("Timing"):
        if timing.get("TimingQualifierCode") == qualifier:
            text = timing.findtext("Time") or ""
            m = re.match(r"(\d{2}):(\d{2})", text)
            if m:
                offset = int(timing.findtext("Offset") or 0)
                return int(m.group(1)) * 60 + int(m.group(2)) + offset * 1440
    return None


def parse_czptt(path: Path) -> dict[str, dict]:
    """kód stanice -> {name, trains}. Jen tuzemské stanice, kde vlak staví pro cestující.

    Vedlejší produkt: ``RAIL_EDGES`` – dvojice po sobě jdoucích zastávek
    vlaků, tedy síť, po které se hledá trasa pro lomení jízdenky na hranici
    integrovaných systémů (dopravci trasu vlaku nevracejí).
    """
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
            previous = None
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
                arrive = _minutes(loc, "ALA")
                leave = _minutes(loc, "ALD")
                if previous is not None and arrive is not None and previous[1] is not None:
                    edge = tuple(sorted((previous[0], code)))
                    took = max(1, arrive - previous[1])
                    RAIL_EDGES[edge] = min(RAIL_EDGES.get(edge, took), took)
                previous = (code, leave if leave is not None else arrive)
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
#  Zóny IDESKA (IDS JK)
# ---------------------------------------------------------------------------
IDESKA_ZONES = "https://kalkulacka.ideska.info/zones.xml"
IDESKA_TARIFF = "https://kalkulacka.ideska.info/jikord.json"
IDS_OUTPUT = ROOT / "vyhledavac" / "data" / "ids_ideska.json.gz"
RAIL_OUTPUT = ROOT / "vyhledavac" / "data" / "rail.json.gz"
#: Profily cestujících, jejichž jednotlivé jízdné se počítá (plné, 6–18, student, 65+).
IDESKA_PROFILES = ("1", "2", "3", "50")


VDV_CALC = "https://tim.abirun.eu/KrajVysocina/TarifniPocitadlo"
VDV_OUTPUT = ROOT / "vyhledavac" / "data" / "ids_vdv.json.gz"


def fetch_vdv(cache: Path, *, refresh: bool) -> Path:
    """Zóny a zastávky VDV z tarifního počítadla Kraje Vysočina.

    Počítadlo (tim.abirun.eu) má mapu: seznam zón a pro každou zónu její
    zastávky včetně nádraží. Ceny se za běhu ptají počítadla (``ids.vdv``),
    sem stačí, která zastávka leží ve které zóně – i přesahy do sousedních
    krajů (Počátky, …).
    """
    import concurrent.futures as cf

    target = cache / "vdv_stops.json"
    if target.exists() and target.stat().st_size and not refresh:
        return target
    client = httpx.Client(headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"},
                          timeout=60, follow_redirects=True)
    page = client.get(VDV_CALC + "/").text
    token = re.search(r"__APIRequestVerificationToken' value='([^']+)'", page).group(1)
    validity = re.search(r'<option value="(\d+)"', page).group(1)
    headers = {"__APIRequestVerificationToken": token}
    zones = client.post(VDV_CALC + "/Mapa/ZakladniDataMapy", headers=headers,
                        data={"platnostTarifuId": validity, "nadzonyId": ""}).json()["zones"]
    print(f"  VDV: {len(zones)} zón, stahuji zastávky…", flush=True)

    def stops(zone: dict) -> list:
        for _ in range(3):
            try:
                r = client.post(VDV_CALC + "/Mapa/Zastavky", headers=headers,
                                data={"platnostTarifuId": validity, "zonaId": zone["id"]})
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                continue
        return []

    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        per_zone = list(pool.map(stops, zones))
    data = {"zones": [{"id": z["id"], "text": z["text"]} for z in zones],
            "stops": [s for chunk in per_zone for s in chunk]}
    target.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return target


def parse_vdv(path: Path) -> tuple[dict[str, str], dict[str, str], dict]:
    """(nádraží -> zóna, zastávka -> zóna, data pro ids_vdv.json.gz).

    Zóna se zapisuje zkratkou („1“ = Jihlava); k ní ID pro dotaz na cenu.
    """
    data = json.loads(path.read_text("utf-8"))
    abbr: dict[int, str] = {}
    names: dict[str, str] = {}
    for zone in data["zones"]:
        m = re.match(r"^(\S+)\s+(.*)$", zone["text"])
        if m:
            abbr[zone["id"]] = m.group(1)
            names[m.group(1)] = m.group(2).strip()
    rail: dict[str, str] = {}
    bus: dict[str, str] = {}
    for stop in data["stops"]:
        zone = abbr.get(stop.get("zoneId"))
        if not zone:
            continue
        key = normalize(stop.get("text", ""))
        (bus if stop.get("isBus") else rail).setdefault(key, zone)
    out = {"source": VDV_CALC, "generated": dt.date.today().isoformat(),
           "zones": {a: {"id": -zid, "name": names.get(a, "")} for zid, a in abbr.items()}}
    return rail, bus, out


PID_TARIFF = "https://pid.cz/jizdne-a-tarif/"
PID_OUTPUT = ROOT / "vyhledavac" / "data" / "ids_pid.json.gz"


def extract_pid_tariff(path: Path) -> dict:
    """Ceník jednotlivého jízdného PID ze stránky pid.cz/jizdne-a-tarif.

    Tabulka „Jednotlivé jízdné v regionu“: počet pásem, platnost a ceny
    plnocenné / zvýhodněné / zvláštní, vždy v aplikaci PID Lítačka a na papír.
    Plus jízdenky jen pro Prahu (30 a 90 minut).
    """
    import html as html_mod

    raw = path.read_text("utf-8")
    raw = re.sub(r"<(script|style)\b.*?</\1>", "", raw, flags=re.S)
    text = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", raw)))
    text = re.sub(r"(\|\s*)+", "| ", text.replace("\xa0", " "))

    def kc(value: str) -> int | None:
        m = re.match(r"\s*(\d[\d ]*)\s*Kč", value)
        return int(m.group(1).replace(" ", "")) if m else None

    region = text.find("regionu | Plnocenné jízdné | Zvýhodněné jízdné")
    zones: dict[str, dict] = {}
    for m in re.finditer(r"\b(\d+) pás(?:ma|em) \| (\d+) min\. \|((?: [^|]+ \|){6})", text[region:region + 6000]):
        cells = [c.strip() for c in m.group(3).split("|") if c.strip()]
        full_app, full_paper, red_app, red_paper = (kc(c) for c in cells[:4])
        zones[m.group(1)] = {"minutes": int(m.group(2)), "full": full_app or full_paper,
                             "reduced": red_app or red_paper}
    praha: dict[str, dict] = {}
    for m in re.finditer(r"\b(30|90) min\.\* \|((?: [^|]+ \|){4})", text[:max(region, 0)]):
        cells = [c.strip() for c in m.group(2).split("|") if c.strip()]
        praha[m.group(1)] = {"minutes": int(m.group(1)), "full": kc(cells[0]), "reduced": kc(cells[2])}
    if len(zones) < 10 or len(praha) < 2:
        raise SystemExit(f"Ceník PID se nepodařilo přečíst (pásma {len(zones)}, Praha {len(praha)}) – "
                         "stránka pid.cz se změnila, upravte extract_pid_tariff().")
    return {"source": PID_TARIFF, "generated": dt.date.today().isoformat(),
            "zones": zones, "praha": praha}


def extract_ideska_tariff(path: Path) -> tuple[dict, dict[str, str]]:
    """Z dat kalkulačky IDESKA vybere, co je potřeba k ceně jednotlivé jízdenky.

    Vrací (tarif, normalizovaný název zastávky -> zóna). Seznam zastávek
    obsahuje i zastávky přesahů do sousedních krajů (Počátky, Benešov…).
    """
    data = json.loads(path.read_text("utf-8"))
    tariffs = [t for t in data["tariffs"]
               if t["type"] == "Single" and t["cp"] in IDESKA_PROFILES]
    tariff = {
        "source": IDESKA_TARIFF,
        "generated": dt.date.today().isoformat(),
        "tariffUnits": data["tariffUnits"],
        "tariffZones": data["tariffZones"],
        "tariffs": tariffs,
        "singleNetworkRelations": data["singleNetworkRelations"],
        "singleNetworkExceptions": data["singleNetworkExceptions"],
        "zoneCountries": data["zoneCountries"],
        "zoneAreas": data["zoneAreas"],
    }
    stops: dict[str, str] = {}
    for item in data["searchItems"]:
        m = re.match(r"^(.*?)\s*\((\d+)\)\s*$", item.get("text", ""))
        if not m or not item.get("zona"):
            continue
        parts = [x.strip() for x in m.group(1).split(",")]
        name = ", ".join(x for x in parts if x)
        stops.setdefault(normalize(name), item["zona"])
    return tariff, stops
OVERPASS_JCK = (
    '[out:json][timeout:170];area["ISO3166-2"="CZ-31"]->.a;'
    '(node(area.a)["place"]["name"];node(area.a)["highway"="bus_stop"]["name"];'
    'node(area.a)["public_transport"="platform"]["name"];);out body;'
)


def _parse_wkt(text: str) -> list[list[tuple[float, float]]]:
    """POLYGON/MULTIPOLYGON z WKT -> seznam prstenců [(lon, lat), …]."""
    rings = []
    for ring in re.findall(r"\(([-0-9., ]+)\)", text):
        pts = []
        for pair in ring.split(","):
            parts = pair.split()
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
        if len(pts) >= 3:
            rings.append(pts)
    return rings


def _in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class IdeskaZones:
    """Tarifní zóny IDESKA jako mnohoúhelníky z oficiální mapy zón.

    Zóny skládají celá katastrální území obcí, takže zastávka bez přesné
    polohy dostane správnou zónu i podle středu své obce.
    """

    def __init__(self, path: Path) -> None:
        root = ET.fromstring(path.read_bytes())
        real = {z.get("ID") for z in root.iter("Zone") if z.get("Area") != "Virtual"}
        self.zones: list[tuple[str, tuple[float, float, float, float], list]] = []
        for g in root.iter("ZoneGeometry"):
            zid = g.get("ZoneID")
            rings = _parse_wkt(g.get("Geometry") or "") if zid in real else []
            if not rings:
                continue
            xs = [x for r in rings for x, _ in r]
            ys = [y for r in rings for _, y in r]
            self.zones.append((zid, (min(xs), min(ys), max(xs), max(ys)), rings))

    def at(self, lat: float, lon: float) -> str:
        hits = []
        for zid, (x0, y0, x1, y1), rings in self.zones:
            # sudý počet zásahů = díra v mnohoúhelníku
            if x0 <= lon <= x1 and y0 <= lat <= y1 and sum(_in_ring(lon, lat, r) for r in rings) % 2:
                hits.append(zid)
        return _zones([",".join(hits)])


class JckLocator:
    """Poloha zastávky v Jihočeském kraji podle OSM: zastávka, část obce, obec."""

    def __init__(self, path: Path) -> None:
        self.stops: dict[str, list[tuple[float, float]]] = defaultdict(list)
        self.places: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
        for el in json.loads(path.read_text("utf-8"))["elements"]:
            tags, lat, lon = el.get("tags", {}), el.get("lat"), el.get("lon")
            if lat is None or not tags.get("name"):
                continue
            key = normalize(tags["name"])
            if "place" in tags:
                self.places[key].append((lat, lon, tags["place"]))
            else:
                self.stops[key].append((lat, lon))

    def _seat(self, okres: str) -> tuple[float, float] | None:
        name = OKRESY.get(okres, ("",))[0]
        towns = [p for p in self.places.get(normalize(name), []) if p[2] in ("city", "town")]
        return towns[0][:2] if towns else None

    @staticmethod
    def _nearest(cands, ref):
        if not cands:
            return None
        if ref is None or len(cands) == 1:
            return cands[0][:2]
        return min(cands, key=lambda c: (c[0] - ref[0]) ** 2 + (c[1] - ref[1]) ** 2)[:2]

    def town(self, obec: str, okres: str) -> tuple[float, float] | None:
        cands = [p for p in self.places.get(normalize(obec), [])
                 if p[2] in ("city", "town", "village", "hamlet")]
        return self._nearest(cands, self._seat(okres))

    def locate(self, name: str, obec: str, okres: str) -> tuple[float, float] | None:
        centre = self.town(obec, okres) if obec else None
        exact = self.stops.get(normalize(name))
        if exact:
            return self._nearest(exact, centre)
        parts = [x.strip() for x in name.split(",")]
        if len(parts) > 1 and centre:
            # "Obec, Část, místo" – část obce bývá v OSM jako samostatné místo
            near = self._nearest(self.places.get(normalize(parts[1]), []), centre)
            if near and abs(near[0] - centre[0]) + abs(near[1] - centre[1]) < 0.1:
                return near
        return centre


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
    ideska = fetch(cache, "ideska_zones.xml", IDESKA_ZONES, refresh=refresh)
    ideska_json = fetch(cache, "jikord.json", IDESKA_TARIFF, refresh=refresh)
    vdv_rail, vdv_bus, vdv_data = parse_vdv(fetch_vdv(cache, refresh=refresh))
    pid_page = fetch(cache, "pid_tarif.html", PID_TARIFF, refresh=refresh)
    pid_tariff = extract_pid_tariff(pid_page)
    osm_jck = fetch_overpass(cache, "osm_jck.json", OVERPASS_JCK, refresh=refresh)
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
    ideska_zones = IdeskaZones(ideska)
    jck = JckLocator(osm_jck)
    ideska_tariff, ideska_stops = extract_ideska_tariff(ideska_json)
    tariff_zones = set(ideska_tariff["zoneCountries"])

    def idsjk_zone(name: str, obec: str, okres: str, where=None) -> str:
        where = where or jck.locate(name, obec, okres)
        return ideska_zones.at(*where) if where else ""
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

    def zone_for(name: str, obec: str, okres: str, kraj: int) -> tuple[int, str]:
        key = normalize(name)
        if kraj == 2:      # vlastní systém kraje má přednost před zajíždějícím PID
            zones = idsjk_zone(name, obec, okres)
            if zones:
                return IDSJK, zones
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
        full, pop = town_full(s["obec"], kraj)
        if not pop:
            # Jízdní řády MHD píšou do pole obce název zastávky („Tyršova“,
            # „aut.nádr.“) – bez skutečné obce by taková položka v nabídce mátla.
            continue
        # CIS zkracuje obec ("Týn n.Vlt., aut.nádr.") – polohu hledat podle plného jména
        spelled = full + name[len(s["obec"]):] if name.startswith(s["obec"]) else name
        ids, zones = zone_for(spelled, full, okres, kraj)
        systems = {ids: zones} if zones else {}
        idpk = idpk_zones(s.get("zones", set()), okres)
        if idpk:
            systems[IDPK] = idpk
            if ids == IDPK and not zones:
                zones = idpk
        vdv_zone = vdv_bus.get(normalize(spelled)) or vdv_bus.get(normalize(name))
        if vdv_zone:
            systems[VDV] = vdv_zone
            if ids == VDV and not zones:
                zones = vdv_zone
        ideska_zone = ideska_stops.get(normalize(spelled)) or ideska_stops.get(normalize(name))
        if ideska_zone:
            systems[IDSJK] = ideska_zone
        places.append({"name": name, "kind": "b", "obec": full, "okres": okres, "systems": systems,
                       "town": s["obec"], "alt": alt_for(name, s["obec"], full),
                       "kraj": kraj, "ids": ids, "zones": _with_prague(zones, kraj),
                       "score": s["departures"]})

    idpk_by_town: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for p in places:
        if IDPK in p["systems"]:
            idpk_by_town[(p["town"], p["okres"])].append((p["name"], p["systems"][IDPK]))

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
        jck_zone = ""
        if kraj == 2:
            where = (osm_info["lat"], osm_info["lon"]) if osm_info and osm_info["lat"] else None
            jck_zone = idsjk_zone(st["name"], town or "", okres, where)
        if jck_zone:
            ids, zones = IDSJK, jck_zone
        elif code in pid_codes:
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
        systems = {ids: zones} if zones else {}
        if osm_info is not None and osm_info["lat"] is not None:
            # přesahy IDESKA do sousedních krajů (Počátky-Žirovnice, Benešov…)
            jz = ",".join(z for z in ideska_zones.at(osm_info["lat"], osm_info["lon"]).split(",")
                          if z in tariff_zones)
            if jz:
                systems[IDSJK] = jz
            near_jmk = _nearest_zone(jmk_points, osm_info["lat"], osm_info["lon"], 400)
            if near_jmk:
                systems.setdefault(JMK, near_jmk)
        if code in pid_codes:
            systems.setdefault(PID, pid_codes[code])
        vdv_zone = vdv_rail.get(normalize(st["name"])) or (
            vdv_rail.get(normalize(osm_info["name"])) if osm_info and osm_info["name"] else None)
        if vdv_zone:
            systems[VDV] = vdv_zone
        if town and okres:
            # Pásmo IDPK nádraží = pásmo autobusové zastávky u nádraží (JDF), jinak obce.
            candidates = idpk_by_town.get((town, okres), [])
            rail_like = [z for n, z in candidates if re.search(r"žel|nádr", n)]
            pick = Counter(rail_like or [z for _, z in candidates]).most_common(1)
            if pick:
                systems[IDPK] = pick[0][0]
        if not zones and ids in systems:
            zones = systems[ids]           # hlavní systém kraje (IDPK) bez vlastní zóny
        places.append({"name": st["name"], "kind": "t", "obec": full, "okres": okres,
                       "systems": systems, "code": code,
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
    by_town: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in places:
        if p["town"] and p["okres"]:
            by_town[(p["town"], p["okres"])].append(p)
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
        elif kraj == 2 and not zones:
            zones = idsjk_zone(full, full, okres)
            ids = IDSJK if zones else ids
        members: dict[int, Counter] = defaultdict(Counter)
        for q in by_town.get((town, okres), ()):
            for sys_i, zs in q["systems"].items():
                members[sys_i][zs] += q["score"] + 1
        systems = {i: c.most_common(1)[0][0] for i, c in members.items()}
        if zones:
            systems[ids] = zones
        entry = {"name": full, "kind": "c", "obec": full, "okres": okres, "town": town,
                 "systems": systems,
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
            "JIKORD – mapa zón IDESKA (IDS JK), kalkulacka.ideska.info",
            "© přispěvatelé OpenStreetMap (ODbL) – kraj železničních stanic, názvy a počty obyvatel obcí",
        ],
        "kraje": KRAJE,
        "ids": [list(x) for x in IDS],
        "okresy": [[OKRESY[o][0], OKRESY[o][1]] for o in okresy],
        # [jméno, druh (c/t/b), obec, okres_idx (-1 = neznámý), kraj_idx, ids_idx, zóny,
        #  skóre, hledací aliasy (normalizované, oddělené "|", jinak "")]
        # + systémy, do kterých místo patří: "ids_idx:zóny;…" (přesahy IDS)
        "places": [[p["name"], p["kind"], p["obec"] if p["obec"] != p["name"] else "",
                    okres_idx.get(p["okres"], -1), p["kraj"], p["ids"], p["zones"], p["score"],
                    p["alt"], ";".join(f"{i}:{z}" for i, z in sorted(p.get("systems", {}).items()))]
                   for p in places],
        "_ideska": ideska_tariff,
        "_pid": pid_tariff,
        "_vdv": vdv_data,
        "_rail": rail_graph(places),
    }


def rail_graph(places: list[dict]) -> dict:
    """Železniční síť: stanice (jména jako v databázi míst), sousedé a minuty jízdy."""
    names: dict[str, str] = {p["code"]: p["name"] for p in places if p.get("code")}
    order = sorted(names)
    index = {code: i for i, code in enumerate(order)}
    edges = [[index[a], index[b], minutes] for (a, b), minutes in sorted(RAIL_EDGES.items())
             if a in index and b in index]
    # kódy SR70 – ČD je používá v adrese detailu vlaku (s předponou 54)
    return {"stations": [names[c] for c in order], "codes": order, "edges": edges}


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
    for target, payload in ((IDS_OUTPUT, data.pop("_ideska")), (PID_OUTPUT, data.pop("_pid")),
                            (VDV_OUTPUT, data.pop("_vdv")),
                            (RAIL_OUTPUT, data.pop("_rail"))):
        with gzip.GzipFile(target, "wb", compresslevel=9, mtime=0) as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        print(f"  {target.name}: {target.stat().st_size // 1024} kB")
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
