"""Porovnávání názvů měst a stanic.

Stanice se v každém systému jmenuje jinak: "Praha hl.n.", "Praha hl. n.",
"Prague Main Station", "Praha hlavní nádraží". Tohle je jediné místo, kde
se řeší diakritika, zkratky a aliasy – dopravci i hacky to sdílejí.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Slova, která na shodu stanice nemají vliv.
_NOISE = {
    "hl", "n", "hlavni", "nadrazi", "nadr", "st", "stanice", "zst", "zastavka",
    "bahnhof", "bf", "hbf", "station", "main", "central", "centrum", "aut",
    "autobusove", "autobusova", "bus", "terminal", "aс",
}

# Ekvivalence napříč jazyky. Klíč i hodnoty se normalizují stejnou funkcí.
_ALIASES: dict[str, set[str]] = {
    "praha": {"prague", "prag", "praga"},
    "brno": {"bruenn", "brunn"},
    "vieden": {"wien", "vienna", "viden", "becs"},
    "bratislava": {"pressburg", "pozsony"},
    "mnichov": {"munchen", "munich", "muenchen"},
    "drazdany": {"dresden"},
    "berlin": {"berlin"},
    "varsava": {"warszawa", "warsaw", "warschau"},
    "krakov": {"krakow", "cracow", "krakau"},
    "budapest": {"budapest", "budapest"},
    "kosice": {"kaschau", "kassa"},
    "linec": {"linz"},
    "salcburk": {"salzburg"},
    "pasov": {"passau"},
    "norimberk": {"nurnberg", "nuremberg", "nuernberg"},
    "zilina": {"sillein"},
}


@lru_cache(maxsize=8192)
def strip_accents(text: str) -> str:
    """Odstraní diakritiku. 'Vídeň' -> 'Viden'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


@lru_cache(maxsize=8192)
def normalize(text: str) -> str:
    """Kanonická podoba názvu: bez diakritiky, malými písmeny, bez interpunkce."""
    if not text:
        return ""
    cleaned = strip_accents(text).lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


@lru_cache(maxsize=8192)
def tokens(text: str) -> frozenset[str]:
    """Významová slova názvu – bez výplně typu 'hl.n.'."""
    words = [w for w in normalize(text).split() if w and w not in _NOISE]
    return frozenset(words or normalize(text).split())


@lru_cache(maxsize=4096)
def _alias_group(word: str) -> frozenset[str]:
    """Všechny podoby daného slova (např. wien/viden/vienna)."""
    for canonical, variants in _ALIASES.items():
        group = {normalize(canonical), *(normalize(v) for v in variants)}
        if word in group:
            return frozenset(group)
    return frozenset({word})


def _expand(words: frozenset[str]) -> frozenset[str]:
    out: set[str] = set()
    for word in words:
        out |= _alias_group(word)
    return frozenset(out)


def aliases(name: str) -> list[str]:
    """Jiné podoby názvu, které stojí za to zkusit v cizím našeptávači.

    'Vídeň' → ['Vieden', 'Wien', 'Vienna', 'Becs']. Pořadí sleduje
    ``_ALIASES`` (nejdřív kanonický tvar, pak varianty).
    """
    found: list[str] = []
    for word in tokens(name):
        for canonical, variants in _ALIASES.items():
            group = [canonical, *sorted(variants)]
            if word in {normalize(g) for g in group}:
                found += [g for g in group if normalize(g) != word]
    return [w.capitalize() for w in dict.fromkeys(found)]


def same_place(a: str, b: str) -> bool:
    """Jde o totéž město/stanici? Symetrické, tolerantní k aliasům."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = _expand(tokens(a)), _expand(tokens(b))
    if not ta or not tb:
        return False
    return bool(ta & tb) and (ta <= tb or tb <= ta or len(ta & tb) >= 2)


def matches_station(needle: str, haystack: str) -> bool:
    """Je ``needle`` (hledaná stanice) obsažena v názvu ``haystack``?

    Nesymetrické a záměrně velkorysé směrem dolů: "Břeclav" sedí na
    "Břeclav hl.n." i "Praha" na "Praha-Ruzyně" – to je pro hledání
    uzlů na trase správně. Porovnávají se ale celá slova, ne podřetězce,
    takže "Praha" nesedne na "Prachatice".
    """
    nt, ht = _expand(tokens(needle)), _expand(tokens(haystack))
    if not nt or not ht:
        return False
    return nt <= ht


def score_match(query: str, candidate: str) -> float:
    """Skóre 0..1 pro výběr nejlepší stanice z našeptávače."""
    nq, nc = normalize(query), normalize(candidate)
    if not nq or not nc:
        return 0.0
    if nq == nc:
        return 1.0
    tq, tc = _expand(tokens(query)), _expand(tokens(candidate))
    if not tq or not tc:
        return 0.0
    overlap = len(tq & tc)
    if not overlap:
        return 0.0
    score = overlap / len(tq | tc)
    if tq <= tc:
        score += 0.35            # dotaz je plně obsažen – silný signál
    if nc.startswith(nq):
        score += 0.15
    return min(score, 0.99)


def best_match(query: str, candidates: list[str], threshold: float = 0.3) -> int | None:
    """Index nejlepšího kandidáta, nebo ``None`` když nic nesedí."""
    best_idx, best_score = None, threshold
    for idx, cand in enumerate(candidates):
        score = score_match(query, cand)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx
