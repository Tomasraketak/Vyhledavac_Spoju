"""Zeměpis pro mezinárodní travel hacky.

* ``country_of`` – v jaké zemi leží stanice (podle klíčových slov u
  zahraničí, jinak databáze zastávek ČR);
* ``BORDERS`` – hraniční přechody mezi dvěma zahraničními státy: poslední
  stanice v tranzitní zemi a první v cílové (Marchegg / Devínska Nová Ves);
* ``AT_CORRIDOR`` – trať Salzburg – Linz – Wien, kde se vyplatí jízdenka ČD až
  do Horního Dvořiště nebo Břeclavi;
* ``PASSES`` – regionální síťové jízdenky (Einfach-Raus-Ticket, Bayern-Ticket),
  vstupní stanice z Česka a kategorie vlaků, ve kterých platí.

Ceny síťových jízdenek (říjen 2026, ověřte na oebb.at / bahn.de):
Einfach-Raus-Ticket 46 € pro 2–5 osob (jen regionální vlaky ÖBB: S, R, REX,
CJX; ve všední dny od 9:00), Bayern-Ticket 34 € + 10 € za každou další osobu
(max. 5; regionální vlaky RB, RE, S, ALX, ag …; ve všední dny od 9:00).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

from .normalize import normalize

# Klíčová slova (normalizovaná) -> země. Pořadí nevadí, hledá se celé slovo.
_COUNTRY_WORDS: dict[str, tuple[str, ...]] = {
    "AT": ("wien", "vieden", "viden", "linz", "linec", "salzburg", "salcburk", "graz",
           "innsbruck", "st polten", "sankt polten", "amstetten", "wels", "attnang",
           "vocklabruck", "voecklabruck", "marchegg", "kittsee", "gmund", "gmuend", "summerau",
           "freistadt", "hohenau", "bernhardsthal", "retz", "hollabrunn", "stockerau",
           "nickelsdorf", "spielfeld", "villach", "klagenfurt", "bruck an der mur", "leoben",
           "wiener neustadt", "melk", "tullnerfeld", "ybbs", "gansendorf", "ganserndorf",
           "laa an der thaya", "mistelbach", "selzthal", "kufstein", "bregenz", "feldkirch"),
    "SK": ("bratislava", "devinska nova ves", "petrzalka", "kuty", "trnava", "zilina", "kosice",
           "nove zamky", "sturovo", "komarno", "trencin", "poprad", "malacky", "leopoldov",
           "galanta", "banska bystrica", "zvolen", "cadca"),
    "HU": ("budapest", "budapest keleti", "budapest nyugati", "budapest deli", "budapest kelenfold",
           "budapesta", "gyor", "hegyeshalom", "szob", "komarom",
           "miskolc", "debrecen", "szeged", "sopron", "tatabanya", "pecs", "kecskemet",
           "szekesfehervar", "nyiregyhaza", "szolnok", "eger", "vac", "esztergom", "siofok",
           "keszthely", "szombathely", "zalaegerszeg", "veszprem", "mosonmagyarovar", "nagykanizsa",
           "kaposvar", "bekescsaba", "hatvan", "cegled", "szentendre", "godollo", "balatonfured",
           "hajduszoboszlo", "sarospatak", "zahony", "lokoshaza", "kelebia", "rajka",
           "budapest liszt ferenc", "madarsko", "magyarorszag"),
    "DE": ("munchen", "muenchen", "mnichov", "berlin", "berlin hbf", "dresden", "drazdany",
           "nurnberg", "nuernberg", "norimberk", "regensburg", "rezno", "passau", "pasov",
           "plattling", "landshut", "furth im wald", "cham", "schwandorf", "bayerisch eisenstein",
           "schirnding", "marktredwitz", "hof", "weiden", "bad schandau", "leipzig", "lipsko",
           "hamburg", "frankfurt", "stuttgart", "augsburg", "ingolstadt", "freilassing",
           "rosenheim", "bamberg", "bayreuth", "wurzburg", "zittau", "gorlitz", "goerlitz",
           "koln", "koeln", "cologne", "kolin nad rynem", "aachen", "cachy", "dusseldorf", "duisburg",
           "essen", "dortmund"),
    "PL": ("krakow", "krakov", "warszawa", "varsava", "katowice", "katovice", "wroclaw",
           "vratislav", "gdansk", "poznan", "zebrzydowice", "chalupki", "miedzylesie",
           "szczecin", "lodz", "opole", "rybnik", "bielsko biala", "gliwice"),
    "SI": ("ljubljana", "maribor", "sentilj", "celje", "koper", "nova gorica", "kranj",
           "novo mesto", "postojna", "sezana", "divaca", "zidani most", "pragersko", "dobova",
           "lesce bled", "murska sobota", "ptuj", "slovinsko", "slovenija"),
    "HR": ("zagreb", "zahreb", "rijeka", "split", "osijek", "zadar", "pula", "varazdin",
           "koprivnica", "karlovac", "sibenik", "slavonski brod", "vinkovci", "ogulin", "knin",
           "cakovec", "savski marof", "chorvatsko", "hrvatska"),
    "IT": ("venezia", "benatky", "roma", "rim", "milano", "milan", "trieste", "terst",
           "udine", "tarvisio", "verona", "bologna", "firenze", "florencie", "napoli", "neapol",
           "torino", "turin", "genova", "janov", "pisa", "bolzano", "bozen", "trento", "padova",
           "mestre", "bari", "palermo", "rimini", "brennero", "italie", "italia"),
    "CH": ("zurich", "curych", "basel", "bern", "geneve", "zeneva"),
    "NL": ("amsterdam", "utrecht", "rotterdam", "den haag", "schiphol", "eindhoven", "arnhem",
           "venlo", "groningen", "maastricht", "leiden", "haarlem", "zwolle", "enschede", "hengelo",
           "breda", "nijmegen", "amersfoort", "oldenzaal", "nizozemsko", "holandsko"),
    "DK": ("kobenhavn", "kodan", "copenhagen", "padborg"),
    "BE": ("bruxelles", "brusel", "brussel", "antwerpen", "liege", "brussels"),
    "GB": ("london", "londyn", "londres", "st pancras", "ashford", "ebbsfleet"),
    "FR": ("paris", "pariz", "strasbourg", "strasburk", "lyon", "marseille", "nice", "bordeaux",
           "lille", "montpellier", "toulouse", "nantes", "rennes", "nimes", "avignon", "perpignan",
           "narbonne", "marne la vallee", "aix en provence", "grenoble", "dijon", "le mans",
           "tours", "angers", "toulon", "francie"),
    "ES": ("madrid", "barcelona", "sevilla", "seville", "valencia", "valencie", "zaragoza",
           "saragossa", "malaga", "alicante", "murcia", "bilbao", "cordoba", "granada",
           "valladolid", "tarragona", "girona", "figueres", "albacete", "cuenca", "segovia", "elche",
           "santander", "san sebastian", "donostia", "salamanca", "oviedo", "gijon", "vigo",
           "a coruna", "santiago de compostela", "pamplona", "burgos", "cadiz", "huelva", "toledo",
           "badajoz", "lleida", "castellon", "ourense", "spanelsko", "espana"),
    "RO": ("bucuresti", "bukurest", "bukuresti", "bucharest", "bucuresti nord", "cluj", "cluj napoca",
           "brasov", "timisoara", "timisoara nord", "iasi", "constanta", "sibiu", "arad", "oradea",
           "curtici", "craiova", "ploiesti", "sinaia", "predeal", "suceava", "galati", "braila",
           "bacau", "deva", "alba iulia", "targu mures", "baia mare", "satu mare", "pitesti",
           "mangalia", "episcopia bihor", "rumunsko", "romania"),
}
_CZECH_COUNTRY = {"Rakousko": "AT", "Slovensko": "SK", "Německo": "DE", "Maďarsko": "HU",
                  "Polsko": "PL"}


def country_of(name: str) -> str | None:
    """Kód země stanice ("CZ", "AT", "SK" …); ``None`` = nevíme."""
    n = " " + normalize(name) + " "
    for code, words in _COUNTRY_WORDS.items():
        if any(f" {w} " in n for w in words):
            return code
    from . import stations

    place = stations.place(name)
    if place is None:
        return None
    if place.country:
        return _CZECH_COUNTRY.get(place.country)
    return "CZ"


def leaves_czechia(*names: str) -> bool:
    """Leží některé z míst prokazatelně mimo Česko? (Neznámé místo se nepočítá.)"""
    return any(country_of(n) not in (None, "CZ") for n in names)


# ---- hraniční přechody: (tranzitní země, cílová země) -> [(poslední v tranzitu, první v cíli)]
BORDERS: dict[tuple[str, str], list[tuple[str, str]]] = {
    ("AT", "SK"): [("Marchegg", "Devínska Nová Ves"), ("Kittsee", "Bratislava-Petržalka")],
    ("AT", "HU"): [("Nickelsdorf", "Hegyeshalom")],
    ("AT", "SI"): [("Spielfeld-Straß", "Šentilj")],
    ("AT", "IT"): [("Brennero/Brenner", "Brennero/Brenner")],
    ("SK", "HU"): [("Štúrovo", "Szob"), ("Komárno", "Komárom")],
    ("DE", "NL"): [("Bad Bentheim", "Oldenzaal")],
    ("DE", "DK"): [("Flensburg", "Padborg")],
    ("DE", "CH"): [("Basel Bad Bf", "Basel SBB")],
    ("DE", "BE"): [("Aachen Hbf", "Welkenraedt")],
    ("DE", "FR"): [("Kehl", "Strasbourg")],
}

# ---- trať Salzburg – Linz – Wien (od západu); jízdenka ČD do Česka ----------------
AT_CORRIDOR = ["salzburg", "vocklabruck", "attnang", "wels", "linz", "amstetten", "st polten",
               "wien"]
LINZ, WIEN = AT_CORRIDOR.index("linz"), AT_CORRIDOR.index("wien")
#: Stanice v Česku, kam vede jízdenka ČD: přes Linz do Horního Dvořiště,
#: přes Vídeň do Břeclavi.
VIA_LINZ, VIA_WIEN = "Horní Dvořiště", "Břeclav"


def corridor_index(name: str) -> int | None:
    n = " " + normalize(name).replace("sankt", "st") + " "
    aliases = {"linec": "linz", "vieden": "wien", "viden": "wien", "salcburk": "salzburg",
               "voecklabruck": "vocklabruck"}
    for alias, real in aliases.items():
        n = n.replace(f" {alias} ", f" {real} ")
    for i, word in enumerate(AT_CORRIDOR):
        if f" {word} " in n:
            return i
    return None


def corridor_targets(origin: str, destination: str) -> list[str]:
    """Stanice v Česku, do kterých jízdenka z ``origin`` vede přes ``destination``."""
    a, b = corridor_index(origin), corridor_index(destination)
    if a is None or b is None or a == b:
        return []
    out = []
    # do Horního Dvořiště přes Linz: cíl leží mezi výchozí stanicí a Lincem (včetně)
    if (a < LINZ and a < b <= LINZ) or (a > LINZ and LINZ <= b < a):
        out.append(VIA_LINZ)
    # do Břeclavi přes Vídeň: jen směrem na východ, cíl nejdál ve Vídni
    if a < b <= WIEN:
        out.append(VIA_WIEN)
    return out


# ---- regionální síťové jízdenky --------------------------------------------------
@dataclass(frozen=True)
class RegionalPass:
    name: str
    country: str
    entries: tuple[str, ...]          # první stanice za hranicí z Česka
    area: tuple[str, ...]             # klíčová slova cílů, kde jízdenka platí ("" = celá země)
    categories: tuple[str, ...]       # druhy vlaků, kde platí
    first: Decimal                    # EUR za prvního
    extra: Decimal                    # EUR za každého dalšího
    min_people: int
    max_people: int
    url: str

    def price(self, people: int) -> Decimal | None:
        if not self.min_people <= people <= self.max_people:
            return None
        if self.extra == 0:
            return self.first
        return self.first + self.extra * (people - 1)

    def valid_at(self, when: dt.datetime) -> bool:
        """Ve všední dny až od 9:00 (o víkendu celý den)."""
        return when.weekday() >= 5 or when.hour >= 9

    def train_ok(self, line: str) -> bool:
        prefix = (re.match(r"[A-Za-zäöü]+", line or "") or [""])[0].upper()
        return prefix in self.categories

    def covers(self, destination: str) -> bool:
        if not self.area:
            return country_of(destination) == self.country
        n = " " + normalize(destination) + " "
        return any(f" {w} " in n for w in self.area)


PASSES = [
    RegionalPass(
        "Einfach-Raus-Ticket", "AT",
        entries=("Gmünd NÖ", "Summerau", "Hohenau", "Bernhardsthal", "Retz", "Laa an der Thaya"),
        area=(), categories=("R", "REX", "S", "CJX"),
        first=Decimal(46), extra=Decimal(0), min_people=2, max_people=5,
        url="https://www.oebb.at/de/tickets-kundenkarten/freizeit-urlaub/einfach-raus-ticket"),
    RegionalPass(
        "Bayern-Ticket", "DE",
        entries=("Furth im Wald", "Bayerisch Eisenstein", "Schirnding", "Marktredwitz"),
        area=("munchen", "muenchen", "mnichov", "regensburg", "rezno", "nurnberg", "nuernberg",
              "norimberk", "passau", "pasov", "plattling", "landshut", "furth im wald", "cham",
              "schwandorf", "weiden", "marktredwitz", "hof", "bayreuth", "bamberg", "wurzburg",
              "augsburg", "ingolstadt", "rosenheim", "freilassing", "salzburg", "deggendorf",
              "straubing", "zwiesel", "bayerisch eisenstein", "schirnding", "lindau"),
        categories=("RB", "RE", "S", "ALX", "AG", "BRB", "MEX", "OPB", "ERB", "IRE", "M"),
        first=Decimal(34), extra=Decimal(10), min_people=1, max_people=5,
        url="https://www.bahn.de/angebot/regio/laender-tickets/bayern-ticket"),
]
