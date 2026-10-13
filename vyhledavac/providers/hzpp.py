"""HŽPP – Chorvatské dráhy (HŽ Putnički prijevoz, e-shop prodaja.hzpp.hr).

E-shop prodaja.hzpp.hr je React aplikace nad JSON API
``prodaja.hzpp.hr/hzpp-sales-middleware-web`` (bez přihlášení, bez captchy;
hlavičky jako web: ``X-device-type: UE9SVEFM`` – base64 „PORTAL“,
``deviceType: WEB``, ``Language: hr``):

* ``GET stations?lang=hr&deviceType=WEB`` – všech ~500 chorvatských stanic
  (``{"id": 72480, "name": "Zagreb Glavni kol."}``); stahuje se jednou
  týdně a hledá lokálně;
* ``GET benefits?ticketTypeId=1`` – druhy jízdného; „Redovna cijena“ má ID 11;
* ``GET journeys-with-details/busInfo?from&to&startTime&trainClass=2&benefit=11
  &countForBenefit=N&transportChanges=DIRECT|ALL&clientUICOperatorCode=1178`` –
  spoje celého dne (web posílá ``startTime`` na půlnoc) s cenou
  ``Tickets[].FinalPrice`` v eurocentech **za všech N cestujících**
  (jízdenka + příplatek za vlak IC/brzi, sleva za online nákup a případná
  akce, např. „Vlakom na more“, už odečtené).

Postup: stanice ze seznamu → dva dotazy na spoje (jen přímé ``DIRECT``
a všechny ``ALL``) → sloučení, zahození spojů, které jiný spoj předjede
(odjíždí později a přijede dřív – ``ALL`` vrací i čekání přes noc).

Na co si dát pozor:

* **Jen vnitrostátní jízdenky.** Seznam stanic obsahuje jen Chorvatsko;
  mezinárodní cesty (Zagreb → Ljubljana, Budapešť) e-shop neprodává.
* ``transportChanges=ALL`` umí na dlouhých tratích (Zagreb → Split) po dvou
  minutách skončit chybou 504. Dotaz se proto posílá jen jednou s kratším
  limitem; když selže, zůstanou přímé vlaky a poznámka.
* Dětské jízdné e-shop neprodává (nabízí jen slevy na průkazy). Děti do
  6 let jedou zdarma (do dotazu se nepočítají), děti 6–11 let počítáme jako
  dospělé (u pokladny mají 50% slevu) – cena je pak horní odhad a spoj nese
  poznámku.
* ``Stops`` API nevrací – úseky mají jen nástupní a výstupní stanici.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Chorvatsku
a žádný není známý jako zahraniční.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import ProviderError, StationNotFoundError
from ..http import HostLimiter, HttpClient, RetryPolicy
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import best_match, normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://prodaja.hzpp.hr/hzpp-sales-middleware-web"
WEB = "https://prodaja.hzpp.hr/"
STATIONS_TTL = 7 * 24 * 3600
#: „Redovna cijena“ – plné jízdné bez průkazu.
REGULAR = 11
#: Kód dopravce, který web posílá v ``clientUICOperatorCode``.
OPERATOR = 1178
HEADERS = {"X-device-type": "UE9SVEFM", "deviceType": "WEB", "Language": "hr",
           "Origin": "https://prodaja.hzpp.hr",
           # bez balíčku brotli by httpx odpověď „br“ nerozbalil
           "Accept-Encoding": "gzip, deflate"}
#: Limit pro dotaz s přestupy (server občas vrátí 504 až po 2 minutách).
ALL_TIMEOUT = 40.0
#: Druh vlaku podle ``Category``.
KINDS = {"1": "Pu", "2": "B", "3": "IC", "4": "ICN"}
EXONYMS = {"zahreb": "Zagreb", "zahrab": "Zagreb", "agram": "Zagreb", "rjeka": "Rijeka", "fiume": "Rijeka",
           "spljet": "Split", "osek": "Osijek"}


@PROVIDERS.register()
class HzppProvider(HttpProvider):
    name = "HŽPP"
    label = "HŽPP – Chorvatské dráhy (jen cesty po Chorvatsku)"
    multi_passenger = True
    currency = "EUR"
    countries = ("HR",)
    home = ("HR",)
    international_only = True
    referer = WEB
    min_interval = 0.5

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._slow = None

    @property
    def slow_client(self) -> HttpClient:
        """Klient pro ``ALL`` – jeden pokus s kratším limitem, bez opakování."""
        if self._slow is None:
            self._slow = HttpClient(self.name, language="hr", timeout=ALL_TIMEOUT,
                                    retry=RetryPolicy(attempts=1),
                                    limiter=HostLimiter(min_interval=self.min_interval),
                                    proxy=self.options.get("proxy"))
        return self._slow

    async def aclose(self) -> None:
        if self._slow is not None:
            await self._slow.close()
            self._slow = None
        await super().aclose()

    async def stations(self) -> list[dict[str, Any]]:
        cached = self.cache.get(self.name, "stations")
        if cached:
            return cached
        data = await self.client.get_json(f"{API}/stations", params={"lang": "hr", "deviceType": "WEB"},
                                          referer=WEB, headers=HEADERS)
        items = [{"id": s["id"], "name": s["name"]} for s in (data or {}).get("response") or []
                 if s.get("id") and s.get("name")]
        if items:
            self.cache.set(self.name, "stations", items, ttl=STATIONS_TTL)
        return items

    async def resolve(self, place: str) -> dict[str, Any]:
        found = pick_station(place, await self.stations())
        if found is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} HŽPP nezná (prodává jen po Chorvatsku)")
        return found

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "HR" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí v Chorvatsku")
        if any(c not in (None, "HR") for c in known):
            raise StationNotFoundError(self.name, "e-shop HŽPP prodává jen jízdenky po Chorvatsku")
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        if origin["id"] == destination["id"]:
            raise StationNotFoundError(self.name, "výchozí a cílová stanice jsou stejné")
        count, children = passenger_count(query)
        start = query.departure.astimezone(DEFAULT_TZ)
        end = query.window_end.astimezone(DEFAULT_TZ)
        days = [start.date()] + ([end.date()] if end.date() > start.date() and end.hour >= 4 else [])
        calls = [self._day(origin, destination, day, count, changes)
                 for day in days for changes in ("DIRECT", "ALL")]
        results = await asyncio.gather(*calls, return_exceptions=True)
        kinds = [changes for _ in days for changes in ("DIRECT", "ALL")]
        if all(isinstance(r, BaseException) for r, k in zip(results, kinds) if k == "DIRECT"):
            raise next(r for r in results if isinstance(r, BaseException))
        if any(isinstance(r, BaseException) for r, k in zip(results, kinds) if k == "ALL"):
            self.note("spoje s přestupy server HŽPP nestihl vrátit – zobrazeny jsou jen přímé vlaky")
        journeys: dict[tuple, Journey] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            for item in result:
                journey = self.to_journey(item, children)
                if journey is not None and query.departure <= journey.departure <= query.window_end:
                    journeys.setdefault(journey.signature(), journey)
        return drop_dominated(sorted(journeys.values(), key=lambda j: j.departure))

    async def _day(self, origin: dict[str, Any], destination: dict[str, Any], day: dt.date, count: int,
                   changes: str) -> list[dict[str, Any]]:
        client = self.client if changes == "DIRECT" else self.slow_client
        params = {"lang": "hr", "deviceType": "WEB", "clientUICOperatorCode": OPERATOR,
                  "from": origin["id"], "to": destination["id"], "startTime": f"{day.isoformat()}T00:00:00",
                  "trainClass": 2, "benefit": REGULAR, "countForBenefit": count, "transportChanges": changes}
        try:
            data = await client.get_json(f"{API}/journeys-with-details/busInfo", params=params, referer=WEB,
                                         headers=HEADERS)
        except ProviderError as exc:
            log.debug("HŽPP %s: %s", changes, exc.message)
            raise
        return parse_journeys(data)

    def to_journey(self, item: dict[str, Any], children: int = 0) -> Journey | None:
        legs = parse_legs(item)
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=WEB)
        tickets = item.get("Tickets") or []
        cents = [t.get("FinalPrice") for t in tickets]
        if tickets and all(isinstance(c, (int, float)) for c in cents):
            amount = Decimal(int(sum(cents))) / 100
            names = [str((t.get("Benefit") or {}).get("Description") or "") for t in tickets]
            tariff = ", ".join(dict.fromkeys(n for n in [str(tickets[0].get("Description") or ""), *names] if n))
            journey.fares = [self.make_fare(amount, "EUR", label="HŽPP", tariff=tariff, deep_link=WEB)]
            journey.fare_status = FareStatus.PRICED
            if children:
                journey.notes.append("HŽPP online neprodává dětské jízdné – děti 6–11 let jsou v ceně jako "
                                     "dospělí (u pokladny mají slevu 50 %).")
        return journey


def passenger_count(query: SearchQuery) -> tuple[int, int]:
    """Počet platících (od 6 let) a z nich dětí 6–11 let."""
    paying = sum(1 for p in query.passengers if p.age >= 6)
    children = sum(1 for p in query.passengers if 6 <= p.age < 12)
    return max(paying, 1), children


def pick_station(place: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Přesná shoda, pak hlavní nádraží města („Zagreb Glavni kol.“), pak nejpodobnější."""
    if not items:
        return None
    wanted = normalize(place.split(",")[0])
    wanted = " ".join(EXONYMS.get(w, w) for w in wanted.split(" "))
    wanted = normalize(wanted)
    names = [normalize(str(i["name"])) for i in items]
    for item, name in zip(items, names):
        if name == wanted:
            return item
    same_city = [(i, n) for i, n in zip(items, names) if n.split(" ")[0] == wanted.split(" ")[0]]
    if same_city and " " not in wanted:
        main = [i for i, n in same_city if "glavni" in n.split(" ")]
        return main[0] if main else min((i for i, _ in same_city), key=lambda i: len(i["name"]))
    idx = best_match(wanted, [str(i["name"]) for i in items], threshold=0.5)
    return items[idx] if idx is not None else None


def parse_journeys(data: Any) -> list[dict[str, Any]]:
    body = (data or {}).get("JourneysTypesResponsev2") or {}
    status = body.get("Status") or {}
    if status and not status.get("Success", True):
        log.debug("HŽPP: %s", status.get("Errors"))
    return list(((body.get("JourneyWithTickets") or {}).get("JourneysWithTicketsType")) or [])


def parse_legs(item: dict[str, Any]) -> list[Leg]:
    legs = []
    for raw in ((item.get("TransportationsWithDelay") or {}).get("TransportationWithDelay")) or []:
        dep, arr = _time(raw.get("DepartureDateTime")), _time(raw.get("ArrivalDateTime"))
        if dep is None or arr is None:
            continue
        origin = str((raw.get("Start") or {}).get("Name") or "")
        destination = str((raw.get("Dest") or {}).get("Name") or "")
        number = str(raw.get("Number") or "").strip()
        bus = bool(raw.get("BusNumbers"))
        kind = "Bus" if bus else KINDS.get(str(raw.get("Category") or ""), "")
        line = " ".join(p for p in (kind, number) if p) or "vlak"
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=Mode.BUS if bus else Mode.TRAIN, carrier="HŽPP", line=line,
                        stops=[Stop(name=origin, departure=dep), Stop(name=destination, arrival=arr)]))
    return legs


def drop_dominated(journeys: list[Journey]) -> list[Journey]:
    """Zahodí spoje, které jiný spoj předjede: odjede stejně nebo později, přijede stejně
    nebo dřív, nemá víc přestupů, není dražší a v něčem z toho je opravdu lepší."""
    def beats(b: Journey, a: Journey) -> bool:
        if not (b.departure >= a.departure and b.arrival <= a.arrival and b.transfers <= a.transfers):
            return False
        if a.price is not None and (b.price is None or b.price > a.price):
            return False
        return b.departure > a.departure or b.arrival < a.arrival or b.transfers < a.transfers

    return [a for a in journeys if not any(beats(b, a) for b in journeys if b is not a)]


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
