"""Transitous – všechna spojení jako v IDOS (vlaky, autobusy, MHD), bez cen.

Jízdní řád z otevřeného plánovače Transitous nad daty CIS JŘ (viz
``vyhledavac/transitous.py``). Hodí se, když IDOS nejde nebo když chcete
vidět i spoje, ke kterým žádný e-shop cenu nedá. Dopravce u úseku je
skutečný provozovatel (``agencyName``: „ČSAD AUTOBUSY České Budějovice a.s.“,
„České dráhy, a.s.“).

Ve výchozím výběru není – zapněte ho v panelu *Zdroje dat* nebo přes
``--providers Transitous``. IDS ho jako zálohu použije sám, když IDOS
odmítne odpovídat.
"""

from __future__ import annotations

from .. import idos, transitous
from ..models import FareStatus, Journey, Leg, Mode, SearchQuery, Stop
from ..registry import PROVIDERS
from .base import HttpProvider


@PROVIDERS.register()
class TransitousProvider(HttpProvider):
    name = "Transitous"
    label = "Transitous – všechny spoje jako v IDOS (otevřená data CIS JŘ), bez cen"
    provides_prices = False
    provides_stops = True
    currency = "CZK"
    countries = ("CZ", "SK", "AT", "DE", "PL")
    czech_names = True
    hack_legs = False
    referer = None
    min_interval = 0.5

    async def search(self, query: SearchQuery) -> list[Journey]:
        connections = await transitous.search(self.client, self.cache, self.name, query.origin,
                                              query.destination, query.departure, query.window_end)
        journeys = [to_journey(conn) for conn in connections]
        out: list[Journey] = []
        seen: set[tuple] = set()
        for journey in (_trim_foreign_ends(j, query) for j in journeys):
            if journey is None:
                continue
            key = (journey.departure, journey.arrival, journey.legs[0].line)
            if key not in seen:              # po oříznutí MHD můžou splynout dva spoje
                seen.add(key)
                out.append(journey)
        return out


#: MHD na konci cesty v cizím městě (autobus 23 z nádraží Brašov do čtvrti
#: Căprioara) – spoj má končit na nádraží / autobusovém nádraží města.
LOCAL_MINUTES = 30
#: Krátká jízda S-Bahnem na konci v cizím městě je MHD, ne část cesty.
S_BAHN_MINUTES = 12


def _is_local(leg: Leg) -> bool:
    if leg.mode in (Mode.TRAM, Mode.METRO, Mode.WALK):
        return True
    if leg.mode is Mode.TRAIN:
        # S-Bahn z nádraží do centra (München Hbf → Marienplatz, 3 min)
        return ("s-bahn" in (leg.carrier or "").lower()
                and leg.duration.total_seconds() <= S_BAHN_MINUTES * 60)
    return (leg.mode is Mode.BUS and "flix" not in (leg.carrier or "").lower()
            and leg.duration.total_seconds() <= LOCAL_MINUTES * 60)


def _within(leg: Leg, city: str) -> bool:
    """Úsek MHD jen po městě ``city`` (Praha, Václavské nám. → Praha, Hlavní nádraží)."""
    from ..normalize import same_place

    if leg.mode is Mode.WALK:
        return True
    if leg.mode not in (Mode.BUS, Mode.TRAM, Mode.METRO) or "flix" in (leg.carrier or "").lower():
        return False
    return (leg.duration.total_seconds() <= LOCAL_MINUTES * 60
            and same_place(leg.origin, city) and same_place(leg.destination, city))


def _trim_foreign_ends(journey: Journey, query: SearchQuery) -> Journey | None:
    """Ořízne MHD na koncích: v cizím městě (autobus z nádraží do čtvrti) a po
    městě, které je začátkem/cílem (Transitous začíná „Prahu“ ve středu města
    a jede vyhlídkovou linkou na hlavní nádraží)."""
    from .. import geo
    from ..normalize import same_place

    legs = list(journey.legs)
    city_trip = same_place(query.origin, query.destination)      # MHD po městě – nic neořezávat
    foreign_end = geo.country_of(query.destination) not in (None, "CZ")
    while len(legs) > 1 and ((foreign_end and _is_local(legs[-1])) or (not city_trip and _within(legs[-1], query.destination))):
        legs.pop()
    foreign_start = geo.country_of(query.origin) not in (None, "CZ")
    while len(legs) > 1 and ((foreign_start and _is_local(legs[0])) or (not city_trip and _within(legs[0], query.origin))):
        legs.pop(0)
    if not legs or all(leg.mode is Mode.WALK for leg in legs):
        return None
    journey.legs = legs
    return journey


def to_journey(conn: idos.IdosConnection) -> Journey:
    legs = []
    for leg in conn.legs:
        stops = []
        last = len(leg.inline_stops) - 1
        prev = leg.departure
        for i, (name, arr, dep) in enumerate(leg.inline_stops):
            if i == 0:
                stops.append(Stop(name=idos.display_name(name), departure=leg.departure))
                continue
            if i == last:
                stops.append(Stop(name=idos.display_name(name), arrival=leg.arrival))
                continue
            arrival = idos._at(leg.departure.date(), arr, prev)
            departure = idos._at(leg.departure.date(), dep, arrival or prev)
            prev = departure or arrival or prev
            stops.append(Stop(name=idos.display_name(name), arrival=arrival, departure=departure))
        mode = Mode.TRAM if leg.is_city else Mode.BUS if leg.is_bus else Mode.TRAIN
        legs.append(Leg(origin=idos.display_name(leg.origin), destination=idos.display_name(leg.destination),
                        departure=leg.departure, arrival=leg.arrival, mode=mode, carrier=leg.carrier,
                        line=leg.train_label, stops=stops))
    return Journey(provider="Transitous", legs=legs, fare_status=FareStatus.UNKNOWN,
                   deep_link="https://transitous.org/")
