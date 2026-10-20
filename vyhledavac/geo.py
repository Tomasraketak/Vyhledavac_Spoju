"""Zeměpis pro mezinárodní travel hacky.

* ``country_of`` – v jaké zemi leží stanice (podle klíčových slov u
  zahraničí, jinak databáze zastávek ČR);
* ``BORDERS`` – hraniční přechody mezi dvěma zahraničními státy: poslední
  stanice v tranzitní zemi a první v cílové (Marchegg / Devínska Nová Ves);
* ``AT_CORRIDOR`` – trať Salzburg – Linz – Wien, kde se vyplatí jízdenka ČD až
  do Horního Dvořiště nebo Břeclavi;
* ``AT_STATES`` / ``state_of`` – rakouské spolkové země stanic (kde platí
  zemské jízdenky), ``holidays`` – státní svátky (jízdenky platí jako o víkendu);
* ``PASSES`` – regionální síťové jízdenky (Einfach-Raus-Ticket, Bayern-Ticket,
  rakouské zemské Freizeit-Tickety), vstupní stanice z Česka, oblast,
  platnost a kategorie vlaků, ve kterých platí.

Ceny síťových jízdenek (říjen 2026, ověřte na oebb.at / bahn.de / u svazů):
Einfach-Raus-Ticket 38/42/46/50 € pro 2–5 osob (jen regionální vlaky ÖBB:
S, R, REX, CJX; ve všední dny od 9:00), Bayern-Ticket 34 € + 10 € za každou
další osobu (max. 5; RB, RE, S, ALX, ag …; ve všední dny od 9:00), VOR
Freizeitticket 19,90 € (s Vídní 29,60 €), Freizeit-Ticket OÖ 26,50 €
a Salzburg 25 € (2 dospělí + 4 děti), Steiermark 15 €, Kärnten 14,90 €
(dítě 7,90 €), Tagesticket Tirol 2Plus 42,60 €, VMOBIL MAXIMO 18,40 €.
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
    if state_of(name):
        return "AT"
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


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Vzdušná vzdálenost dvou bodů (šířka, délka) v km."""
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


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


# ---- rakouské spolkové země (kde platí zemské jízdenky) ----------------------------
#: Klíčová slova (normalizovaná) stanic po spolkových zemích: W Vídeň, N Dolní
#: Rakousko, B Burgenland, O Horní Rakousko, S Salcbursko, ST Štýrsko,
#: K Korutany, T Tyrolsko, V Vorarlbersko. Rozhoduje nejdelší shoda
#: („flughafen wien“ je v Dolním Rakousku, „gmund in karnten“ v Korutanech).
AT_STATES: dict[str, tuple[str, ...]] = {
    "W": ("wien", "vienna", "viden", "vieden", "wien hbf", "wien meidling", "wien floridsdorf",
          "wien praterstern", "wien mitte", "wien westbahnhof", "wien hutteldorf", "wien handelskai",
          "wien simmering", "wien liesing", "wien heiligenstadt", "wien stadlau", "wien aspern nord"),
    "N": ("flughafen wien", "st polten", "sankt polten", "amstetten", "melk", "ybbs", "ybbs an der donau",
          "pochlarn", "tullnerfeld", "tulln", "krems", "krems an der donau", "gmund", "gmund no",
          "schwarzenau", "zwettl", "retz", "hollabrunn", "stockerau", "korneuburg", "mistelbach",
          "laa an der thaya", "hohenau", "hohenau an der march", "bernhardsthal", "gansendorf",
          "ganserndorf", "marchegg", "wiener neustadt", "modling", "neunkirchen", "neunkirchen no",
          "gloggnitz", "semmering", "payerbach reichenau", "bruck an der leitha", "bruck leitha",
          "schwechat", "st valentin", "scheibbs", "wolkersdorf", "poysdorf", "wieselburg",
          "waidhofen an der ybbs", "waidhofen ybbs", "leobersdorf", "deutsch wagram", "herzogenburg",
          "traismauer", "neulengbach", "lilienfeld", "traisen", "hainfeld", "absdorf hippersdorf",
          "sigmundsherberg", "ebenfurth", "bad voslau", "baden bei wien", "wiener neudorf",
          "purkersdorf", "pressbaum", "klosterneuburg", "laxenburg", "himberg", "gramatneusiedl",
          "felixdorf", "ternitz", "puchberg am schneeberg", "kirchberg an der pielach",
          "mariazell"),        # Mariazellerbahn (NÖVOG) končí ve Štýrsku, nádraží patří k NÖ síti
    "B": ("eisenstadt", "neusiedl am see", "parndorf", "kittsee", "nickelsdorf", "pamhagen",
          "deutschkreutz", "mattersburg", "oberwart", "gussing", "frauenkirchen", "jennersdorf",
          "pinkafeld", "wulkaprodersdorf", "gols", "zurndorf", "bruckneudorf", "neusiedl",
          "wallern im burgenland", "st andra am zicksee", "purbach", "schutzen am gebirge",
          "siegendorf", "mullendorf"),
    "O": ("linz", "linec", "linz donau", "linz hbf", "linz donau hbf", "linz urfahr", "wels", "attnang",
          "attnang puchheim", "vocklabruck", "voecklabruck", "steyr", "enns", "freistadt", "summerau",
          "gaisbach wartberg", "kefermarkt", "pregarten", "gmunden", "bad ischl", "ebensee", "hallstatt",
          "obertraun", "bad goisern", "traunkirchen", "braunau", "braunau am inn", "ried im innkreis",
          "scharding", "grieskirchen", "eferding", "kirchdorf an der krems", "windischgarsten",
          "spital am pyhrn", "marchtrenk", "horsching", "traun", "ansfelden", "leonding",
          "st georgen an der gusen", "mauthausen", "perg", "grein", "kremsmunster", "lambach",
          "schwanenstadt", "timelkam", "frankenmarkt", "aigen schlagl", "rohrbach berg", "bad hall",
          "garsten", "losenstein", "weyer", "kleinreifling", "neumarkt kallham", "wels hbf",
          "st valentin landesgrenze", "hinterstoder", "mattighofen", "altheim"),
    "S": ("salzburg", "salcburk", "salzburg hbf", "hallein", "golling abtenau", "golling",
          "bischofshofen", "schwarzach st veit", "zell am see", "saalfelden", "st johann im pongau",
          "bad gastein", "bad hofgastein", "radstadt", "mittersill", "krimml", "seekirchen",
          "seekirchen am wallersee", "neumarkt am wallersee", "strasswalchen", "steindorf bei strasswalchen",
          "oberndorf bei salzburg", "lamprechtshausen", "burmoos", "tamsweg", "werfen", "kaprun",
          "bruck fusch", "eben im pongau", "altenmarkt im pongau", "pinzgauer lokalbahn",
          "salzburg liefering", "salzburg gnigl", "salzburg aigen", "kuchl", "puch bei hallein",
          "elsbethen"),
    "ST": ("graz", "graz hbf", "styrsky hradec", "bruck an der mur", "bruck mur", "leoben", "leoben hbf",
           "kapfenberg", "murzzuschlag", "kindberg", "knittelfeld", "judenburg", "zeltweg", "unzmarkt",
           "selzthal", "liezen", "schladming", "stainach irdning", "bad aussee", "admont", "gleisdorf",
           "weiz", "feldbach", "fehring", "leibnitz", "spielfeld", "spielfeld strass", "wildon",
           "koflach", "voitsberg", "deutschlandsberg", "frohnleiten", "peggau deutschfeistritz",
           "eisenerz", "trofaiach", "hartberg", "furstenfeld", "bad radkersburg", "gratkorn", "mixnitz",
           "murau", "murau stolzalpe", "lieboch", "kalsdorf", "graz flughafen", "graz ostbahnhof",
           "graz don bosco", "puntigam", "st michael in obersteiermark", "mitterdorf veitsch", "krieglach",
           "langenwang", "spital am semmering", "grobming", "trieben", "rottenmann",
           "fohnsdorf", "fridlbach", "stubenberg", "wies eibiswald", "gross st florian"),
    "K": ("klagenfurt", "klagenfurt hbf", "villach", "villach hbf", "spittal millstattersee",
          "spittal an der drau", "st veit an der glan", "wolfsberg", "feldkirchen in karnten",
          "velden am worthersee", "velden", "portschach am worthersee", "portschach", "krumpendorf",
          "bleiburg", "volkermarkt", "hermagor", "mallnitz obervellach", "mallnitz", "obervellach",
          "arnoldstein", "rosenbach", "friesach", "treibach althofen", "launsdorf hochosterwitz",
          "oberdrauburg", "greifenburg", "paternion", "ferndorf", "gmund in karnten", "radenthein",
          "ossiach", "st paul im lavanttal", "lavamund", "villach westbahnhof", "klagenfurt ost",
          "lendorf", "sachsenburg", "kuhnsdorf", "grafenstein", "faak am see",
          "st andra im lavanttal", "kotschach mauthen"),
    "T": ("innsbruck", "innsbruck hbf", "kufstein", "worgl", "worgl hbf", "jenbach", "schwaz",
          "hall in tirol", "telfs", "telfs pfaffenhofen", "landeck", "landeck zams", "imst", "imst pitztal",
          "otztal", "kitzbuhel", "st johann in tirol", "fieberbrunn", "hochfilzen", "kirchberg in tirol",
          "brixlegg", "rattenberg", "matrei", "matrei am brenner", "steinach am brenner", "seefeld",
          "seefeld in tirol", "scharnitz", "reutte", "reutte in tirol", "lienz", "ehrwald",
          "ehrwald zugspitzbahn", "mayrhofen", "zell am ziller", "fugen", "st anton am arlberg",
          "langkampfen", "kundl", "radfeld", "volders", "fritzens", "terfens", "zirl",
          "kematen in tirol", "volders baumkirchen", "innsbruck west", "pians", "strengen", "kirchbichl", "schaftenau", "wattens", "absam",
          "hopfgarten", "westendorf", "oberndorf in tirol", "kitzbuhel hahnenkamm"),
    "V": ("bregenz", "bregenz hafen", "dornbirn", "feldkirch", "bludenz", "hohenems", "gotzis",
          "rankweil", "lustenau", "lochau", "lochau horbranz", "wolfurt", "schruns", "langen am arlberg",
          "nenzing", "frastanz", "lauterach", "altach", "hard fussach", "bregenz riedenburg",
          "dornbirn schoren", "dornbirn haselstauden", "klaus in vorarlberg", "sulz rothis",
          "dalaas", "klosterle", "schlins beschling", "ludesch", "nuziders", "tschagguns",
          "vandans", "st anton im montafon", "lingenau"),
}
_STATE_NAMES = {"W": "Vídeň", "N": "Dolní Rakousko", "B": "Burgenland", "O": "Horní Rakousko",
                "S": "Salcbursko", "ST": "Štýrsko", "K": "Korutany", "T": "Tyrolsko", "V": "Vorarlbersko"}
_STATE_INDEX: list[tuple[str, str]] = sorted(
    ((word, code) for code, words in AT_STATES.items() for word in words),
    key=lambda item: -len(item[0]))


def state_of(name: str) -> str | None:
    """Rakouská spolková země stanice ("W", "N", "O" …); ``None`` = nevíme.
    Rozhoduje nejdelší klíčové slovo, takže „Flughafen Wien“ je N, ne W."""
    if marked_country(name) not in (None, "AT"):
        return None
    n = " " + normalize(name) + " "
    for word, code in _STATE_INDEX:
        if f" {word} " in n:
            return code
    return None


def state_name(code: str) -> str:
    return _STATE_NAMES.get(code, code)


# ---- státní svátky (síťové jízdenky platí jako o víkendu) -----------------------
def _easter(year: int) -> dt.date:
    """Velikonoční neděle (gregoriánský výpočet, anonymní algoritmus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7          # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def holidays(year: int, country: str) -> frozenset[dt.date]:
    """Státní svátky v Rakousku ("AT") a Bavorsku ("DE")."""
    easter = _easter(year)
    moving = {"AT": (1, 39, 50, 60), "DE": (-2, 1, 39, 50, 60)}.get(country, ())
    fixed = {"AT": ((1, 1), (1, 6), (5, 1), (8, 15), (10, 26), (11, 1), (12, 8), (12, 25), (12, 26)),
             "DE": ((1, 1), (1, 6), (5, 1), (8, 15), (10, 3), (11, 1), (12, 25), (12, 26))}.get(country, ())
    return frozenset([*(easter + dt.timedelta(days=d) for d in moving),
                      *(dt.date(year, m, d) for m, d in fixed)])


def is_day_off(day: dt.date, country: str) -> bool:
    """Sobota, neděle nebo státní svátek."""
    return day.weekday() >= 5 or day in holidays(day.year, country)


# ---- regionální síťové jízdenky --------------------------------------------------
@dataclass(frozen=True)
class RegionalPass:
    """Síťová jízdenka na den (Einfach-Raus-Ticket, Bayern-Ticket, zemské
    Freizeit-Tickety). Cena podle cestujících, platnost podle dne a hodiny,
    oblast podle země, spolkových zemí nebo klíčových slov stanic."""

    name: str
    country: str
    entries: tuple[str, ...]          # první stanice za hranicí z Česka (prázdné = jen cesty uvnitř oblasti)
    area: tuple[str, ...]             # klíčová slova cílů, kde jízdenka platí ("" = celá země / spolkové země)
    categories: tuple[str, ...]       # druhy vlaků, kde platí
    first: Decimal                    # EUR za prvního (u rodinné jízdenky cena za skupinu)
    extra: Decimal                    # EUR za každého dalšího
    min_people: int
    max_people: int
    url: str
    states: tuple[str, ...] = ()      # rakouské spolkové země (``AT_STATES``)
    group: tuple[tuple[int, Decimal], ...] = ()   # cena podle počtu osob (Einfach-Raus-Ticket)
    child: Decimal | None = None      # EUR za dítě 6–14 let (jinak jako dospělý)
    max_adults: int | None = None     # rodinná jízdenka: nejvýš tolik dospělých …
    max_children: int = 0             # … a dětí do 15 let, cena ``first`` za všechny
    weekend_only: bool = False        # jen soboty, neděle a svátky
    weekday_from: int | None = 9      # ve všední dny až od (hodina); ``None`` = celý den
    until: int = 3                    # platí do této hodiny následujícího dne (0 = do půlnoci)
    hours: int | None = None          # místo kalendářního dne platí N hodin od začátku
    local: bool = False               # i autobusy, tramvaje a metro v oblasti
    validity: str = "ve všední dny od 9:00, o víkendu celý den"

    def price(self, passengers) -> Decimal | None:
        """Cena v EUR za skupinu; ``passengers`` = počet dospělých nebo ``Passenger``-y.
        Děti do 6 let jezdí zdarma a nepočítají se. ``None`` = skupina se nevejde."""
        ages = [26] * passengers if isinstance(passengers, int) else [p.age for p in passengers]
        ages = [a for a in ages if a >= 6]
        children = sum(1 for a in ages if a < 15)
        adults = len(ages) - children
        if self.max_adults is not None:                       # rodinná jízdenka
            if not 1 <= adults <= self.max_adults or children > self.max_children:
                return None
            return self.first
        if not self.min_people <= len(ages) <= self.max_people:
            return None
        if self.group:
            return dict(self.group).get(len(ages))
        if self.child is not None:
            if adults == 0:
                return self.child * children
            return self.first + self.extra * (adults - 1) + self.child * children
        if self.extra == 0:
            return self.first
        return self.first + self.extra * (len(ages) - 1)

    def valid_at(self, when: dt.datetime) -> bool:
        """Dá se s jízdenkou v tuto chvíli vyrazit? (Víkendy a svátky celý den.)"""
        day_off = is_day_off(when.date(), self.country)
        if self.weekend_only:
            return day_off
        return day_off or self.weekday_from is None or when.hour >= self.weekday_from

    def valid_until(self, start: dt.datetime, end: dt.datetime) -> bool:
        """Stihne se cesta, než jízdenka propadne?"""
        if self.hours is not None:
            return end - start <= dt.timedelta(hours=self.hours)
        midnight = dt.datetime.combine(start.date() + dt.timedelta(days=1), dt.time(),
                                       tzinfo=start.tzinfo)
        return end <= midnight + dt.timedelta(hours=self.until)

    def train_ok(self, line: str) -> bool:
        prefix = (re.match(r"[A-Za-zäöü]+", line or "") or [""])[0].upper()
        return prefix in self.categories

    def covers(self, destination: str) -> bool:
        """Leží stanice v oblasti jízdenky?"""
        if self.states:
            return state_of(destination) in self.states
        if not self.area:
            return country_of(destination) == self.country
        n = " " + normalize(destination) + " "
        return any(f" {w} " in n for w in self.area)

    def outside(self, name: str) -> bool:
        """Leží stanice prokazatelně mimo oblast? (Neznámá stanice ne.)"""
        if self.covers(name):
            return False
        if self.states:
            state = state_of(name)
            if state is not None:
                return True
        country = country_of(name)
        return country is not None and country != self.country

    def covers_legs(self, legs) -> bool:
        """Dá se celý úsek ``legs`` projet s touto jízdenkou? Druhy vlaků (a autobusů,
        tramvají), začátek a konec v oblasti, žádná zastávka prokazatelně mimo ni."""
        rides = [leg for leg in legs if leg.mode.value != "walk"]
        if not rides:
            return False
        for leg in rides:
            if leg.mode.value == "train":
                if not self.train_ok(leg.line):
                    return False
            elif not self.local:
                return False
            names = [leg.origin, leg.destination, *(s.name for s in leg.stops)]
            if any(self.outside(name) for name in names):
                return False
        return self.covers(rides[0].origin) and self.covers(rides[-1].destination)

    @property
    def area_label(self) -> str:
        if self.states:
            return ", ".join(state_name(s) for s in self.states)
        return {"AT": "celé Rakousko", "DE": "Bavorsko"}.get(self.country, self.country)


_REGIO_AT = ("R", "REX", "S", "CJX")
#: Rakouské vstupní stanice z Česka do Dolního Rakouska (Gmünd, Břeclav, Znojmo, Mikulov)
#: a Horního Rakouska (Horní Dvořiště → Summerau).
_ENTRIES_NO = ("Gmünd NÖ", "Hohenau", "Bernhardsthal", "Retz", "Laa an der Thaya")

PASSES = [
    RegionalPass(
        "Einfach-Raus-Ticket", "AT",
        entries=(*_ENTRIES_NO[:1], "Summerau", *_ENTRIES_NO[1:]),
        area=(), categories=_REGIO_AT,
        first=Decimal(38), extra=Decimal(0), min_people=2, max_people=5,
        group=((2, Decimal(38)), (3, Decimal(42)), (4, Decimal(46)), (5, Decimal(50))),
        url="https://www.oebb.at/de/tickets-kundenkarten/freizeit-urlaub/einfach-raus-ticket",
        validity="2–5 osob, ve všední dny od 9:00, o víkendu a svátcích celý den, do 3:00 dalšího dne; "
                 "jen regionální vlaky ÖBB a Raaberbahn, ne autobusy a tramvaje"),
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
        url="https://www.bahn.de/angebot/regio/laender-tickets/bayern-ticket",
        validity="ve všední dny od 9:00, o víkendu a svátcích celý den, do 3:00 dalšího dne"),
    # ---- rakouské zemské jízdenky na den (ceny 2026, ověřte u dopravce) -----------
    RegionalPass(
        "VOR Freizeitticket", "AT", entries=_ENTRIES_NO, area=(), states=("N", "B"),
        categories=_REGIO_AT, first=Decimal("19.90"), extra=Decimal("19.90"),
        min_people=1, max_people=9, weekend_only=True, until=0, local=True,
        url="https://shop.vor.at/index.php/vor/moderation/selectStandardProducts",
        validity="za osobu, jen v soboty, neděle a svátky celý den; regionální vlaky a autobusy VOR "
                 "v Dolním Rakousku a Burgenlandu, ne ve Vídni"),
    RegionalPass(
        "VOR Freizeitticket Plus", "AT", entries=_ENTRIES_NO, area=(), states=("W", "N", "B"),
        categories=_REGIO_AT, first=Decimal("29.60"), extra=Decimal("29.60"),
        min_people=1, max_people=9, weekend_only=True, until=0, local=True,
        url="https://shop.vor.at/index.php/vor/moderation/selectStandardProducts",
        validity="za osobu, jen v soboty, neděle a svátky celý den; regionální vlaky a autobusy VOR "
                 "v Dolním Rakousku, Burgenlandu i MHD ve Vídni"),
    RegionalPass(
        "Freizeit-Ticket OÖ", "AT", entries=("Summerau",), area=(), states=("O",),
        categories=_REGIO_AT, first=Decimal("26.50"), extra=Decimal(0), min_people=1, max_people=6,
        max_adults=2, max_children=4, until=3, local=True,
        url="https://www.ooevv.at/de/tickets-preise/freizeit-ticket-ooe.html",
        validity="až 2 dospělí a 4 děti do 15 let, ve všední dny od 9:00, o víkendu a svátcích celý den, "
                 "do 3:00 dalšího dne; regionální vlaky, autobusy a MHD OÖVV"),
    RegionalPass(
        "Freizeit-Ticket Salzburg", "AT", entries=(), area=(), states=("S",),
        categories=_REGIO_AT, first=Decimal(25), extra=Decimal(0), min_people=1, max_people=6,
        max_adults=2, max_children=4, weekday_from=None, until=3, local=True,
        url="https://salzburg-verkehr.at/tickets-preise/einzelfahrkarten/freizeit-ticket-salzburg/",
        validity="až 2 dospělí a 4 děti do 15 let, celý den do 3:00 dalšího dne; regionální vlaky, "
                 "autobusy a MHD v Salcbursku, ne dálkové vlaky"),
    RegionalPass(
        "Freizeit-Ticket Steiermark", "AT", entries=(), area=(), states=("ST",),
        categories=("S", "R", "REX", "IR"), first=Decimal(15), extra=Decimal(15),
        min_people=1, max_people=9, weekend_only=True, until=0, local=True,
        url="https://www.oebb.at/de/regionale-angebote/steiermark/freizeit-ticket-steiermark",
        validity="za osobu, jen v soboty, neděle a svátky 0–24 h; S-Bahn, R, REX, IR, autobusy "
                 "a MHD ve Štýrsku, ne dálkové vlaky"),
    RegionalPass(
        "Freizeit-Ticket Kärnten", "AT", entries=(), area=(), states=("K",),
        categories=("S", "R", "REX", "IR"), first=Decimal("14.90"), extra=Decimal("14.90"),
        child=Decimal("7.90"), min_people=1, max_people=9, weekend_only=True, until=3, local=True,
        url="https://www.oebb.at/de/regionale-angebote/kaernten/freizeit-ticket.html",
        validity="14,90 € dospělý, 7,90 € dítě, jen v soboty, neděle a svátky do 3:00 dalšího dne; "
                 "S-Bahn, REX, regionální vlaky, IR v Korutanech a autobusy Kärntner Linien, "
                 "ne Railjet/EC/IC/Nightjet"),
    RegionalPass(
        "Tagesticket Tirol 2Plus", "AT", entries=(), area=(), states=("T",),
        categories=_REGIO_AT, first=Decimal("42.60"), extra=Decimal(0), min_people=1, max_people=5,
        max_adults=2, max_children=3, weekday_from=None, until=0, local=True,
        url="https://www.vvt.at/tickets/zeitkarten",
        validity="až 2 dospělí a 3 děti, celý den; regionální vlaky, autobusy a MHD VVT v Tyrolsku, "
                 "ne dálkové vlaky"),
    RegionalPass(
        "VMOBIL Tageskarte MAXIMO", "AT", entries=(), area=(), states=("V",),
        categories=_REGIO_AT, first=Decimal("18.40"), extra=Decimal("18.40"),
        min_people=1, max_people=9, weekday_from=None, hours=24, local=True,
        url="https://www.vmobil.at/de/tickets",
        validity="za osobu, 24 hodin od začátku platnosti; vlaky a autobusy v celém Vorarlbersku "
                 "(levnější LOKAL 4,60 € / REGIO 9,20 € jen pro menší oblast)"),
]
