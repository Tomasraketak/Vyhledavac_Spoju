"""Renfe – Španělské dráhy (e-shop venta.renfe.com).

Web renfe.com i e-shop venta.renfe.com jsou klasické JSP stránky; seznam
spojů si stránka výsledků dotahuje přes DWR (Direct Web Remoting – Java RPC
přes POST s textovým tělem, odpověď je JavaScript, ne JSON):

* ``GET www.renfe.com/content/dam/renfe/es/General/buscadores/javascript/estacionesEstaticas.js``
  – statický seznam ~1 400 stanic (``var estacionesEstatico=[…]``, kódování
  ISO-8859-1). ``cdgoEstacion`` je kód pro hledání: pětimístné číslo stanice,
  nebo skupina města („MADRI“ = MADRID (TODAS), „BARCE“, „VALEN“ …);
  ``cdgoAdmon`` je správa: 0071 Španělsko, 0087 Francie, 0094 Portugalsko;
* ``POST venta.renfe.com/vol/buscarTren.do`` – formulář hledání (stanice,
  datum ``dd/mm/rrrr``, ``adultos_`` 14+ let, ``ninos_`` 4–13, ``ninosMenores``
  0–3). Uloží hledání **do serverové session** (cookie JSESSIONID) a vrátí
  HTML šablonu výsledků;
* ``POST /vol/dwr/call/plaincall/__System.generateId.dwr`` – ID DWR session;
* ``POST /vol/dwr/call/plaincall/trainEnlacesManager.getTrainsList.dwr`` –
  spoje celého dne (``listadoTrenes[0].listviajeViewEnlaceBean``): přímé
  vlaky i spoje s přestupem (``trayectos`` = jednotlivé vlaky), nejnižší
  cena na osobu ``tarifaMinima`` („87,15“) a všechny tarify
  (``tarifasDisponibles``: Básico, Elige, Prémium …).

Na co si dát pozor:

* **Hledání drží serverová session.** DWR vrací spoje toho hledání, které
  bylo naposled odesláno formulářem – parametry ``filtro`` se ve skutečnosti
  neuplatní. Každé hledání proto znamená POST formuláře a hned potom DWR
  dotaz; souběžná hledání se serializují zámkem (travel hacky volají
  dopravce paralelně).
* Hlavička ``Akamai-Key`` (hodnoty filtru spojené „, “) – posílá ji i web,
  bez ní ale dotaz zatím také projde. Žádná captcha ani challenge.
* **Cena je za osobu**, ne za skupinu. Při 4 a více cestujících Renfe nabízí
  skupinové tarify „… 4 o más“ a ``tarifaMinima`` už je zohledňuje.
  Cenu skupiny dopočítáváme: od 14 let plná cena, děti 4–13 let −40 %
  (AVE, AVE Internacional, Larga/Media Distancia, Avant), do 4 let zdarma
  bez místa. **AVLO** (nízkonákladové AVE) má pro děti pevnou cenu 5 € (nejvýš
  dvě na dospělého) – u přímého AVLO ji použijeme, u přestupu AVE + AVLO
  s dětmi je cena jen orientační (jde do ``journey.raw`` + poznámka).
* Odpověď DWR je JavaScriptový literál (klíče bez uvozovek, ``\\uXXXX``) –
  ``parse_dwr`` ho převádí na JSON.
* Seznam ukazuje jen vlaky Renfe (AVE, AVLO, Alvia, Euromed, MD …),
  ne konkurenci (OUIGO, iryo). Do Francie jen AVE Internacional (Barcelona –
  Lyon, Madrid – Marseille); Paříž Renfe neprodává.
* Web chystá přechod na nový prodejní systém („NSV“, skryté pole
  ``esConvivencia`` / ``fechaCorte`` ve stránce výsledků) – pak se postup
  nejspíš změní.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Španělsku nebo ve
Francii (a stanici zná seznam Renfe).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import re
import string
import warnings
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import quote

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

# DWR posílá textové tělo; HttpClient ho předává jako ``data=str``.
warnings.filterwarnings("ignore", message="Use 'content=", category=DeprecationWarning)

WEB = "https://www.renfe.com/es/es"
VENTA = "https://venta.renfe.com/vol"
STATIONS_URL = ("https://www.renfe.com/content/dam/renfe/es/General/buscadores/javascript/"
                "estacionesEstaticas.js")
RESULTS_PAGE = "/vol/buscarTrenEnlaces.do"
STATIONS_TTL = 7 * 24 * 3600
ADMON_COUNTRY = {"0071": "ES", "0087": "FR", "0094": "PT"}
CHILD_DISCOUNT = Decimal("0.6")      # děti 4–13 let: −40 %
AVLO_CHILD = Decimal(5)              # AVLO: dítě pevně 5 €, max. 2 na dospělého
#: Pořadí polí filtru – z něj web skládá hlavičku ``Akamai-Key``.
FILTER_KEYS = ("origen", "destino", "fechaSalida", "fechaVuelta", "trayecto", "idaVuelta", "adultos",
               "ninos", "ninosMenores", "sinEnlace", "conMascota", "conBicicleta", "plazaH", "atendo",
               "tipoFranjaI", "horaFranjaIda", "tipoFranjaV", "horaFranjaVuelta", "codPromo")
#: Česká a jiná jména měst → název v seznamu Renfe.
ALIASES = {"valencie": "valencia", "saragossa": "zaragoza", "seville": "sevilla", "pariz": "paris",
           "lisabon": "lisboa", "marsej": "marseille", "marseilles": "marseille"}


@PROVIDERS.register()
class RenfeProvider(HttpProvider):
    name = "Renfe"
    label = "Renfe – Španělské dráhy (jen cesty ze/do Španělska a AVE do Francie)"
    multi_passenger = True
    currency = "EUR"
    countries = ("ES", "FR", "PT")
    international_only = True
    referer = WEB + "/"
    min_interval = 0.5

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._lock = asyncio.Lock()
        self._dwr_session: str | None = None

    # ---- stanice ---------------------------------------------------------------------
    async def stations(self) -> list[dict[str, str]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        response = await self.client.request("GET", STATIONS_URL, referer=WEB + "/", expect_json=False)
        items = parse_stations(response.content.decode("latin-1"))
        if not items:
            raise TransientProviderError(self.name, "seznam stanic Renfe je prázdný")
        self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, str]:
        chosen = pick_station(place, await self.stations())
        if chosen is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} Renfe nezná")
        return chosen

    # ---- hledání ---------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and not {"ES", "FR"} & set(known):
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Španělsku či Francii")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if not {"ES", "FR"} & {origin["country"], destination["country"]}:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Španělsku či Francii")
        if origin["code"] == destination["code"]:
            raise StationNotFoundError(self.name, "výchozí a cílová stanice jsou stejné")
        adults, children, infants = count_passengers(query)
        date = query.departure.astimezone(DEFAULT_TZ).strftime("%d/%m/%Y")
        async with self._lock:
            text = await self._trains(origin, destination, date, adults, children, infants)
        data = parse_dwr(text)
        journeys = []
        for item in trains_of(data):
            journey = self.to_journey(item, adults, children)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    async def _trains(self, origin: dict[str, str], destination: dict[str, str], date: str,
                      adults: int, children: int, infants: int) -> str:
        # 1) formulář – uloží hledání do session
        form = {
            "tipoBusqueda": "autocomplete", "currenLocation": "menuBusqueda", "vengoderenfecom": "SI",
            "desOrigen": origin["name"], "desDestino": destination["name"],
            "cdgoOrigen": origin["code"], "cdgoDestino": destination["code"], "idiomaBusqueda": "ES",
            "FechaIdaSel": date, "FechaVueltaSel": "", "_fechaIdaVisual": date, "_fechaVueltaVisual": "",
            "adultos_": str(adults), "ninos_": str(children), "ninosMenores": str(infants),
            "codPromocional": "", "plazaH": "false", "sinEnlace": "false", "asistencia": "false",
            "franjaHoraI": "", "franjaHoraV": "", "Idioma": "es", "Pais": "ES",
        }
        await self.client.request("POST", f"{VENTA}/buscarTren.do?Idioma=es&Pais=ES", data=form,
                                  referer=WEB + "/", expect_json=False)
        # 2) DWR session (jednou na instanci)
        if self._dwr_session is None:
            reply = await self._dwr("__System", "generateId", {}, "")
            m = re.search(r'handleCallback\("\d+","\d+","([^"]+)"\)', reply)
            if not m:
                raise TransientProviderError(self.name, "DWR nevrátil ID session")
            self._dwr_session = m.group(1)
        page = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(10))
        # 3) seznam vlaků
        filtro = {"origen": origin["code"], "destino": destination["code"], "fechaSalida": date,
                  "fechaVuelta": "", "trayecto": "I", "idaVuelta": "", "adultos": str(adults),
                  "ninos": str(children), "ninosMenores": str(infants), "sinEnlace": "false",
                  "conMascota": "false", "conBicicleta": "false", "plazaH": "false", "atendo": "false",
                  "tipoFranjaI": "", "horaFranjaIda": "", "tipoFranjaV": "", "horaFranjaVuelta": "",
                  "codPromo": ""}
        akamai = ", ".join({"true": "1", "false": "0"}.get(filtro[k], filtro[k]) for k in FILTER_KEYS)
        text = await self._dwr("trainEnlacesManager", "getTrainsList", encode_object(filtro),
                               f"{self._dwr_session}/{page}", {"Akamai-Key": akamai.strip()})
        if "handleException" in text or "handleBatchException" in text:
            self._dwr_session = None
            m = re.search(r'localizedMessage:"([^"]*)"|message:\'([^\']*)\'', text)
            reason = (m.group(1) or m.group(2)) if m else "neznámá chyba"
            raise TransientProviderError(self.name, f"Renfe odmítlo hledání: {reason[:120]}")
        return text

    async def _dwr(self, script: str, method: str, params: dict[str, str], session: str,
                   headers: dict[str, str] | None = None) -> str:
        lines = {"callCount": "1", "nextReverseAjaxIndex": "0", "c0-scriptName": script,
                 "c0-methodName": method, "c0-id": "0", **params, "batchId": "0", "instanceId": "0",
                 "page": quote(RESULTS_PAGE, safe=""), "scriptSessionId": session, "windowName": ""}
        body = "".join(f"{k}={v}\n" for k, v in lines.items())
        response = await self.client.request(
            "POST", f"{VENTA}/dwr/call/plaincall/{script}.{method}.dwr", data=body,
            referer=f"https://venta.renfe.com{RESULTS_PAGE}", expect_json=False,
            headers={"Content-Type": "text/plain", "Accept": "*/*", **(headers or {})})
        return response.text

    # ---- převod ------------------------------------------------------------------------
    def to_journey(self, item: dict[str, Any], adults: int, children: int) -> Journey | None:
        legs = parse_legs(item)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        per_person = _decimal(item.get("tarifaMinima"))
        if per_person is None:
            if item.get("completo"):
                journey.fare_status = FareStatus.SOLD_OUT
            elif item.get("msgTrenNoDispo"):
                journey.fare_status = FareStatus.NOT_BOOKABLE
            return journey
        tariff = cheapest_tariff(item, per_person)
        total, exact = group_price(per_person, [leg.line.split(" ")[0] for leg in legs], adults, children)
        if not exact:
            journey.raw = {"per_person_eur": str(per_person), "indicative_eur": str(total)}
            journey.notes.append(f"Renfe uvádí {per_person:.2f} € za dospělého; cenu dětí na spoji "
                                 "s AVLO a přestupem nelze spočítat – ověřte u dopravce.")
            return journey
        journey.fares = [self.make_fare(total, "EUR", label="Renfe", tariff=tariff)]
        journey.fare_status = FareStatus.PRICED
        journey.raw = {"per_person_eur": str(per_person)}
        return journey


# ---- pomocné funkce (bez sítě, kvůli testům) ------------------------------------------
def parse_stations(script: str) -> list[dict[str, str]]:
    """``estacionesEstaticas.js`` → ``[{code, name, country}]`` bez duplicit."""
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"\[\s*\{.*?\}\s*\]", script, flags=re.S):
        try:
            items = json.loads(m.group(0))
        except ValueError:
            continue
        for s in items:
            code = str(s.get("cdgoEstacion") or "").strip()
            name = str(s.get("desgEstacion") or s.get("desgEstacionPlano") or "").strip()
            if code and name and code not in out:
                out[code] = {"code": code, "name": name,
                             "country": ADMON_COUNTRY.get(str(s.get("cdgoAdmon")), ""),
                             "priority": int(s.get("nmroPrioridad") or 9999)}
    return list(out.values())


def pick_station(place: str, stations: list[dict[str, str]]) -> dict[str, str] | None:
    """Skupina „MĚSTO (TODAS)“ pro samotné město, jinak nejpodobnější stanice."""
    wanted = normalize(place)
    wanted = " ".join(ALIASES.get(w, w) for w in wanted.split())
    if not wanted:
        return None
    names = [normalize(s["name"]) for s in stations]
    for s, n in zip(stations, names):
        if n == wanted or n == f"{wanted} todas":
            return s
    # samotné město: nejdůležitější stanice, jejíž název městem začíná (nmroPrioridad)
    starting = [s for s, n in zip(stations, names) if n.startswith(wanted + " ")]
    if starting:
        return min(starting, key=lambda s: (not normalize(s["name"]).endswith(" todas"),
                                            s.get("priority", 9999)))
    idx = best_match(wanted, [s["name"] for s in stations], threshold=0.45)
    return stations[idx] if idx is not None else None


def count_passengers(query: SearchQuery) -> tuple[int, int, int]:
    adults = sum(1 for p in query.passengers if p.age >= 14)
    children = sum(1 for p in query.passengers if 4 <= p.age < 14)
    infants = sum(1 for p in query.passengers if p.age < 4)
    if adults == 0:          # bez dospělého Renfe dítě neprodá – počítá se jako dospělý
        adults, children = (1, children - 1) if children else (1, 0)
    return adults, children, infants


def group_price(per_person: Decimal, kinds: list[str], adults: int, children: int) -> tuple[Decimal, bool]:
    """Cena za skupinu a zda je přesná (dospělí plně, děti −40 % / AVLO 5 €)."""
    total = per_person * adults
    if children:
        avlo = [k.upper() == "AVLO" for k in kinds]
        if all(avlo) and children <= 2 * adults:
            total += AVLO_CHILD * children
        elif any(avlo):
            return (total + per_person * CHILD_DISCOUNT * children).quantize(Decimal("0.01")), False
        else:
            total += per_person * CHILD_DISCOUNT * children
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), True


def cheapest_tariff(item: dict[str, Any], price: Decimal) -> str:
    for tariff in item.get("tarifasDisponibles") or []:
        if _decimal(tariff.get("precioTarifa")) == price and tariff.get("titulo"):
            return str(tariff["titulo"])
    return ""


def trains_of(data: Any) -> list[dict[str, Any]]:
    listing = (data or {}).get("listadoTrenes") or []
    if not listing or not isinstance(listing[0], dict):
        return []
    return listing[0].get("listviajeViewEnlaceBean") or []


def parse_legs(item: dict[str, Any]) -> list[Leg]:
    legs = []
    for t in item.get("trayectos") or []:
        day = t.get("fecha") or item.get("fecha")
        dep, arr = _time(day, t.get("horaSalida")), _time(day, t.get("horaLlegada"))
        if dep is None or arr is None:
            continue
        if arr < dep:
            arr += dt.timedelta(days=1)
        if legs and dep < legs[-1].arrival:       # přestup po půlnoci
            dep += dt.timedelta(days=1)
            arr += dt.timedelta(days=1)
        kind = str(t.get("tipoTren") or "").strip() or "Renfe"
        number = str(t.get("cdgoTren") or "").lstrip("0")
        origin = _title(t.get("descripcionEstacionOrigen"))
        destination = _title(t.get("descripcionEstacionDestino"))
        bus = "BUS" in kind.upper()
        # dálkové vlaky Renfe (AVE, AVLO, Alvia, MD …) mají místenku v ceně jízdenky
        reserved = not bus and not any(w in kind.upper() for w in ("REGIONAL", "CERCAN", "RODALIES"))
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN, carrier="Renfe",
                        line=f"{kind} {number}".strip(), reservation_required=reserved,
                        stops=[Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    return legs


_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
                    r'|(?P<word>[A-Za-z_$][\w$]*)|(?P<punct>[{}\[\]:,]))')


def parse_dwr(text: str) -> Any:
    """Výsledek z ``r.handleCallback("0","0", …)`` – JS literál převedený na JSON."""
    m = re.search(r'handleCallback\("\d+","\d+",', text)
    if not m:
        raise TransientProviderError("Renfe", "neočekávaná odpověď DWR")
    pos, depth, out = m.end(), 0, []
    in_object: list[bool] = []
    expect_key = False
    while True:
        tok = _TOKEN.match(text, pos)
        if tok is None:
            raise TransientProviderError("Renfe", "nečitelná odpověď DWR")
        pos = tok.end()
        if tok.group("str") is not None:
            out.append(_js_string(tok.group("str")))
            expect_key = False
        elif tok.group("num") is not None:
            out.append(tok.group("num"))
            expect_key = False
        elif tok.group("word") is not None:
            word = tok.group("word")
            if expect_key:
                out.append(json.dumps(word))
            elif word in ("null", "true", "false"):
                out.append(word)
            else:
                raise TransientProviderError("Renfe", f"neočekávaný symbol v DWR: {word}")
            expect_key = False
        else:
            p = tok.group("punct")
            out.append(p)
            if p in "{[":
                in_object.append(p == "{")
                depth += 1
                expect_key = p == "{"
            elif p in "}]":
                in_object.pop()
                depth -= 1
                expect_key = False
            elif p == ",":
                expect_key = bool(in_object and in_object[-1])
            elif p == ":":
                expect_key = False
        if depth == 0:
            break
    return json.loads("".join(out))


def _js_string(literal: str) -> str:
    """JS řetězec → JSON řetězec (JS navíc smí escapovat apostrof a lomítko)."""
    try:
        return json.dumps(json.loads(literal))
    except ValueError:
        return json.dumps(json.loads(re.sub(r"\\(['/])", r"\1", literal)))


def encode_object(fields: dict[str, str]) -> dict[str, str]:
    """Parametry DWR pro jeden objekt s řetězcovými poli."""
    params, refs = {}, []
    for i, (key, value) in enumerate(fields.items(), 1):
        params[f"c0-e{i}"] = "string:" + quote(value, safe="")
        refs.append(f"{key}:reference:c0-e{i}")
    params["c0-param0"] = "Object_Object:{" + ", ".join(refs) + "}"
    return params


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(".", "").replace(",", ".")) if "," in str(value) \
            else Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _title(value: Any) -> str:
    """„MADRID-PUERTA DE ATOCHA-ALMUDENA GRANDES“ → „Madrid-Puerta De Atocha-…“."""
    text = str(value or "").strip()
    return text.title() if text.isupper() else text


def _time(day: Any, hhmm: Any) -> dt.datetime | None:
    if not isinstance(day, str) or not isinstance(hhmm, str):
        return None
    try:
        parsed = dt.datetime.strptime(f"{day} {hhmm[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.replace(tzinfo=DEFAULT_TZ)
