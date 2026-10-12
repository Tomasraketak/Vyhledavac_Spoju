"""CFR Călători – rumunské státní dráhy (e-shop bilete.cfrcalatori.ro).

E-shop je klasická ASP.NET MVC aplikace; výsledky hledání i ceny vrací jako
kusy HTML. Bez API klíče a bez captchy (při běžném provozu) to vypadá takto:

1. ``GET /ro-RO/Itineraries`` – stránka s formulářem; v JavaScriptu je
   ``availableStations = [{ name: "București Nord", … }]`` – všech ~1 700
   rumunských stanic (cache 14 dní). Skupina celého města je
   „București (toate stațiile)“.
2. ``GET /ro-RO/Rute-trenuri/{Odkud}/{Kam}?DepartureDate=dd.mm.yyyy&MinutesInDay=…``
   – jména bez diakritiky, mezery -> pomlčky („Bucuresti-Nord“). Stránka
   obsahuje skrytý formulář s ``ConfirmationKey`` (podpis parametrů hledání)
   a antiforgery tokenem ``__RequestVerificationToken`` (+ cookie).
3. ``POST /ro-RO/Itineraries/GetItineraries`` (formulář z kroku 2) – HTML se
   spoji (``li-itinerary-N``): vlaky, časy „Ple 14 oct. 06:09“ / „Sos …“,
   přestupní stanice, mezizastávky a u každého spoje formulář na cenu
   s ``TransactionString``.
4. ``POST /api/ro-RO/Itineraries/Price`` – ``TicketFareTypeId`` (102 dospělý,
   101 dítě), ``TrainServiceKeys[i]`` (výchozí 2. třída „B&B“),
   ``TransactionString`` a token → „<strong>75,0 lei</strong>“ = cena **za
   jednoho** cestujícího. Pro skupinu se ptáme za každý typ zvlášť a sčítáme.

Cestující (podle obchodních podmínek CFR): dítě do 10 let platí dětské
jízdné, do 5 let bez vlastního místa zdarma (do ceny se nepočítá); od 10 let
dospělý. Rumunské slevy (Elev, Student, Pensionar, TrenPlus) neuplatňujeme.

Na co si dát pozor:

* **Jen vnitrostátní Rumunsko.** Mezinárodní e-shop
  (bileteinternationale.cfrcalatori.ro) chrání vyhledávání Cloudflare
  Turnstile (``cf-turnstile-response`` u ``/api/booking/journey``) – ten
  neobcházíme. Zahraniční stanice proto končí „stanici nezná“.
* Když server vrátí ``ReCaptchaFailed``, chce captchu – hlásíme blokaci,
  captchu neřešíme.
* Časy jsou rumunské (EET/EEST, o hodinu víc než v Praze).
* Hledá se jen jeden den od zadané hodiny (prodej nejvýš 30 dní dopředu).
* Odpověď na hledání má i 2 MB HTML – ceny zjišťujeme jen pro spoje v okně
  (jen první 3 spoje do 4:15 h od odjezdu, ``price_scope``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import BlockedError, StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider, PRICE_SCOPE_COUNT, price_scope

log = logging.getLogger(__name__)

BASE = "https://bilete.cfrcalatori.ro"
WEB = f"{BASE}/ro-RO/Itineraries"
SEARCH = f"{BASE}/ro-RO/Itineraries/GetItineraries"
PRICE = f"{BASE}/api/ro-RO/Itineraries/Price"
STATIONS_TTL = 14 * 24 * 3600
PRICE_TTL = 30 * 60
MAX_PRICED = PRICE_SCOPE_COUNT
ADULT, CHILD = "102", "101"
#: Nouzový kurz, když ČNB kurz RON chybí (nouzový lístek RON nezná).
RON_CZK_FALLBACK = Decimal("4.95")
MONTHS = {"ian": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "iun": 6, "iul": 7, "aug": 8,
          "sep": 9, "sept": 9, "oct": 10, "noi": 11, "nov": 11, "dec": 12}
#: Česká / cizí jména měst -> jméno u CFR.
ALIASES = {"bukurest": "București", "bukuresti": "București", "bucharest": "București",
           "bucuresti": "București", "kluz": "Cluj Napoca", "cluj": "Cluj Napoca",
           "kolozsvar": "Cluj Napoca", "temesvar": "Timișoara Nord",
           "timisoara": "Timișoara Nord", "nagyvarad": "Oradea", "brasso": "Brașov",
           "kronstadt": "Brașov", "hermannstadt": "Sibiu", "jasy": "Iași", "konstanca": "Constanța"}

_ASCII = str.maketrans("ăâîșțşţĂÂÎȘȚŞŢ", "aaiststAAISTST")
_RE_STATION = re.compile(r'col-9 color-blue text-1-1rem">\s*([^<]+?)\s*<')
_RE_TIME = re.compile(r'div-itineraries-departure-arrival">\s*(Ple|Sos)\s+(\d{1,2})\s+([a-zăâî]+)\.?\s+(\d{1,2}):(\d{2})')
_RE_TRAIN = re.compile(r'span-train-category-[\w-]+">\s*([^<]+?)\s*</span>\s*<a href="/ro-RO/Tren/(\w+)\?')
_RE_STOP = re.compile(r'<div class="pl-2 mt-1 mt-sm-0 mb-1 mb-sm-0 pl-sm-3">\s*([^<]+?)\s*</div>')
_RE_BUS = re.compile(r'Transb\.auto\s*(\d{1,2}\.\d{1,2}\s*-\s*\d{1,2}\.\d{1,2})')


def _bucharest_tz() -> dt.tzinfo | None:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Bucharest")
    except Exception:  # noqa: BLE001 - Windows bez tzdata
        return None


RO_TZ = _bucharest_tz()


@PROVIDERS.register()
class CfrProvider(HttpProvider):
    name = "CFR"
    label = "CFR Călători – Rumunské dráhy (jen vnitrostátní spoje po Rumunsku)"
    multi_passenger = True
    currency = "RON"
    countries = ("RO",)
    international_only = True
    referer = WEB
    min_interval = 0.35

    # ---- stanice -------------------------------------------------------------------
    async def stations(self) -> list[str]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        page = await self.client.get_text(WEB, referer=BASE + "/")
        names = parse_stations(page)
        if not names:
            raise TransientProviderError(self.name, "seznam stanic se nepodařilo načíst")
        self.cache.set(self.name, "stations", names, ttl=STATIONS_TTL)
        return names

    # ---- hledání -------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if "RO" not in known and known != [None, None]:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Rumunsku")
        names = await self.stations()
        origin, destination = pick_station(query.origin, names), pick_station(query.destination, names)
        for place, found in ((query.origin, origin), (query.destination, destination)):
            if found is None:
                raise StationNotFoundError(
                    self.name, f"stanici {place!r} CFR online nezná (prodává jen vnitrostátní spoje po "
                               "Rumunsku – mezinárodní e-shop chrání Cloudflare Turnstile)")
        local = to_romania(query.departure)
        date = local.strftime("%d.%m.%Y")
        url = f"{BASE}/ro-RO/Rute-trenuri/{slug(origin)}/{slug(destination)}"
        page = await self.client.get_text(url, referer=WEB, params={
            "DepartureDate": date, "TimeSelectionId": 0, "MinutesInDay": local.hour * 60,
            "OrderingTypeId": 0, "ConnectionsTypeId": 1, "BetweenTrainsMinimumMinutes": 15})
        form = parse_hidden_form(page, "form-search")
        if not form.get("ConfirmationKey"):
            raise TransientProviderError(self.name, "stránka hledání nemá formulář (změna webu?)")
        response = await self.client.request("POST", SEARCH, data=form, referer=url, expect_json=False,
                                             headers={"X-Requested-With": "XMLHttpRequest"})
        body = response.text
        if body.strip() == "ReCaptchaFailed":
            raise BlockedError(self.name, "CFR chce ověření reCAPTCHA – zkuste to později")
        if body.strip() == "ServiceTemporarilyUnavailable":
            raise TransientProviderError(self.name, "služba CFR je dočasně nedostupná")
        journeys = []
        for item in parse_itineraries(body, local.date()):
            journey = self.to_journey(item, url, date)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        journeys = price_scope(journeys, query, MAX_PRICED)     # cena = dotaz na každý spoj
        if journeys:
            self.publish(journeys)
            await self._prices(journeys, query, url)
        return journeys

    def to_journey(self, item: dict[str, Any], url: str, date: str) -> Journey | None:
        if not item["legs"]:
            return None
        journey = Journey(provider=self.name, legs=item["legs"], fare_status=FareStatus.UNKNOWN,
                          journey_id=f"cfr:{date}:{'-'.join(l.line for l in item['legs'])}",
                          deep_link=f"{url}?DepartureDate={date}",
                          raw={"form": item["form"], "services": item["services"]})
        for note in item["notes"]:
            journey.notes.append(f"CFR: {note}")
        if not item["form"].get("TransactionString"):
            journey.fare_status = FareStatus.NOT_BOOKABLE
        return journey

    async def _prices(self, journeys: list[Journey], query: SearchQuery, referer: str) -> None:
        counts: dict[str, int] = {}
        for passenger in query.passengers:
            kind = fare_type(passenger.age)
            if kind is not None:
                counts[kind] = counts.get(kind, 0) + 1
        if not counts:                       # samé malé děti – jedou zdarma
            counts = {ADULT: 0}

        async def one(journey: Journey) -> None:
            form = journey.raw.get("form") or {}
            if not form.get("TransactionString"):
                return
            total = Decimal(0)
            parts = []
            for kind, count in counts.items():
                body = {**form, "TicketFareTypeId": kind}
                try:
                    response = await self.client.request("POST", PRICE, data=body, referer=referer,
                                                         expect_json=False,
                                                         headers={"X-Requested-With": "XMLHttpRequest"})
                except Exception as exc:  # noqa: BLE001 - jízdní řád zůstane
                    log.debug("CFR: cena %s: %s", journey.journey_id, exc)
                    self.note("ceny některých spojů se nepodařilo zjistit")
                    return
                price = parse_price(response.text)
                if price is None:
                    return
                total += price * count
                parts.append(f"{count}× {'dítě' if kind == CHILD else 'dospělý'} {price} lei")
            fare = self.make_fare(total, "RON", label="CFR Călători",
                                  tariff="2. třída; " + ", ".join(parts), deep_link=journey.deep_link)
            if fare.amount_czk is None:
                fare.amount_czk = (total * RON_CZK_FALLBACK).quantize(Decimal("0.01"))
            journey.fares = [fare]
            journey.fare_status = FareStatus.PRICED
            self.publish(journeys)

        await asyncio.gather(*(one(j) for j in journeys))


# ---- parsování -------------------------------------------------------------------
def parse_stations(page: str) -> list[str]:
    start = page.find("availableStations = [")
    if start < 0:
        return []
    block = page[start:page.find("];", start)]
    return [html.unescape(n) for n in re.findall(r'name: "([^"]+)"', block)]


def pick_station(place: str, names: list[str]) -> str | None:
    """Jméno stanice CFR: přesná shoda bez diakritiky, skupina města
    „(toate stațiile)“, „X Nord“, jinak nejpodobnější jméno."""
    wanted = normalize(place)
    if not wanted:
        return None
    if wanted in ALIASES:
        wanted = normalize(ALIASES[wanted])
    by_norm = {normalize(n): n for n in names}
    if wanted in by_norm:
        group = by_norm.get(f"{wanted} toate statiile")
        return group or by_norm[wanted]
    for suffix in (" toate statiile", " nord"):
        if wanted + suffix in by_norm:
            return by_norm[wanted + suffix]
    idx = best_match(place, names, threshold=0.85)
    return names[idx] if idx is not None else None


def slug(name: str) -> str:
    """Jméno do URL jako na webu: bez diakritiky, mezery -> pomlčky."""
    return name.replace(" ", "-").translate(_ASCII)


def parse_hidden_form(page: str, form_id: str) -> dict[str, str]:
    start = page.find(f'id="{form_id}"')
    if start < 0:
        return {}
    block = page[start:page.find("</form>", start)]
    return {html.unescape(k): html.unescape(v)
            for k, v in re.findall(r'name="([^"]+)" type="hidden" value="([^"]*)"', block)}


def parse_itineraries(body: str, date: dt.date) -> list[dict[str, Any]]:
    """Spoje z odpovědi ``GetItineraries``."""
    marks = [(m.start(), m.group(1)) for m in re.finditer(r'id="li-itinerary-(\d+)"', body)]
    items = []
    for i, (pos, number) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        chunk = body[pos:end]
        details_at = chunk.find(f'id="div-itinerary-{number}-details"')
        fares_at = chunk.find(f'id="div-itinerary-{number}-fares"')
        details = chunk[details_at:fares_at if fares_at > details_at else len(chunk)]
        legs, notes = parse_legs(details, date)
        form_at = chunk.find(f'id="form-itinerariy-{number}"')
        form, services = {}, []
        if form_at >= 0:
            block = chunk[form_at:chunk.find("</form>", form_at)]
            form, services = parse_price_form(block)
        items.append({"legs": legs, "notes": notes, "form": form, "services": services})
    return items


def parse_legs(details: str, date: dt.date) -> tuple[list[Leg], list[str]]:
    stations = [(m.start(), html.unescape(m.group(1)).strip()) for m in _RE_STATION.finditer(details)]
    legs, notes = [], []
    for k in range(len(stations) - 1):
        part = details[stations[k][0]:stations[k + 1][0]]
        times = {m.group(1): m for m in _RE_TIME.finditer(part)}
        if "Ple" not in times or "Sos" not in times:
            continue
        dep, arr = _when(times["Ple"], date), _when(times["Sos"], date)
        train = _RE_TRAIN.search(part)
        line = f"{train.group(1)} {train.group(2)}" if train else "vlak"
        origin, destination = stations[k][1], stations[k + 1][1]
        middle = [Stop(name=html.unescape(s).strip()) for s in _RE_STOP.findall(part)]
        for period in _RE_BUS.findall(part):
            if _in_period(period, dep.date()):
                notes.append(f"{line}: část trasy náhradní autobusovou dopravou ({period.strip()})")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.TRAIN, carrier="CFR Călători", line=line,
                        reservation_required=True,
                        stops=[Stop(name=origin, departure=dep), *middle, Stop(name=destination, arrival=arr)]))
    return legs, notes


def parse_price_form(block: str) -> tuple[dict[str, str], list[list[str]]]:
    """Formulář ceny: výchozí (vybrané) služby každého vlaku + TransactionString."""
    form: dict[str, str] = {}
    services = []
    for name, options in re.findall(r'<select[^>]*name="(TrainServiceKeys\[\d+\])"[^>]*>(.*?)</select>', block, re.S):
        opts = re.findall(r'<option( selected="selected")? value="([^"]*)">([^<]*)</option>', options)
        if not opts:
            continue
        chosen = next((o for o in opts if o[0]), opts[0])
        form[name] = html.unescape(chosen[1])
        services.append([html.unescape(o[2]) for o in opts])
    for key in ("TransactionString", "__RequestVerificationToken"):
        m = re.search(rf'name="{re.escape(key)}"[^>]*value="([^"]*)"', block)
        if m:
            form[key] = html.unescape(m.group(1))
    return form, services


def parse_price(text: str) -> Decimal | None:
    """„<strong>75,0 lei</strong>“ -> 75.0 (součet, kdyby bylo víc jízdenek)."""
    values = re.findall(r"<strong>\s*([\d.\s]+,?\d*)\s*lei\s*</strong>", text)
    if not values:
        return None
    return sum((Decimal(v.replace(".", "").replace(" ", "").replace(",", ".")) for v in values), Decimal(0))


def fare_type(age: int) -> str | None:
    """Do 5 let zdarma (bez místa), do 10 let dítě, jinak dospělý."""
    if age < 5:
        return None
    return CHILD if age < 10 else ADULT


def _in_period(period: str, day: dt.date) -> bool:
    """„21.09-02.11“ – platí výluka v den jízdy? (Přes Nový rok taky.)"""
    try:
        (d1, m1), (d2, m2) = [map(int, part.strip().split(".")) for part in period.split("-")]
    except ValueError:
        return True
    start, end, current = (m1, d1), (m2, d2), (day.month, day.day)
    return start <= current <= end if start <= end else current >= start or current <= end


def to_romania(when: dt.datetime) -> dt.datetime:
    if RO_TZ is not None:
        return when.astimezone(RO_TZ)
    return when.astimezone(DEFAULT_TZ) + dt.timedelta(hours=1)


def _when(match: re.Match, date: dt.date) -> dt.datetime:
    day, month = int(match.group(2)), MONTHS.get(match.group(3).rstrip("."), date.month)
    year = date.year + (1 if month < date.month else 0)
    naive = dt.datetime(year, month, day, int(match.group(4)), int(match.group(5)))
    if RO_TZ is not None:
        return naive.replace(tzinfo=RO_TZ)
    # Rumunsko má stejná pravidla letního času jako Praha, jen o hodinu napřed.
    return (naive - dt.timedelta(hours=1)).replace(tzinfo=DEFAULT_TZ)
