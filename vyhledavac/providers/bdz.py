"""BDŽ – Bulharské dráhy (jízdní řád razpisanie.bdz.bg s cenami).

E-shop tickets.bdz.bg ukáže cenu jen přihlášenému uživateli
(``api/Ticket/GetTicket?onlyPrice=true`` bez tokenu vrací 401). Veřejný
jízdní řád **razpisanie.bdz.bg** ale u každého spoje ukazuje ceník
(záložka „Цени“) – to používáme:

* ``GET /bg/{odkud}/{kam}/{DD.MM.RRRR}`` – HTML se spoji celého dne;
  stanice jsou slugy („sofia“, „plovdiv“) ze seznamu ``window.stationsData``
  na stejné stránce. Každá karta spoje má čas „06:10 — 11:07“, dobu jízdy
  a tlačítka „Още информация“ (trasa) a „Цени“ (ceny) s odkazem
  ``data-route-index`` / ``data-price-index`` = klíč do
  ``BDTJSONItemsPriceAPI.trains`` (vlaky spoje: číslo, druh, km, UIC úseků),
  u některých spojů ``data-is-price-key`` („05:25-11:07“);
* ``POST /bg/route/{datum}/{UIC od}/{UIC do}`` s vlaky spoje (JSON, hlavička
  ``X-CSRF-Token`` ze stránky) – HTML trasy: vlaky, všechny zastávky s časy,
  přestupy;
* ``POST /bg/price/…`` (stejné tělo) – ceník spoje: „Редовна тарифа“
  (plné jízdné 2./1. třída), „Дете 7-10 г.“, „Групова тарифа от 3 до 6
  пътника“ (skupina 3–6 osob, za osobu), „Цена за запазено място“ (místenka).

Cenu skupiny počítáme z ceníku: děti do 7 let zdarma, 7–10 let dětské
jízdné, ostatní plné; 3–6 platících dospělých jede za skupinovou cenu, je-li
nižší. U vlaku s povinnou místenkou („Влак със задължителна резервация“)
přičteme místenku každému platícímu, pokud ji ceník uvádí.

Na co si dát pozor:

* Ceny jsou orientační („Цената може да се различава…“) – BDŽ samo píše, že
  přesnou cenu řekne pokladna / e-shop. Spoj proto nese poznámku.
* Cena se zjišťuje dotazem na každý spoj – jen první spoje (``price_scope``:
  3 spoje, nejvýš 4:15 h od odjezdu). Ke každému dva dotazy (trasa a cena).
* Bulharsko platí eurem (od 2026), ceny jsou v EUR.
* Na stránce nejsou latinkou jména stanic, jen slugy – podle nich hledáme.

Ptáme se jen na cesty po Bulharsku.
"""

from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider, price_scope

log = logging.getLogger(__name__)

BASE = "https://razpisanie.bdz.bg"
STATIONS_TTL = 7 * 24 * 3600
#: Česká a anglická jména → slug BDŽ.
ALIASES = {"sofie": "sofia", "sofija": "sofia", "plovdiv": "plovdiv", "varna": "varna", "burgas": "burgas",
           "ruse": "ruse", "rusa": "ruse", "stara zagora": "stara-zagora", "veliko tarnovo": "veliko-tarnovo",
           "gorna orjahovica": "gorna-oryahovitsa"}
CHILD_FREE, CHILD_MAX = 7, 10


@PROVIDERS.register()
class BdzProvider(HttpProvider):
    name = "BDŽ"
    label = "BDŽ – Bulharské dráhy (jen po Bulharsku; ceny z ceníku jízdního řádu)"
    multi_passenger = True
    currency = "EUR"
    countries = ("BG",)
    international_only = True
    hack_legs = False
    referer = BASE + "/bg"
    min_interval = 0.4

    async def stations(self) -> list[dict[str, str]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        page = await self.client.get_text(f"{BASE}/bg", referer=BASE + "/")
        items = parse_stations(page)
        if not items:
            raise TransientProviderError(self.name, "seznam stanic BDŽ se nepodařilo načíst")
        self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, str]:
        found = pick_station(place, await self.stations())
        if found is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} BDŽ nezná")
        return found

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if any(c not in (None, "BG") for c in known):
            raise StationNotFoundError(self.name, "BDŽ – jen cesty po Bulharsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        day = query.departure.astimezone(DEFAULT_TZ).strftime("%d.%m.%Y")
        url = f"{BASE}/bg/{origin['id']}/{destination['id']}/{day}"
        page = await self.client.get_text(url, referer=self.referer)
        token = re.search(r"X-CSRF-Token', '([^']+)'", page)
        data = re.search(r"BDTJSONItemsPriceAPI = JSON\.parse\('(.*?)'\);", page, re.S)
        if not token or not data:
            raise TransientProviderError(self.name, "stránka jízdního řádu BDŽ má jiný tvar")
        api = json.loads(data.group(1).encode().decode("unicode_escape") if "\\u" in data.group(1)
                         else data.group(1))
        route = api.get("route") or {}
        cards = [c for c in parse_cards(page, query.departure.date())
                 if c["departure"] >= query.departure - dt.timedelta(minutes=1)]
        stubs = [Journey(provider=self.name, legs=[Leg(origin=origin["title"], destination=destination["title"],
                                                       departure=c["departure"], arrival=c["arrival"],
                                                       mode=Mode.TRAIN, carrier="BDŽ", line=latin_train(c["train"]))],
                         raw={"card": c}, deep_link=url) for c in cards]
        headers = {"X-CSRF-Token": token.group(1), "Content-Type": "application/json;charset=UTF-8",
                   "X-Requested-With": "XMLHttpRequest"}
        journeys = []
        for stub in price_scope(stubs, query):      # trasa + cena = dva dotazy na každý spoj
            card = stub.raw.pop("card")
            body, suffix = "{}", ""
            if card.get("index"):
                body = json.dumps((api.get("trains") or {}).get(card["index"]) or {}, ensure_ascii=False)
            elif card.get("key"):
                suffix = f"?is_key={card['key']}"
            path = f"{route.get('date') or day}/{route.get('from')}/{route.get('to')}{suffix}"
            try:
                detail = await self._post(f"{BASE}/bg/route/{path}", body, headers)
                prices = await self._post(f"{BASE}/bg/price/{path}", body, headers)
            except TransientProviderError as exc:
                log.debug("BDŽ: detail spoje %s: %s", card, exc.message)
                journeys.append(stub)
                continue
            journeys.append(self.to_journey(stub, card, detail, prices, query, url))
        return journeys

    async def _post(self, url: str, body: str, headers: dict[str, str]) -> str:
        response = await self.client.request("POST", url, data=body.encode(), headers=headers,
                                             referer=self.referer, expect_json=False)
        return response.text

    def to_journey(self, stub: Journey, card: dict[str, Any], detail: str, prices: str,
                   query: SearchQuery, url: str) -> Journey:
        legs = parse_route(detail, stub.departure) or stub.legs
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=url)
        mandatory = "задължителна резервация" in detail
        table = parse_prices(prices)
        total, tariff = group_price(table, query.passengers, mandatory)
        if total is not None:
            journey.fares = [self.make_fare(total, "EUR", label="BDŽ", tariff=tariff)]
            journey.fare_status = FareStatus.PRICED
            journey.notes.append("Cena podle ceníku v jízdním řádu BDŽ – BDŽ ji uvádí jako orientační.")
        return journey


# ---- parsování (bez sítě, kvůli testům) ---------------------------------------------
def parse_stations(page: str) -> list[dict[str, str]]:
    block = page[page.find("window.stationsData"):]
    block = block[:block.find("];") + 1]
    return [{"title": htmlmod.unescape(t), "id": i, "uic": s.split(" ")[0]}
            for t, i, s in re.findall(r"title:\s*'([^']*)',\s*id:\s*'([^']*)',\s*search:\s*'([^']*)'", block)]


def pick_station(place: str, stations: list[dict[str, str]]) -> dict[str, str] | None:
    wanted = normalize(place)
    slug = ALIASES.get(wanted, wanted.replace(" ", "-"))
    by_id = {s["id"]: s for s in stations}
    if slug in by_id:
        return by_id[slug]
    first = slug.split("-")[0]
    if first in by_id:
        return by_id[first]
    idx = best_match(wanted, [s["id"].replace("-", " ") for s in stations])
    return stations[idx] if idx is not None else None


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _at(day: dt.date, hhmm: str, after: dt.datetime | None = None) -> dt.datetime | None:
    m = re.match(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return None
    value = dt.datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=BG_TZ)
    while after is not None and value < after:
        value += dt.timedelta(days=1)
    return value


def parse_cards(page: str, day: dt.date) -> list[dict[str, Any]]:
    """Karty spojů z výsledků: vlak, odjezd, příjezd, klíč pro trasu/cenu."""
    out = []
    for card in page.split('<div class="card uv-card')[1:]:
        times = re.search(r"(\d{1,2}:\d{2})\s*&mdash;\s*(\d{1,2}:\d{2})", card)
        if not times:
            continue
        train = re.search(r'/train-info/[^"]*"[^>]*>([^<]+)</a>', card)
        index = re.search(r'data-price-index="(\d+)"', card)
        key = re.search(r'data-is-price-key="([^"]+)"', card)
        dep = _at(day, times.group(1))
        arr = _at(day, times.group(2), dep)
        out.append({"departure": dep, "arrival": arr, "train": _clean(train.group(1)) if train else "",
                    "index": index.group(1) if index else "", "key": key.group(1) if key else ""})
    return out


def parse_route(detail: str, departure: dt.datetime) -> list[Leg]:
    """Úseky z HTML trasy: vlak (druh + číslo) a zastávky s časy."""
    legs = []
    prev = departure.replace(second=0, microsecond=0) - dt.timedelta(minutes=1)
    for block in detail.split('<p class="d-flex justify-content-between')[1:]:
        name = re.search(r'/train-info/[^"]*"[^>]*>\s*([^<]+?)\s*</a>', block)
        stops = []
        for arr, dep, place in re.findall(
                r'<div class="col-2 text-center text-nowrap">\s*(.*?)\s*</div>\s*'
                r'<div class="col-2 text-center text-nowrap">\s*(.*?)\s*</div>\s*'
                r'<div class="col-8[^"]*">\s*(.*?)\s*</div>', block, re.S):
            arrival = _at(prev.date(), arr, prev) if ":" in arr else None
            departure_at = _at(prev.date(), dep, arrival or prev) if ":" in dep else None
            prev = departure_at or arrival or prev
            stops.append(Stop(name=_clean(place).title(), arrival=arrival, departure=departure_at))
        if len(stops) < 2 or stops[0].departure is None or stops[-1].arrival is None:
            continue
        line = latin_train(_clean(name.group(1))) if name else ""
        legs.append(Leg(origin=stops[0].name, destination=stops[-1].name, departure=stops[0].departure,
                        arrival=stops[-1].arrival, mode=Mode.TRAIN, carrier="BDŽ", line=line, stops=stops))
    return legs


#: Druhy vlaků BDŽ (cyrilice → latinka): rychlík, osobní, příměstský, mezinárodní rychlík.
TRAIN_KINDS = {"МБВ": "MBV", "КПВ": "KPV", "БВ": "BV", "ПВ": "PV"}


def latin_train(name: str) -> str:
    """„БВ 8611“ → „BV 8611“."""
    kind, _, number = name.partition(" ")
    return f"{TRAIN_KINDS.get(kind, kind)} {number}".strip()


def _eur(text: str) -> Decimal | None:
    return Decimal(text.replace(",", ".")) if text else None


def parse_prices(prices: str) -> dict[str, Decimal]:
    """Ceník spoje (2. třída): plné, dítě, skupina, místenka."""
    text = _clean(prices)
    out = {}
    for key, label in (("full", r"Редовна тарифа"), ("child", r"Дете 7-10 г\."),
                       ("group", r"Групова тарифа от 3 до 6 пътника"), ("seat", r"Цена за запазено място")):
        m = re.search(label + r"\s*([\d]+,[\d]{2})\s*€", text)
        if m:
            out[key] = _eur(m.group(1))
    return out


def group_price(table: dict[str, Decimal], passengers: tuple[Passenger, ...],
                mandatory_seat: bool) -> tuple[Decimal | None, str]:
    full = table.get("full")
    if full is None:
        return None, ""
    adults = [p for p in passengers if p.age > CHILD_MAX]
    children = [p for p in passengers if CHILD_FREE <= p.age <= CHILD_MAX]
    if not adults and not children:
        adults = list(passengers[:1])
    adult_price, parts = full, ["2. třída"]
    if 3 <= len(adults) <= 6 and table.get("group") and table["group"] < full:
        adult_price = table["group"]
        parts.append("skupinová cena")
    child_price = table.get("child", full)
    total = adult_price * len(adults) + child_price * len(children)
    if children:
        parts.append("děti 7–10 let −50 %")
    if any(p.age < CHILD_FREE for p in passengers):
        parts.append("děti do 7 let zdarma")
    if mandatory_seat and table.get("seat"):
        total += table["seat"] * (len(adults) + len(children))
        parts.append("+ povinná místenka")
    return total, ", ".join(parts)


try:
    from zoneinfo import ZoneInfo

    BG_TZ = ZoneInfo("Europe/Sofia")
except Exception:  # pragma: no cover
    BG_TZ = DEFAULT_TZ
