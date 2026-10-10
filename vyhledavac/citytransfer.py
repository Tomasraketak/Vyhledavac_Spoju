"""Přejezdy mezi nádražími a autobusovými terminály v Praze a Brně.

Když cesta přestupuje v Praze (vlak ČD na hlavní nádraží, dál autobus
RegioJet z Černého Mostu), je potřeba vědět, jak dlouho trvá přejezd a kolik
stojí. Časy jsou odhad „ode dveří ke dveřím“ v běžném provozu:

* **Praha** – metro podle jízdní doby mezi stanicemi (linky B a C, přestup
  na Florenci), plus docházka z nástupiště/stanoviště k metru a průměrné
  čekání na vlak metra (3 min). Libeň a Zahradní Město nemají metro:
  tramvají k Palmovce (B), resp. vlakem/tramvají na hlavní nádraží.
  Jízdenka PID pro Prahu (30 nebo 90 min, děti 6–15 a senioři 65+ zdarma).
* **Brno** – bez metra; časy pěšky nebo tramvají podle zkušenosti. Pěší
  přesuny (Grand – hlavní nádraží, hlavní – Dolní nádraží) jsou zdarma,
  tramvaj za jízdenku IDS JMK pro Brno (zóny 100+101).

Nejsou to živá data – slouží k ověření, že přestup stihnete, a k započtení
ceny MHD. Rezerva na přestup se přičítá zvlášť (``BUFFER``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .normalize import normalize

#: Rezerva navíc k přejezdu (zpoždění, hledání nástupiště).
BUFFER = 5
METRO_WAIT = 3
LINE_CHANGE = 4

# Jízdní doba metra od konečné (min), podle jízdního řádu PID.
METRO = {
    "B": {"Zličín": 0, "Stodůlky": 2, "Nové Butovice": 8, "Smíchovské nádraží": 14, "Anděl": 16,
          "Můstek": 21, "Náměstí Republiky": 22, "Florenc": 24, "Palmovka": 28,
          "Českomoravská": 30, "Černý Most": 39},
    "C": {"Letňany": 0, "Ládví": 6, "Kobylisy": 8, "Nádraží Holešovice": 11, "Vltavská": 13,
          "Florenc": 15, "Hlavní nádraží": 16, "Muzeum": 18, "Budějovická": 25, "Roztyly": 29,
          "Háje": 35},
}


@dataclass(frozen=True)
class Point:
    """Místo přestupu: jak se k němu dostat z metra (linka, stanice, docházka)."""
    key: str
    label: str
    words: tuple[str, ...]          # klíčová slova v normalizovaném jménu
    line: str = ""
    station: str = ""
    access: int = 0                 # docházka/dojezd k metru (min)


PRAHA = [
    Point("hln", "Praha hl. n.", ("hl n", "hlavni nadrazi", "hln"), "C", "Hlavní nádraží", 5),
    Point("florenc", "Florenc (ÚAN)", ("florenc",), "B", "Florenc", 4),
    Point("zlicin", "Zličín", ("zlicin",), "B", "Zličín", 3),
    Point("cernymost", "Černý Most", ("cerny most",), "B", "Černý Most", 3),
    Point("ladvi", "Ládví", ("ladvi",), "C", "Ládví", 3),
    Point("kobylisy", "Kobylisy", ("kobylisy",), "C", "Kobylisy", 3),
    Point("roztyly", "Roztyly", ("roztyly",), "C", "Roztyly", 3),
    Point("holesovice", "Nádraží Holešovice", ("holesovice",), "C", "Nádraží Holešovice", 4),
    Point("smichov", "Praha-Smíchov", ("smichov", "na knizeci", "andel"), "B", "Smíchovské nádraží", 4),
    Point("masarykovo", "Masarykovo nádraží", ("masarykovo",), "B", "Náměstí Republiky", 6),
    # bez metra: tramvají k Palmovce / vlakem na hlavní nádraží
    Point("liben", "Praha-Libeň", ("liben",), "B", "Palmovka", 12),
    Point("zahradni", "Praha-Zahradní Město", ("zahradni mesto",), "C", "Hlavní nádraží", 15),
]

#: Brno: (A, B) -> (minuty, pěšky?)
BRNO_POINTS = {
    "hln": ("Brno hl. n.", ("hl n", "hlavni nadrazi", "hln")),
    "grand": ("Brno, Grand (Benešova)", ("grand", "benesova")),
    "zvonarka": ("Brno, ÚAN Zvonařka", ("zvonarka",)),
    "dolni": ("Brno, Dolní nádraží", ("dolni nadrazi",)),
    "zidenice": ("Brno-Židenice", ("zidenice",)),
    "krpole": ("Brno-Královo Pole", ("kralovo pole",)),
}
BRNO = {
    ("grand", "hln"): (4, True), ("hln", "dolni"): (8, True), ("grand", "dolni"): (10, True),
    ("hln", "zvonarka"): (12, False), ("grand", "zvonarka"): (14, False),
    ("dolni", "zvonarka"): (12, False), ("zidenice", "hln"): (15, False),
    ("zidenice", "grand"): (17, False), ("zidenice", "zvonarka"): (20, False),
    ("zidenice", "dolni"): (20, False), ("krpole", "hln"): (18, False),
    ("krpole", "grand"): (18, False), ("krpole", "zvonarka"): (25, False),
    ("krpole", "dolni"): (24, False), ("krpole", "zidenice"): (25, False),
}
#: Pražské pěší přesuny, kde se metro nevyplatí.
PRAHA_WALK = {("hln", "florenc"): 12, ("florenc", "masarykovo"): 8, ("hln", "masarykovo"): 10}

HUBS = {"praha": "Praha", "brno": "Brno"}


@dataclass(frozen=True)
class Transfer:
    city: str
    origin: str                 # popisek místa
    destination: str
    minutes: int                # bez rezervy
    walk: bool                  # jen pěšky (zdarma)
    how: str                    # „metro B → C“, „pěšky“, „tramvaj“

    @property
    def needed(self) -> int:
        """Minuty, které musí přestup mít (přejezd + rezerva)."""
        return self.minutes + BUFFER


def hub_of(name: str) -> str | None:
    """„Praha hl.n.“ -> "praha", „Brno, ÚAN Zvonařka“ -> "brno"."""
    n = normalize(name)
    for key in HUBS:
        if n == key or n.startswith(key + " "):
            return key
    return None


def _praha_point(name: str) -> Point | None:
    n = " " + normalize(name) + " "
    for point in PRAHA:
        if any(f" {w} " in n for w in point.words):
            return point
    return None


def _brno_point(name: str) -> str | None:
    n = " " + normalize(name) + " "
    for key, (_, words) in BRNO_POINTS.items():
        if any(f" {w} " in n for w in words):
            return key
    return None


def _metro_minutes(a: Point, b: Point) -> tuple[int, str]:
    if a.line == b.line:
        ride = abs(METRO[a.line][a.station] - METRO[b.line][b.station])
        return ride + METRO_WAIT, f"metro {a.line}"
    ride = (abs(METRO[a.line][a.station] - METRO[a.line]["Florenc"])
            + abs(METRO[b.line]["Florenc"] - METRO[b.line][b.station]))
    return ride + 2 * METRO_WAIT + LINE_CHANGE, f"metro {a.line} → {b.line} (Florenc)"


def transfer(arrive_at: str, depart_from: str) -> Transfer | None:
    """Přejezd mezi dvěma místy v jednom městě; ``None`` = neznámá místa / jiná města."""
    city = hub_of(arrive_at)
    if city is None or city != hub_of(depart_from):
        return None
    if city == "praha":
        a, b = _praha_point(arrive_at), _praha_point(depart_from)
        if a is None or b is None:
            return None
        if a.key == b.key:
            return Transfer(city, a.label, b.label, 0, True, "stejné místo")
        walk = PRAHA_WALK.get((a.key, b.key)) or PRAHA_WALK.get((b.key, a.key))
        if walk:
            return Transfer(city, a.label, b.label, walk, True, "pěšky")
        ride, how = _metro_minutes(a, b)
        return Transfer(city, a.label, b.label, a.access + ride + b.access, False, how)
    a_key, b_key = _brno_point(arrive_at), _brno_point(depart_from)
    if a_key is None or b_key is None:
        return None
    a_label, b_label = BRNO_POINTS[a_key][0], BRNO_POINTS[b_key][0]
    if a_key == b_key:
        return Transfer(city, a_label, b_label, 0, True, "stejné místo")
    minutes, walk = BRNO.get((a_key, b_key)) or BRNO.get((b_key, a_key)) or (None, False)
    if minutes is None:
        return None
    return Transfer(city, a_label, b_label, minutes, walk, "pěšky" if walk else "tramvaj / pěšky")


def city_fare(t: Transfer, passengers: Sequence) -> Decimal | None:
    """Cena MHD pro celou skupinu (0 při pěším přesunu); ``None`` = neznámá."""
    if t.walk:
        return Decimal(0)
    from .ids import group_fare, jmk, pid

    minutes = t.minutes + BUFFER
    if t.city == "praha":
        fare = group_fare(passengers, lambda p: pid.best_fare("P", "P", p, minutes))
    else:
        fare = group_fare(passengers, lambda p: jmk.best_between(("100",), ("101",), p, minutes))
    return fare.price if fare is not None else None
