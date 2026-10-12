"""SBB – Švýcarské dráhy: jízdní řád z transport.opendata.ch (bez cen).

**Ceny SBB z programu zjistit nejde.** Web sbb.ch volá GraphQL API
``graphql.www.sbb.ch`` (dotazy ``Places``, ``Trips``, ``TripPrices``), ale
to je za ochranou AWS CloudFront: z datacentra vrací 403 „Request blocked“
každému požadavku, i skutečnému prohlížeči (stránka sbb.ch tamtéž 403).
Kombo ani Trainline vnitrostátní švýcarské jízdenky nenabízí. Oficiální
cenové API (OJP Fare na opentransportdata.swiss) chce registraci a klíč.

Jízdní řád je ale veřejný: **transport.opendata.ch** (komunitní API nad
daty SBB, bez klíče):

* ``GET /v1/connections?from=…&to=…&date=RRRR-MM-DD&time=HH:MM&limit=6`` –
  spojení s úseky (``sections``), vlakem (``journey.category`` + ``number``,
  ``operator``), nástupištěm a všemi zastávkami (``passList``).

Spoj proto nese poznámku, že cenu je třeba ověřit na sbb.ch. Ve výchozím
výběru dopravce není (jen jízdní řád) – zapněte ho v panelu *Zdroje dat*.
Vlaky přes Švýcarsko do zahraničí s cenou najdou ÖBB, DB a SNCF (Lyria).

Ptáme se jen tehdy, když aspoň jeden konec cesty leží ve Švýcarsku.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from .. import geo
from ..errors import StationNotFoundError
from ..models import DEFAULT_TZ, FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

API = "https://transport.opendata.ch/v1"
#: Česká jména → jak je zná opendata.ch.
ALIASES = {"curych": "Zürich HB", "zurich": "Zürich HB", "zeneva": "Genève", "geneva": "Genève",
           "basilej": "Basel SBB", "basel": "Basel SBB", "bern": "Bern", "luzern": "Luzern", "lucern": "Luzern"}
MODES = {"B": Mode.BUS, "BUS": Mode.BUS, "T": Mode.TRAM, "TRAM": Mode.TRAM, "BAT": Mode.FERRY, "FUN": Mode.TRAIN}


@PROVIDERS.register()
class SbbProvider(HttpProvider):
    name = "SBB"
    label = "SBB – Švýcarsko, jen jízdní řád (ceny SBB blokuje CloudFront)"
    provides_prices = False
    provides_stops = True
    currency = "CHF"
    countries = ("CH",)
    international_only = True
    hack_legs = False
    referer = None
    min_interval = 0.4

    async def search(self, query: SearchQuery) -> list[Journey]:
        known = [geo.country_of(query.origin), geo.country_of(query.destination)]
        if None not in known and "CH" not in known:
            raise StationNotFoundError(self.name, "cesta nezačíná ani nekončí ve Švýcarsku")
        from ..normalize import normalize

        local = query.departure.astimezone(DEFAULT_TZ)
        data = await self.client.get_json(f"{API}/connections", params={
            "from": ALIASES.get(normalize(query.origin), query.origin),
            "to": ALIASES.get(normalize(query.destination), query.destination),
            "date": local.date().isoformat(), "time": local.strftime("%H:%M"), "limit": 6})
        connections = (data or {}).get("connections") or []
        if not connections:
            raise StationNotFoundError(self.name, "spojení nenalezeno")
        journeys = []
        for conn in connections:
            journey = to_journey(conn)
            if journey is not None and query.departure <= journey.departure <= query.window_end:
                journeys.append(journey)
        return journeys


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value))
    except ValueError:
        return None


def to_journey(conn: dict[str, Any]) -> Journey | None:
    legs = []
    for section in conn.get("sections") or []:
        journey = section.get("journey")
        if not journey:
            continue            # pěší přesun
        dep, arr = _time((section.get("departure") or {}).get("departure")), \
            _time((section.get("arrival") or {}).get("arrival"))
        if dep is None or arr is None:
            continue
        stops = []
        for s in journey.get("passList") or []:
            name = str((s.get("station") or {}).get("name") or "")
            if name:
                stops.append(Stop(name=name, arrival=_time(s.get("arrival")), departure=_time(s.get("departure")),
                                  platform=s.get("platform") or None))
        origin = str((section["departure"].get("station") or {}).get("name") or "")
        destination = str((section["arrival"].get("station") or {}).get("name") or "")
        category = str(journey.get("category") or "")
        legs.append(Leg(origin=origin, destination=destination, departure=dep, arrival=arr,
                        mode=MODES.get(category.upper(), Mode.TRAIN), carrier=str(journey.get("operator") or "SBB"),
                        line=f"{category} {journey.get('number') or ''}".strip(), stops=stops))
    if not legs:
        return None
    result = Journey(provider="SBB", legs=legs, fare_status=FareStatus.UNKNOWN, deep_link="https://www.sbb.ch/")
    result.notes.append("Cenu SBB program nezjistí (sbb.ch blokuje dotazy) – ověřte ji na sbb.ch.")
    return result
