"""Skutečný čas přestupu mezi dvěma stanicemi (pěšky nebo MHD).

Kombinace jízdenek od různých prodejců často znamená přestup mezi různými
nádražími ve městě (FlixBus přijede na Berlin Südkreuz, vlak ÖBB jede
z Berlin Hbf). Pevná rezerva tu nestačí – Südkreuz → Hbf je S-Bahnem
kolem 15 minut a s čekáním víc. Proto:

1. stejná stanice (stejné jméno, jen jiný zápis) → ``SAME`` (4 min);
2. Praha a Brno → ``citytransfer`` (metro/tramvaj podle jízdních dob);
3. jinak se zeptáme Transitous na cestu z A do B v daný čas (pěšky i MHD,
   reálné odjezdy) a k příjezdu přičteme 3 min rezervy;
4. když to nejde zjistit, počítá se opatrně s ``UNKNOWN`` (40 min) – a ve
   výpisu úvah je to vidět.

Výsledky se pamatují (stejná dvojice stanic ve stejnou hodinu).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from . import stations, transitous
from .normalize import normalize, tokens
from .trace import trace

log = logging.getLogger(__name__)

SAME = dt.timedelta(minutes=4)
UNKNOWN = dt.timedelta(minutes=40)
RESERVE = dt.timedelta(minutes=3)
NEAR_KM = 25.0

_memo: dict[tuple[str, str, int], dt.timedelta] = {}
_client = None


def same_station(a: str, b: str) -> bool:
    """Tatáž stanice, jen jinak zapsaná („Wien Hbf (Bahnsteige 3-12)“ = „Wien Hauptbahnhof“)?
    Kromě jména města musí sedět i to, co stanici odlišuje (Südkreuz ≠ Hauptbahnhof)."""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    city_words: set[str] = set()
    for p in (stations.place(a), stations.place(b)):
        if p is None:
            continue
        # jen jméno města (u české stanice obec – „Praha-Holešovice“ ≠ „Praha hl.n.“)
        names = [p.obec] if p.kind != "c" else [p.name, p.local, *p.keys]
        city_words |= {w for n in names if n for w in normalize(n).split()}
    ta = set(normalize(a).split()) - city_words - _MAIN
    tb = set(normalize(b).split()) - city_words - _MAIN
    ta, tb = {w for w in ta if not w.isdigit()}, {w for w in tb if not w.isdigit()}
    return ta == tb                      # zbylo totéž (nic = hlavní nádraží)


#: Slova, která stanici neodlišují: hlavní nádraží v různých jazycích, „S+U“, „/Donau“.
_MAIN = {"hbf", "hauptbahnhof", "hl", "n", "st", "hlavni", "nadrazi", "stanica", "central", "centraal",
         "centrale", "centralstation", "c", "h", "s", "u", "main", "bahnhof", "bf", "gl", "glowny",
         "bahnsteige", "bahnsteig", "zugang", "station", "gare", "stazione", "estacion", "donau", "am",
         "an", "der", "im", "ob", "pu", "palyaudvar", "hlavna", "dworzec", "glavni", "kolodvor",
         "centralny", "termini", "centrum", "de", "la", "l", "le", "du", "des", "del", "di"}


async def transfer_time(a: str, b: str, when: dt.datetime) -> dt.timedelta:
    """Kolik času potřebuji z příjezdu do stanice ``a`` na odjezd ze stanice ``b``."""
    if not a or not b or same_station(a, b):
        return SAME
    key = (normalize(a), normalize(b), when.hour)
    if key in _memo:
        return _memo[key]
    from . import citytransfer

    known = citytransfer.transfer(a, b)
    if known is not None:
        result = dt.timedelta(minutes=known.minutes) + RESERVE
        trace(f"⇄ přestup {a} → {b}: {known.how} {result.seconds // 60} min")
    else:
        result = await _transitous(a, b, when)
    _memo[key] = result
    return result


async def _transitous(a: str, b: str, when: dt.datetime) -> dt.timedelta:
    global _client
    try:
        from .http import HttpClient

        if _client is None:
            _client = HttpClient("Přestupy", timeout=20)
        headers = {"User-Agent": transitous.USER_AGENT}
        ends = []
        for name in (a, b):
            found = await _client.get_json(f"{transitous.API}/v1/geocode", params={"text": name, "language": "en"},
                                           headers=headers)
            ends.append(_pick(name, found or []))
        if None in ends:
            raise LookupError("stanici nenašel")
        data = await _client.get_json(f"{transitous.API}/v5/plan", headers=headers, params={
            "fromPlace": ends[0], "toPlace": ends[1], "numItineraries": 2,
            "time": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) or {}
        arrivals = [transitous._time(it.get("endTime")) for it in data.get("itineraries") or []]
        arrivals = [t for t in arrivals if t is not None]
        if not arrivals:
            raise LookupError("cestu nenašel")
        result = min(arrivals) - when + RESERVE
        trace(f"⇄ přestup {a} → {b}: podle Transitous {result.seconds // 60} min (vč. {RESERVE.seconds // 60} min rezervy)")
        return max(result, SAME)
    except Exception as exc:  # noqa: BLE001 - přestup neznám = opatrná rezerva
        log.debug("přestup %s → %s: %s", a, b, exc)
        trace(f"⇄ přestup {a} → {b}: nezjištěn ({exc}) – počítám {UNKNOWN.seconds // 60} min")
        return UNKNOWN


_located: dict[str, tuple[float, float] | None] = {}


async def locate(name: str) -> tuple[float, float] | None:
    """Souřadnice místa: z databáze měst, jinak z geokodéru Transitous
    (česká místa v databázi souřadnice nemají)."""
    place = stations.place(name)
    if place is not None and place.lat is not None:
        return place.lat, place.lon
    key = normalize(name)
    if key in _located:
        return _located[key]
    global _client
    found = None
    try:
        from .http import HttpClient

        if _client is None:
            _client = HttpClient("Přestupy", timeout=20)
        results = await _client.get_json(f"{transitous.API}/v1/geocode", params={"text": name, "language": "en"},
                                         headers={"User-Agent": transitous.USER_AGENT})
        picked = _pick(name, results or [])
        if picked:
            lat, lon = picked.split(",")
            found = (float(lat), float(lon))
    except Exception as exc:  # noqa: BLE001 - poloha neznámá
        log.debug("poloha %s: %s", name, exc)
    _located[key] = found
    return found


def _pick(name: str, results: list[dict[str, Any]]) -> str | None:
    """Zastávka nejpodobnější jménu, v okolí města (podle databáze měst)."""
    city = stations.place(name)
    wanted = tokens(name)
    best, best_key = None, None
    for r in results:
        if r.get("type") != "STOP" or not r.get("id") or r.get("lat") is None:
            continue
        if city is not None and city.lat is not None:
            from .providers.planner import _km

            if _km(city.lat, city.lon, float(r["lat"]), float(r["lon"])) > NEAR_KM:
                continue
        common = len(wanted & tokens(str(r.get("name") or "")))
        key = (-common, len(str(r.get("name") or "")))
        if best_key is None or key < best_key:
            best, best_key = f"{r['lat']},{r['lon']}", key
    return best
