"""Železniční síť ČR pro hledání trasy vlaku.

Dopravci (ČD) vracejí jen odkud–kam a čas, ne přes které stanice vlak
jede. Pro lomení jízdenky na hranici integrovaného systému potřebujeme
vědět, kudy trasa vede – proto síť sestavená z celostátního jízdního
řádu (CIS JŘ, ``tools/build_stations.py``): stanice a dvojice po sobě
jdoucích zastávek vlaků.
"""

from __future__ import annotations

import gzip
import heapq
import json
import logging
from functools import lru_cache
from pathlib import Path

from . import stations
from .normalize import normalize

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data" / "rail.json.gz"


@lru_cache(maxsize=1)
def _graph() -> tuple[list[str], dict[int, list[tuple[int, int]]], dict[str, int], list[str]] | None:
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Železniční síť se nepodařilo načíst: %s", exc)
        return None
    names: list[str] = data["stations"]
    adjacent: dict[int, list[tuple[int, int]]] = {i: [] for i in range(len(names))}
    for a, b, minutes in data["edges"]:
        adjacent[a].append((b, minutes))
        adjacent[b].append((a, minutes))
    index = {normalize(n): i for i, n in enumerate(names)}
    return names, adjacent, index, data.get("codes", [])


def station_for(name: str) -> int | None:
    """Uzel sítě pro jméno zadané uživatelem nebo dopravcem.

    Přímo stanice („Praha hl.n.“), jinak nejdůležitější stanice obce
    („Třeboň“ -> Třeboň, „Havlíčkův Brod“ -> Havlíčkův Brod).
    """
    graph = _graph()
    if graph is None or not name:
        return None
    names, _, index, _ = graph
    hit = index.get(normalize(name))
    if hit is not None:
        return hit
    place = stations.place(name)
    if place is None:
        return None
    if place.kind == "t" and normalize(place.name) in index:
        return index[normalize(place.name)]
    town = place.obec or place.name
    db = stations.database()
    best = None
    for candidate in db.places if db else ():       # seřazeno podle důležitosti
        if candidate.kind == "t" and (candidate.obec or candidate.name) == town \
                and normalize(candidate.name) in index:
            best = index[normalize(candidate.name)]
            break
    return best


def route(origin: str, destination: str) -> list[str]:
    """Nejrychlejší trasa mezi dvěma místy – seznam stanic.

    Váha úseku je nejkratší jízdní doba vlaku z jízdního řádu, takže trasa
    odpovídá tomu, kudy vlaky opravdu jezdí (ne skokům rychlíků přes půl
    republiky).
    """
    graph = _graph()
    start, goal = station_for(origin), station_for(destination)
    if graph is None or start is None or goal is None:
        return []
    names, adjacent, _, _ = graph
    dist = {start: 0}
    prev: dict[int, int] = {}
    queue = [(0, start)]
    while queue:
        d, node = heapq.heappop(queue)
        if node == goal:
            break
        if d > dist.get(node, 1 << 30):
            continue
        for nxt, minutes in adjacent[node]:
            if d + minutes < dist.get(nxt, 1 << 30):
                dist[nxt] = d + minutes
                prev[nxt] = node
                heapq.heappush(queue, (d + minutes, nxt))
    if goal not in dist:
        return []
    path = [goal]
    while path[-1] != start:
        path.append(prev[path[-1]])
    return [names[i] for i in reversed(path)]


def station_code(name: str) -> str | None:
    """Kód stanice, jak ho používá cd.cz (54 + SR70), nebo ``None``."""
    graph = _graph()
    node = station_for(name)
    if graph is None or node is None or not graph[3]:
        return None
    return "54" + graph[3][node]


def station_name(name: str) -> str | None:
    """Jméno železniční stanice pro místo („Třeboň“ -> „Třeboň“, „Brno“ -> „Brno hl. n.“)."""
    graph = _graph()
    node = station_for(name)
    return graph[0][node] if graph is not None and node is not None else None


def name_for_code(code: str | None) -> str | None:
    """Jméno stanice v databázi míst pro kód z cd.cz (54xxxxx)."""
    graph = _graph()
    if graph is None or not code:
        return None
    short = code[2:] if len(code) == 7 and code.startswith("54") else code
    try:
        return graph[0][graph[3].index(short)]
    except ValueError:
        return None
