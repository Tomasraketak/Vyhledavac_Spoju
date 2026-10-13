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
    "CH": ("zurich", "curych", "basel", "bern", "geneve", "zeneva", "geneva", "lausanne", "luzern",
           "lucern", "lugano", "st gallen", "winterthur", "interlaken", "chur", "brig", "visp", "olten",
           "thun", "zermatt", "biel", "sion", "montreux", "schaffhausen", "svycarsko", "schweiz", "suisse"),
    "NL": ("amsterdam", "utrecht", "rotterdam", "den haag", "schiphol", "eindhoven", "arnhem",
           "venlo", "groningen", "maastricht", "leiden", "haarlem", "zwolle", "enschede", "hengelo",
           "breda", "nijmegen", "amersfoort", "oldenzaal", "nizozemsko", "holandsko"),
    "DK": ("kobenhavn", "kodan", "copenhagen", "padborg", "aarhus", "arhus", "odense", "aalborg",
           "esbjerg", "fredericia", "kolding", "vejle", "horsens", "randers", "roskilde", "naestved",
           "helsingor", "kastrup", "nyborg", "slagelse", "herning", "silkeborg", "viborg", "holstebro",
           "frederikshavn", "hjorring", "sonderborg", "tonder", "rodby", "nykobing falster",
           "dansko", "danmark"),
    "BG": ("sofia", "sofie", "plovdiv", "varna", "burgas", "ruse", "stara zagora", "pleven",
           "veliko tarnovo", "gorna oryahovitsa", "gorna orjahovica", "blagoevgrad", "kardzhali", "haskovo",
           "dimitrovgrad", "sliven", "yambol", "shumen", "dobrich", "vidin", "montana", "kulata",
           "svilengrad", "bulharsko", "bulgaria"),
    "PT": ("lisboa", "lisabon", "lisbon", "porto", "coimbra", "faro", "braga", "aveiro", "evora", "guimaraes",
           "entroncamento", "santarem", "leiria", "viana do castelo", "tunes", "lagos", "setubal", "cascais",
           "sintra", "portugalsko", "portugal"),
    "RS": ("beograd", "belehrad", "belgrade", "novi sad", "nis", "subotica", "kragujevac", "kraljevo",
           "uzice", "valjevo", "pancevo", "zrenjanin", "sombor", "vranje", "lapovo", "jagodina",
           "srbsko", "srbija"),
    "NO": ("oslo", "bergen", "trondheim", "stavanger", "drammen", "kristiansand", "fredrikstad",
           "lillehammer", "hamar", "bodo", "narvik", "gardermoen", "myrdal", "flam", "andalsnes",
           "halden", "sarpsborg", "skien", "larvik", "tonsberg", "lillestrom", "kongsberg",
           "voss", "egersund", "dombas", "roros", "mo i rana", "steinkjer", "norsko", "norge"),
    "SE": ("stockholm", "stokholm", "goteborg", "gothenburg", "malmo", "uppsala", "vasteras",
           "orebro", "linkoping", "helsingborg", "norrkoping", "jonkoping", "lund", "umea", "gavle",
           "boras", "sundsvall", "karlstad", "vaxjo", "halmstad", "lulea", "kiruna", "ostersund",
           "kalmar", "karlskrona", "eskilstuna", "sodertalje", "arlanda", "hassleholm", "alvesta",
           "nassjo", "hallsberg", "katrineholm", "falun", "borlange", "mora", "abisko",
           "svedsko", "sverige"),
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


#: Značky zemí, které dopravci píší jinak než ISO (ÖBB „Jesenice(SL)“).
_MARKS = {"SL": "SI", "UK": "GB", "CS": "CZ", "SLO": "SI", "AUT": "AT", "DEU": "DE", "GER": "DE", "CZE": "CZ",
          "SVK": "SK", "HUN": "HU", "POL": "PL", "ITA": "IT", "HRV": "HR", "CRO": "HR", "SUI": "CH", "CHE": "CH",
          "SZ": "SI", "HZ": "HR"}       # SZ/HZ = značka drah (Loka(SZ))


def marked_country(name: str) -> str | None:
    """Země výslovně uvedená u jména stanice („Rybnik(PL)“, „Jesenice(SL)“)."""
    m = re.search(r"\(([A-Z]{2,3})\)\s*$", name or "")
    if not m:
        return None
    code = _MARKS.get(m.group(1), m.group(1))
    return code if len(code) == 2 else None


def country_of(name: str) -> str | None:
    """Kód země stanice ("CZ", "AT", "SK" …); ``None`` = nevíme."""
    marked = marked_country(name)
    if marked:
        return marked
    n = " " + normalize(name) + " "
    for code, words in _COUNTRY_WORDS.items():
        if any(f" {w} " in n for w in words):
            return code
    from . import stations

    place = stations.place(name)
    if place is None:
        return None
    if place.country:
        return place.code or None
    return "CZ"


#: Země mimo středoevropský čas. Jízdní řády (IDOS, MÁV, HAFAS) píší čas
#: místní – odjezd z Bukurešti v 16:00 je v Praze 15:00.
_ZONES = {"RO": "Europe/Bucharest", "BG": "Europe/Sofia", "GR": "Europe/Athens", "FI": "Europe/Helsinki",
          "EE": "Europe/Tallinn", "LV": "Europe/Riga", "LT": "Europe/Vilnius", "UA": "Europe/Kyiv",
          "MD": "Europe/Chisinau", "TR": "Europe/Istanbul", "BY": "Europe/Minsk", "RU": "Europe/Moscow",
          "GB": "Europe/London", "IE": "Europe/Dublin", "PT": "Europe/Lisbon"}


def zone_of(name: str) -> dt.tzinfo:
    """Časové pásmo stanice (výchozí středoevropské)."""
    from .models import DEFAULT_TZ

    zone = _ZONES.get(country_of(name) or "")
    if zone is None:
        return DEFAULT_TZ
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(zone)
    except Exception:  # noqa: BLE001 - Windows bez tzdata: radši středoevropský než pád
        return DEFAULT_TZ


def local_time(moment: dt.datetime, name: str) -> dt.datetime:
    """Čas z jízdního řádu (místní ve stanici ``name``), zapsaný jako středoevropský,
    přepočtený na správné pásmo: 16:00 v Bukurešti → 15:00 SELČ."""
    zone = zone_of(name)
    return moment.replace(tzinfo=zone) if zone is not moment.tzinfo else moment


def country_near(name: str, neighbours: set[str]) -> str | None:
    """Země stanice s ohledem na sousední stanice trasy: „Jesenice“ mezi
    Villachem a Lublaní je slovinská, ne česká obec stejného jména."""
    found = country_of(name)
    neighbours = {c for c in neighbours if c}
    if neighbours and found not in neighbours:
        from . import stations

        candidates = stations.countries_for(name) & neighbours
        if candidates:
            return sorted(candidates)[0]
    return found


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
