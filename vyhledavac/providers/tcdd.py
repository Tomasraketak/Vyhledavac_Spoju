"""TCDD Taşımacılık – Turecké dráhy (YHT Ankara – Istanbul – Konya, Mavi
Tren, Doğu Ekspresi …). Jízdní řád z IDOS, cenu ukáže e-shop TCDD.

E-shop **ebilet.tcddtasimacilik.gov.tr** (Vue) volá API
``web-api-prod-ytp.tcddtasimacilik.gov.tr``:

* ``GET cdn-api-prod-ytp…/datas/stations.json`` – 787 stanic (bez ochrany);
* ``POST /tms/train/train-availability`` – spoje s cenami. Server ale
  vyžaduje token Cloudflare Turnstile (``GET /crs/captcha/requirement`` →
  ``{"required": true, "siteKey": …}``, bez něj 403) – captchu neřešíme ani
  neobcházíme. Turečtí přeprodejci (enuygun, turna) mají Cloudflare
  „Just a moment…“, obilet vlaky neprodává.

Proto jen **jízdní řád**: IDOS zná vlaky TCDD („X2 81003 YHT ANKARA -
ISTANBUL“) v časech místních (Turecko UTC+3, viz ``geo.zone_of``). Spoje
jsou bez ceny s odkazem na e-shop TCDD. Ptáme se, jen když cesta začíná nebo
končí v Turecku.
"""

from __future__ import annotations

import datetime as dt
import logging

from .. import geo, idos, stations
from ..errors import StationNotFoundError
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery
from ..normalize import normalize, strip_accents
from ..registry import PROVIDERS
from .base import HttpProvider

log = logging.getLogger(__name__)

SHOP = "https://ebilet.tcddtasimacilik.gov.tr/"
NAME_TTL = 30 * 24 * 3600
PAGES = 2


@PROVIDERS.register()
class TcddProvider(HttpProvider):
    name = "TCDD"
    label = "TCDD – Turecké dráhy (jízdní řád z IDOS; cenu ukáže e-shop TCDD, chráněný captchou)"
    multi_passenger = True
    provides_prices = False
    currency = "TRY"
    countries = ("TR",)
    home = ("TR",)
    czech_names = True
    international_only = True
    hack_legs = False
    exact_places = True
    referer = "https://idos.cz/"
    min_interval = 0.7

    async def _idos_name(self, name: str) -> str:
        """„Istanbul; Turecko“ z našeptávače IDOS (jen vlakové jízdní řády)."""
        place = stations.place(name)
        if place is None or not place.code:
            return name
        key = f"tcdd-name:{normalize(name)}"
        cached = self.cache.get(self.name, key)
        if cached:
            return cached
        chosen = None
        for text in dict.fromkeys((place.local or place.name, name, place.name, strip_accents(place.local or place.name))):
            page = await self.client.get_text(idos.ajax_url("SearchTimetableObjects", idos.TRAINS),
                                              params=idos.suggest_params(text), referer=self.referer)
            items = idos.parse_suggest(page)
            chosen = idos.pick_foreign(items, place.country) or next(
                (str(i["selectedText"]) for i in items
                 if str(i.get("description") or "").startswith("stanice")
                 and str(i.get("selectedText") or "").endswith("; " + place.country)), None)
            if chosen:
                break
        if not chosen:
            raise StationNotFoundError(self.name, f"místo {name!r} IDOS nezná")
        self.cache.set(self.name, key, chosen, ttl=NAME_TTL)
        return chosen

    async def search(self, query: SearchQuery) -> list[Journey]:
        ends = {geo.country_of(query.origin), geo.country_of(query.destination)}
        if "TR" not in ends:
            raise StationNotFoundError(self.name, "TCDD – jen cesty z Turecka nebo do Turecka")
        origin, destination = await self._idos_name(query.origin), await self._idos_name(query.destination)
        found: list[idos.IdosConnection] = []
        when = query.departure
        for _ in range(PAGES):
            page = await self.client.get_text(idos.results_url(idos.TRAINS),
                                              params=idos.params(origin, destination, when), referer=self.referer)
            batch = [c for c in idos.parse_results(page, when.astimezone(geo.zone_of(query.origin)).date())
                     if c.departure >= query.departure - dt.timedelta(minutes=1)
                     and all(c.departure != o.departure for o in found)]
            if not batch:
                break
            found += batch
            when = max(c.departure for c in batch) + dt.timedelta(minutes=1)
            if when > query.window_end:
                break
        return [to_journey(self.name, c) for c in found
                if any("TCDD" in leg.carrier.upper() for leg in c.legs)]


def to_journey(provider: str, conn: idos.IdosConnection) -> Journey:
    legs = [Leg(origin=idos.display_name(leg.origin), destination=idos.display_name(leg.destination),
                departure=leg.departure, arrival=leg.arrival,
                mode=Mode.BUS if leg.is_bus else Mode.TRAIN, carrier=leg.carrier or "TCDD",
                line=leg.train_label) for leg in conn.legs]
    journey = Journey(provider=provider, legs=legs, fare_status=FareStatus.UNKNOWN,
                      deep_link=SHOP, raw={"idos": conn.share_url})
    journey.notes.append("TCDD: cenu a místenku ukáže e-shop ebilet.tcddtasimacilik.gov.tr (vyhledávání tam "
                         "chrání captcha, proto ji vyhledávač nezjistí). Jízdní řád z IDOS.")
    return journey
