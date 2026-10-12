"""Srbijavoz – Srbské dráhy (e-shop eKarta, webapi1.srbvoz.rs).

E-shop „eKarta“ (AngularJS, ``webapi1.srbvoz.rs/eKarta/app/``) volá
veřejné JSON API na stejném serveru, bez přihlášení:

* ``GET api/stanica/StaniceGetAll_MPS`` – stanice (``sifra`` 16052 =
  Beograd Centar–Prokop, ``naziv`` latinkou bez háčků, ``medjunarodna``);
* ``GET api/listavozova/ListaVozova_Web?stanicaod&stanicado&datum=2026-10-15
  &brojputnika=n&razred=2`` – vlaky dne **i s cenou pro n cestujících**:
  ``cenau`` = celkem v dinárech, z toho ``cenao`` jízdné a ``cenad``
  příplatek (místenka, IC); ``etTrasaVoza`` = zastávky s časy,
  ``nazivvoz`` druh vlaku (Re, IR, Soko…), ``brvoz`` číslo.

Cestující: děti do 4 let zdarma, děti 4–15 let mají slevu 50 % z jízdného
(povlastice „DETE“, ``procenat`` 50); příplatek platí celý. Senioři
(„PENZIONER“) potřebují srbský průkaz – počítáme je jako dospělé.

Na co si dát pozor:

* E-shop prodává jen některé vlaky – Bělehrad → Niš ano, rychlovlaky
  Soko Bělehrad → Novi Sad v seznamu nejsou (prodává je jiný systém); pak
  vrací prázdný seznam.
* Jen přímé vlaky – přestupní spojení eKarta nevrací.
* Datum je ve tvaru „RRRR-M-D“ (bez úvodních nul), jako to skládá web.

Ptáme se jen na cesty po Srbsku.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, Passenger, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://webapi1.srbvoz.rs/eKarta/"
WEB = API + "app/"
STATIONS_TTL = 7 * 24 * 3600
#: Česká jména → stanice eKarty.
ALIASES = {"belehrad": "Beograd Centar-PROKOP", "beograd": "Beograd Centar-PROKOP",
           "belgrade": "Beograd Centar-PROKOP", "nis": "Nis", "novi sad": "Novi Sad", "subotica": "Subotica"}
CHILD_FREE, CHILD_MAX = 4, 15


@PROVIDERS.register()
class SrbijavozProvider(HttpProvider):
    name = "Srbijavoz"
    label = "Srbijavoz – Srbské dráhy (jen po Srbsku, vlaky prodávané v eKartě)"
    multi_passenger = True
    currency = "RSD"
    countries = ("RS",)
    international_only = True
    referer = WEB
    min_interval = 0.4

    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        data = await self.client.get_json(f"{API}api/stanica/StaniceGetAll_MPS", referer=WEB)
        items = [{"id": int(s["sifra"]), "name": str(s.get("naziv") or "")} for s in data or []
                 if s.get("sifra") and not s.get("medjunarodna")]
        self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, Any]:
        found = pick_station(place, await self.stations())
        if found is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} Srbijavoz nezná")
        return found

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if any(c not in (None, "RS") for c in known):
            raise StationNotFoundError(self.name, "Srbijavoz – jen cesty po Srbsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        day = query.departure.astimezone(DEFAULT_TZ).date()
        paying = max(1, sum(1 for p in query.passengers if p.age >= CHILD_FREE))
        data = await self.client.get_json(f"{API}api/listavozova/ListaVozova_Web", params={
            "stanicaod": origin["id"], "stanicado": destination["id"],
            "datum": f"{day.year}-{day.month}-{day.day}", "brojputnika": paying, "razred": 2}, referer=WEB)
        journeys = []
        for train in data or []:
            journey = self.to_journey(train, day, query, paying)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys

    def to_journey(self, train: dict[str, Any], day: dt.date, query: SearchQuery, paying: int) -> Journey | None:
        leg = parse_leg(train, day)
        if leg is None:
            return None
        journey = Journey(provider=self.name, legs=[leg], fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        total, tariff = group_price(train, query.passengers, paying)
        if total:
            journey.fares = [self.make_fare(total, "RSD", label="Srbijavoz", tariff=tariff)]
            journey.fare_status = FareStatus.PRICED
        return journey


def pick_station(place: str, stations: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = normalize(place)
    target = normalize(ALIASES.get(wanted, place))
    for s in stations:
        if normalize(s["name"]) == target:
            return s
    starts = [s for s in stations if normalize(s["name"]).startswith(target)]
    if starts:
        return starts[0]
    idx = best_match(place, [s["name"] for s in stations])
    return stations[idx] if idx is not None else None


def group_price(train: dict[str, Any], passengers: tuple[Passenger, ...], paying: int) -> tuple[Decimal, str]:
    """Cena skupiny: API dá součet za ``paying`` dospělých; dětem odečteme 50 % jízdného."""
    total = Decimal(str(train.get("cenau") or 0))
    fare = Decimal(str(train.get("cenao") or 0))
    children = sum(1 for p in passengers if CHILD_FREE <= p.age <= CHILD_MAX)
    parts = ["2. třída"]
    if children and paying:
        total -= fare / paying / 2 * children
        parts.append("děti 4–15 let −50 % jízdného")
    if any(p.age < CHILD_FREE for p in passengers):
        parts.append("děti do 4 let zdarma")
    if train.get("cenad"):
        parts.append("vč. příplatku")
    return total.quantize(Decimal("1")), ", ".join(parts)


def _at(day: dt.date, hhmm: str, after: dt.datetime | None) -> dt.datetime | None:
    try:
        hour, minute = (int(x) for x in str(hhmm).split(":")[:2])
    except ValueError:
        return None
    value = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=DEFAULT_TZ)
    while after is not None and value < after:
        value += dt.timedelta(days=1)
    return value


def parse_leg(train: dict[str, Any], day: dt.date) -> Leg | None:
    dep = _at(day, train.get("vremep"), None)
    if dep is None:
        return None
    arr = _at(day, train.get("vremed"), dep)
    stops: list[Stop] = []
    route = sorted(train.get("etTrasaVoza") or [], key=lambda s: s.get("rb") or 0)
    names = [normalize(str(s.get("naziV_STANICE") or "")) for s in route]
    start = names.index(normalize(train.get("nazivod") or "")) if normalize(train.get("nazivod") or "") in names else 0
    end = names.index(normalize(train.get("nazivdo") or "")) if normalize(train.get("nazivdo") or "") in names \
        else len(route) - 1
    prev = dep - dt.timedelta(minutes=1)
    for i, s in enumerate(route[start:end + 1]):
        arrival = None if i == 0 else _at(day, s.get("vremE_DOLASKA"), prev)
        departure = None if start + i == end else _at(day, s.get("vremE_POLASKA"), arrival or prev)
        prev = departure or arrival or prev
        stops.append(Stop(name=str(s.get("naziV_STANICE") or ""), arrival=arrival, departure=departure))
    if len(stops) < 2:
        stops = [Stop(name=str(train.get("nazivod") or ""), departure=dep),
                 Stop(name=str(train.get("nazivdo") or ""), arrival=arr)]
    if arr is None:
        return None
    return Leg(origin=str(train.get("nazivod") or stops[0].name), destination=str(train.get("nazivdo") or stops[-1].name),
               departure=dep, arrival=arr, mode=Mode.TRAIN, carrier="Srbijavoz",
               line=f"{train.get('nazivvoz') or ''} {train.get('brvoz') or ''}".strip(), stops=stops)
