"""DSB – Dánské dráhy (e-shop www.dsb.dk/indland).

Nový e-shop DSB (Next.js aplikace na ``/indland``, anglicky
``/en/domestic``) volá vlastní API na stejné doméně – obyčejné
``GET /api/domestic/v1/<procedura>/?input=<JSON>``. Odpovědi mají tvar
``{"value": …}`` nebo ``{"error": {…}}``. Volání:

* ``location`` ``{"searchText": "Aarhus"}`` – našeptávač míst: stanice
  (``type: STOP``), adresy a body zájmu. Stanice mají ``id`` ve tvaru
  HAFAS (``A=1@O=Aarhus H@X=…@Y=…@L=8600053@``) a ``stopId`` – u vlakových
  stanic sedmimístné číslo UIC (86… Dánsko, 74… Švédsko, 80… Německo),
  u autobusových zastávek interní delší číslo;
* ``search`` ``{"offerCollection": {…}, "tripSearch": {…}}`` – spoje od
  zadaného času (``departureTime`` v UTC) **i s cenami** pro celou skupinu.
  Cestující se zadávají anonymně (``anonymousPassengerSpecifications``)
  typem ``TT_CHILD_0_11``, ``TT_CHILD_12_15``, ``TT_YOUNG`` (16–25),
  ``TT_ADULT`` (26–66) a ``TT_SENIOR`` (67+). Ke každému spoji přijdou
  nabídky (``offers``) podle ``requestedOfferTypes``: ``OT_SINGLE``
  (Standardbillet – platí celý den na zvolené trase), ``OT_ORANGE``
  (levná jízdenka jen na vybraný spoj, nevratná) a ``OT_ORANGEFRI``
  (Orange s možností vrácení). ``offerSummary.minimalPrice`` je cena
  **za celou skupinu** v haléřích (``amount`` / 10^``scale``), stav
  nabídky v ``status.category`` (``SELLABLE`` / ``NOT_SELLABLE``) a důvod
  v ``status.code`` (``SOLD_OUT``, ``UNSUPPORTED_MOT``,
  ``UNSUPPORTED_FARESET`` …);
* ``next`` ``{"offerCollection": …, "tripScroll": {"searchContext":
  {"value": …}}}`` – další stránka spojů (``searchContext`` z předchozí
  odpovědi). Jedno hledání vrátí nejvýš ~6 spojů (``tripsAfter`` má
  v API maximum 6), proto stránkujeme až do konce časového okna dotazu.

Ověřené ceny (říjen 2026, København H → Odense St., Standardbillet):
dospělý 339 DKK, mládež 16–25 a senior 67+ 237 DKK, dítě 169 DKK.
Pravidlo DSB „každý dospělý (i mládež a senior) veze zdarma dvě děti do
11 let, platící dítě 12–15 let jedno“ uplatňuje API samo – 1 dospělý +
3 děti do 11 let = 339 + 169 = 508 DKK. Věk proto jen převedeme na typ
cestujícího a cenu skupiny bereme tak, jak přijde.

Na co si dát pozor:

* **Hlavička Origin.** Bez ``Origin: https://www.dsb.dk`` API odpoví
  403 ``{"error": "Origin header required"}`` (posílá ji HttpClient
  podle ``Referer``).
* **Token ``x-api-proof``.** Web při odpovědi 401 s hlavičkou
  ``x-api-proof-required: 1`` stáhne krátkodobý token z
  ``GET /api/domestic/v1/proof/`` (platí ~2 minuty) a pošle ho
  v hlavičce ``x-api-proof``. Sdílený HttpClient by 401 vyhodnotil jako
  blokaci, proto token stahujeme rovnou předem – stejný obyčejný GET,
  jaký dělá web, bez výzvy a bez captchy.
* **Jen Dánsko.** E-shop prodává jen vnitrostátní jízdenky (vč. cesty na
  Bornholm přes Ystad). Malmö zná, ale vlaky Øresundståg přes most nabízí
  bez ceny (``UNSUPPORTED_FARESET``) – vracíme jen jízdní řád. Německé
  stanice nezná vůbec („Hamburg“ najde jen autobusové nádraží ZOB bez
  spojů), na takové cesty se neptáme.
* Názvy porovnáváme přes ``fold`` (``ø/æ/å`` → ``o/ae/a``, stejně jako
  ``normalize()``).
* Našeptávač je velkorysý (na „Lisboa“ vrátí „Lisboavej“, na „Kodaň“
  ulici „Kodansvej“) – bereme jen zastávky, jejichž název začíná hledaným
  slovem, a z nich přednostně vlakovou stanici (UIC 86…/74…).
* Jeden úsek může mít víc ``transports`` (vlak během jízdy změní číslo,
  „IC 424“ → „IC 2324“) – spojíme je do jednoho úseku.
* Odpověď ``search`` je velká (desítky kB na spoj, s rezervacemi
  a doplňky až ~1 MB na 3 spoje) – žádáme jen ``ADMISSION`` a tři typy
  jízdenek, což ji zmenší asi pětkrát.

Ptáme se jen tehdy, když aspoň jeden konec cesty leží v Dánsku a druhý
není známý jako stát mimo Dánsko a Švédsko.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal
from typing import Any

from .. import geo
from ..errors import StationNotFoundError, TransientProviderError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..normalize import normalize
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

WEB = "https://www.dsb.dk"
API = WEB + "/api/domestic/v1"
SHOP = WEB + "/indland"
RESULTS = SHOP + "/soegeresultat/"
PLACE_TTL = 30 * 24 * 3600
#: Token x-api-proof platí ~120 s – necháme si rezervu.
PROOF_TTL = 90
#: Nejvýš kolik stránek ``next`` po prvním hledání (každá ~3–6 spojů).
MAX_PAGES = 6

MEANS_OF_TRANSPORT = ["INTERCITY_TRAIN", "INTERCITY_EXPRESS_TRAIN", "REGIONAL_TRAIN", "OTHER_TRAINS",
                      "S_TRAIN", "STANDARD_BUS", "EXPRESS_BUS", "OTHER_BUSSES", "SPECIAL_BUS", "FERRY",
                      "METRO", "LIGHT_RAIL"]
#: Typ nabídky → (název, popis tarifu, flexibilní).
OFFER_TYPES = {
    "OT_SINGLE": ("DSB Standard", "Standardbillet – celý den na zvolené trase", True),
    "OT_ORANGE": ("DSB Orange", "Orange – jen vybraný spoj, nevratná", False),
    "OT_ORANGEFRI": ("DSB Orange Fri", "Orange Fri – jen vybraný spoj, lze vrátit", True),
}
#: Česká, anglická a německá jména dánských měst → název v e-shopu DSB.
EXONYMS = {"kodan": "København H", "copenhagen": "København H", "kopenhagen": "København H",
           "kobenhavn": "København H", "arhus": "Aarhus", "elsinore": "Helsingør",
           "helsingor": "Helsingør", "flensborg": "Flensburg"}
#: Klíčová slova (po ``fold``) pro dánská místa – geo je zatím nezná všechna.
DK_WORDS = ("kobenhavn", "kodan", "copenhagen", "kopenhagen", "aarhus", "arhus", "odense", "aalborg",
            "esbjerg", "roskilde", "padborg", "kolding", "fredericia", "vejle", "horsens", "randers",
            "helsingor", "naestved", "svendborg", "nyborg", "slagelse", "holbaek", "hillerod", "herning",
            "silkeborg", "viborg", "sonderborg", "rodby", "frederikshavn", "hjorring", "skagen", "struer",
            "holstebro", "ringsted", "kastrup", "billund", "ronne", "tonder", "aabenraa", "middelfart",
            "hobro", "grenaa", "kalundborg", "nykobing falster", "korsor", "koge", "hoje taastrup",
            "taastrup", "orestad", "skanderborg", "vojens", "tinglev", "bramming", "ribe", "thisted",
            "skive", "lunderskov", "kalundborg", "cph lufthavn", "dansko", "danmark", "denmark")
OPERATORS = {"DSB": "DSB", "SKAANETRAFIKKEN": "Skånetrafiken", "ARRIVA": "Arriva",
             "MOLS_LINIEN": "Molslinjen", "SYDTRAFIK": "Sydtrafik", "MIDTTRAFIK": "Midttrafik",
             "MOVIA": "Movia", "NORDJYLLANDS_TRAFIKSELSKAB": "NT", "METRO": "Metro",
             "LOKALTOG": "Lokaltog", "NORDJYSKE_JERNBANER": "Nordjyske Jernbaner", "SJ": "SJ",
             "GO_COLLECTIVE": "GoCollective"}


@PROVIDERS.register()
class DsbProvider(HttpProvider):
    name = "DSB"
    label = "DSB – Dánské dráhy (jen cesty po Dánsku; ceny pro celou skupinu)"
    multi_passenger = True
    provides_stops = True
    currency = "DKK"
    countries = ("DK", "SE")
    international_only = True
    referer = SHOP
    min_interval = 0.3

    # ---- přístup k API --------------------------------------------------------------
    async def proof(self) -> str | None:
        cached = self.cache.get(self.name, "proof")
        if cached:
            return cached
        try:
            data = await self.client.get_json(f"{API}/proof/", referer=SHOP)
        except Exception as exc:  # noqa: BLE001 - bez tokenu to zatím jde také
            log.debug("DSB: token x-api-proof nelze získat: %s", exc)
            return None
        token = (data or {}).get("token")
        if token:
            self.cache.set(self.name, "proof", token, ttl=PROOF_TTL)
        return token

    async def _call(self, procedure: str, payload: dict[str, Any], *, proof: bool = False) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "*/*"}
        if proof:
            token = await self.proof()
            if token:
                headers["x-api-proof"] = token
        data = await self.client.get_json(
            f"{API}/{procedure}/",
            params={"input": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            referer=RESULTS, headers=headers)
        if not isinstance(data, dict) or "value" not in data:
            detail = (data or {}).get("error") if isinstance(data, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("detail") or detail.get("title")
            raise TransientProviderError(self.name, f"DSB API {procedure}: {detail or 'neplatná odpověď'}")
        return data["value"]

    async def resolve(self, place: str) -> dict[str, Any]:
        key = f"place:{fold(place)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        wanted = EXONYMS.get(fold(place), place)
        items = await self._call("location", {"searchText": wanted}) or []
        chosen = pick_location(wanted, items)
        if chosen is None:
            raise StationNotFoundError(self.name, f"stanici {place!r} DSB nezná")
        out = {k: chosen[k] for k in ("name", "type", "id") if k in chosen}
        if chosen.get("stopId") is not None:
            out["stopId"] = chosen["stopId"]
        out.update(transports=[], zones=[])
        self.cache.set(self.name, key, out, ttl=PLACE_TTL)
        return out

    # ---- hledání ----------------------------------------------------------------------
    async def search(self, query: SearchQuery) -> list[Journey]:
        check_route(query.origin, query.destination)
        origin, destination = await self.resolve(query.origin), await self.resolve(query.destination)
        countries = {stop_country(origin), stop_country(destination)}
        if "DK" not in countries or not countries <= {"DK", "SE"}:
            raise StationNotFoundError(self.name, "e-shop DSB prodává jen jízdenky po Dánsku")
        offers = offer_collection(query)
        value = await self._call("search", {"offerCollection": offers, "tripSearch": {
            "strategy": {"departureLocation": origin, "arrivalLocation": destination,
                         "departureTime": utc_iso(query.departure),
                         "searchStrategy": "STRATEGY_DEPARTURE_TIME", "tripsBefore": 0, "tripsAfter": 6},
            "meansOfTransportationClasses": MEANS_OF_TRANSPORT,
            "flags": {"polyline": False, "polylineBeforeAfter": False, "realTimeInformation": True,
                      "turnByTurnNavigation": False}}}, proof=True)
        journeys: dict[tuple, Journey] = {}
        for page in range(MAX_PAGES + 1):
            last = self._collect(value, query, journeys)
            self.publish(sorted(journeys.values(), key=lambda j: j.departure))
            context = (value or {}).get("searchContext")
            if not context or last is None or last >= query.window_end or page == MAX_PAGES:
                break
            try:
                value = await self._call("next", {"offerCollection": offers,
                                                  "tripScroll": {"searchContext": {"value": context}}},
                                         proof=True)
            except TransientProviderError as exc:
                self.note(f"další spoje DSB se nepodařilo načíst: {exc.message}")
                break
        return sorted(journeys.values(), key=lambda j: j.departure)

    def _collect(self, value: Any, query: SearchQuery, out: dict[tuple, Journey]) -> dt.datetime | None:
        """Přidá spoje ze stránky do ``out``; vrátí odjezd posledního spoje stránky."""
        last = None
        for item in (value or {}).get("tripOffers") or []:
            journey = self.to_journey(item, query)
            if journey is None:
                continue
            last = journey.departure if last is None else max(last, journey.departure)
            if query.departure <= journey.departure <= query.window_end:
                out.setdefault(journey.signature(), journey)
        return last

    def to_journey(self, item: dict[str, Any], query: SearchQuery | None = None) -> Journey | None:
        trip = item.get("trip") or {}
        legs = [leg for leg in (to_leg(raw) for raw in trip.get("tripLegs") or []) if leg is not None]
        if not legs:
            return None
        journey = Journey(provider=self.name, legs=legs, fare_status=FareStatus.UNKNOWN, deep_link=SHOP,
                          journey_id=str(trip.get("id") or ""))
        journey.notes.extend(f"Upozornění DSB: {m}" for m in trip_messages(trip))
        fares, reasons = [], set()
        for offer in item.get("offers") or []:
            kind = (offer.get("contents") or {}).get("offerType")
            if kind not in OFFER_TYPES:
                continue
            status = offer.get("status") or {}
            amount = price_of(offer)
            if status.get("category") != "SELLABLE" or amount is None:
                reasons.add(str(status.get("code") or ""))
                continue
            label, tariff, flexible = OFFER_TYPES[kind]
            if query is not None:
                tariff += group_note(query)
            fares.append(self.make_fare(amount, "DKK", label=label, tariff=tariff, flexible=flexible,
                                        deep_link=SHOP))
        if fares:
            journey.fares = sorted(fares, key=lambda f: f.amount)
            journey.fare_status = FareStatus.PRICED
        elif reasons and reasons <= {"SOLD_OUT"}:
            journey.fare_status = FareStatus.SOLD_OUT
        else:
            journey.notes.append("DSB jízdenku na tento spoj neprodává (např. Øresundståg do Švédska) – "
                                 "kupte ji u dopravce.")
        return journey


# ---- pomocné funkce ---------------------------------------------------------------------
def fold(text: str) -> str:
    """``normalize`` s dánskými písmeny: ø → o, æ → ae, å → aa (jinak by z nich byla mezera)."""
    low = (text or "").lower().replace("ø", "o").replace("æ", "ae").replace("å", "aa")
    return normalize(low)


def _bare(text: str) -> str:
    """Název bez přípon stanice („Odense St.“ → „odense“, „Aarhus H“ → „aarhus“)."""
    return re.sub(r"( (st|h|station|c))+$", "", fold(text))


def is_danish(name: str) -> bool:
    n = f" {fold(name)} "
    return any(f" {w} " in n for w in DK_WORDS)


def check_route(origin: str, destination: str) -> None:
    """Bez sítě odmítne cesty, které s Dánskem nesouvisí (nebo vedou mimo DK/SE)."""
    known = ["DK" if is_danish(p) else geo.country_of(p) for p in (origin, destination)]
    if None not in known and "DK" not in known:
        raise StationNotFoundError(DsbProvider.name, "cesta nezačíná ani nekončí v Dánsku")
    if any(c not in (None, "DK", "SE") for c in known):
        raise StationNotFoundError(DsbProvider.name, "e-shop DSB prodává jen jízdenky po Dánsku")


def stop_country(location: dict[str, Any]) -> str:
    """Země podle čísla UIC (86 DK, 74 SE, 80 DE …); místní zastávky bez UIC jsou dánské."""
    sid = str(location.get("stopId") or "")
    if len(sid) == 7 and sid.isdigit():
        return {"86": "DK", "74": "SE", "80": "DE", "76": "NO"}.get(sid[:2], "XX")
    return "DK"


def _is_rail(location: dict[str, Any]) -> bool:
    sid = str(location.get("stopId") or "")
    return len(sid) == 7 and sid[:2] in ("86", "74")


def pick_location(place: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Přesná shoda, jinak zastávka začínající hledaným slovem; vlaková stanice má přednost."""
    stops = [i for i in items if isinstance(i, dict) and i.get("type") == "STOP" and i.get("id")]
    wanted = _bare(place)
    if not wanted or not stops:
        return None
    exact = [s for s in stops if _bare(s.get("name", "")) == wanted or fold(s.get("name", "")) == fold(place)]
    if exact:
        return sorted(exact, key=lambda s: (fold(s.get("name", "")) != fold(place), not _is_rail(s)))[0]
    first = wanted.split(" ")[0]
    similar = [s for s in stops if fold(s.get("name", "")).split(" ")[0] == first]
    if not similar:
        return None
    rail = [s for s in similar if _is_rail(s)]
    return (rail or similar)[0]


def traveler_type(age: int) -> str:
    if age < 12:
        return "TT_CHILD_0_11"
    if age < 16:
        return "TT_CHILD_12_15"
    if age < 26:
        return "TT_YOUNG"
    if age < 67:
        return "TT_ADULT"
    return "TT_SENIOR"


def offer_collection(query: SearchQuery) -> dict[str, Any]:
    passengers = [{"travelerType": traveler_type(p.age), "externalRef": f"passenger_{i}"}
                  for i, p in enumerate(query.passengers, start=1)]
    return {"anonymousPassengerSpecifications": passengers or [
                {"travelerType": "TT_ADULT", "externalRef": "passenger_1"}],
            "offerSearchCriteria": {"requestedOfferParts": ["ADMISSION"],
                                    "requestedOfferTypes": ["OT_ORANGE", "OT_ORANGEFRI", "OT_SINGLE"],
                                    "customerGroups": ["B2C"]}}


def group_note(query: SearchQuery) -> str:
    kids = sum(1 for p in query.passengers if p.age < 12)
    if not kids or len(query.passengers) == kids:
        return ""
    return "; děti do 11 let s dospělým zdarma (max. 2 na dospělého)"


def price_of(offer: dict[str, Any]) -> Decimal | None:
    price = (offer.get("offerSummary") or {}).get("minimalPrice") or {}
    if price.get("amount") is None or (price.get("currency") or "DKK") != "DKK":
        return None
    return Decimal(int(price["amount"])).scaleb(-int(price.get("scale", 2)))


def utc_iso(when: dt.datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=DEFAULT_TZ)
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")


def trip_messages(trip: dict[str, Any]) -> list[str]:
    headers = [m.get("header") for m in trip.get("himMessages") or []]
    for leg in trip.get("tripLegs") or []:
        headers.extend(m.get("header") for m in leg.get("messages") or [])
    return list(dict.fromkeys(h.strip() for h in headers if isinstance(h, str) and h.strip()))[:3]


def _mode(kind: str) -> Mode:
    kind = (kind or "").upper()
    if "BUS" in kind:
        return Mode.BUS
    if "FERRY" in kind:
        return Mode.FERRY
    if "METRO" in kind:
        return Mode.METRO
    if "LIGHT_RAIL" in kind or "TRAM" in kind:
        return Mode.TRAM
    return Mode.TRAIN


def _operator(code: Any) -> str:
    code = str(code or "")
    if not code or code == "UNKNOWN":
        return ""
    return OPERATORS.get(code, code.replace("_", " ").title())


def to_leg(raw: dict[str, Any]) -> Leg | None:
    stops_raw = [s for s in raw.get("stops") or [] if s.get("board", True) or s.get("unboard", True)]
    if len(stops_raw) < 2:
        return None
    first, last = stops_raw[0], stops_raw[-1]
    dep = _time((first.get("plannedTimeAndTrackInfo") or {}).get("departureTime"))
    arr = _time((last.get("plannedTimeAndTrackInfo") or {}).get("arrivalTime"))
    if dep is None or arr is None:
        return None
    stops = []
    for s in stops_raw:
        planned = s.get("plannedTimeAndTrackInfo") or {}
        geo_ = (s.get("location") or {}).get("geoCoordinate") or {}
        stops.append(Stop(name=str((s.get("location") or {}).get("name") or ""),
                          arrival=_time(planned.get("arrivalTime")),
                          departure=_time(planned.get("departureTime")),
                          platform=planned.get("departureTrack") or planned.get("arrivalTrack"),
                          lat=geo_.get("latitude"), lon=geo_.get("longitude")))
    transports = raw.get("transports") or []
    if raw.get("type") != "PUBLIC_TRANSPORT" or not transports:
        mode, line, carrier = Mode.WALK, "", ""
    else:
        mode = _mode(str(transports[0].get("meansOfTransportation") or ""))
        line = " / ".join(dict.fromkeys(str(t.get("name") or "").strip() for t in transports
                                        if t.get("name")))
        carrier = _operator(transports[0].get("operator"))
    return Leg(origin=stops[0].name, destination=stops[-1].name, departure=dep, arrival=arr,
               mode=mode, carrier=carrier, line=line, stops=stops)


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=DEFAULT_TZ)
