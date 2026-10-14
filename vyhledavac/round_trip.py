"""Cena tam i zpět – návrat tentýž den, nebo do 24 hodin.

U každého spoje se spočítá, kolik stojí cesta tam a zpět stejnou trasou:

* každá jízdenka spoje (část cesty) dvakrát – tam i zpátky stejně draho;
* nebo místo ní **celodenní / 24hodinová jízdenka IDS** pro tutéž část, když
  vyjde levněji než dvě jednosměrné (IDESKA, PID, VDV… – podle zón trasy);
* nebo jedna celodenní jízdenka IDS na celou cestu.

Vybere se nejlevnější kombinace. Jízdenka IDS platí na regionální vlaky
a autobusy stejně jako jednosměrná (ne na EC/IC/railjety ani komerční
autobusy). „Tentýž den“ = jízdenka platná od odjezdu do půlnoci, „do 24 h“
= jízdenka na 24 hodin.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Sequence

from .models import DEFAULT_TZ, Journey, Leg, Mode, SearchQuery

log = logging.getLogger(__name__)

#: Volby ve formuláři.
MODES = {"day": "tam i zpět tentýž den", "24h": "tam i zpět do 24 hodin"}


def valid_minutes(mode: str, departure: dt.datetime) -> float:
    """Jak dlouho musí jízdenka platit, aby pokryla cestu tam i zpět."""
    if mode == "24h":
        return 24 * 60
    local = departure.astimezone(DEFAULT_TZ)
    midnight = dt.datetime.combine(local.date() + dt.timedelta(days=1), dt.time(), tzinfo=local.tzinfo)
    return max(60.0, (midnight - local).total_seconds() / 60)


def _rides(legs: Sequence[Leg]) -> list[Leg]:
    return [leg for leg in legs if leg.mode is not Mode.WALK]


def day_ticket(query: SearchQuery, legs: Sequence[Leg], minutes: float) -> Journey | None:
    """Nejlevnější jízdenka IDS platná ``minutes`` minut na tuto část cesty (``None`` = není)."""
    from .hacks.idos_complete import ids_fare

    rides = _rides(legs)
    if not rides or any(leg.mode not in (Mode.TRAIN, Mode.BUS, Mode.TRAM, Mode.METRO) for leg in rides):
        return None
    try:
        return ids_fare(query, rides, valid_minutes=minutes)
    except Exception:                         # tarif IDS nesmí shodit výsledky
        log.debug("celodenní jízdenka IDS nejde spočítat", exc_info=True)
        return None


def _ticket_name(ticket: Journey) -> str:
    fare = ticket.fares[0]
    return f"{fare.label} {fare.tariff}".strip()


def compute(journey: Journey, query: SearchQuery) -> dict[str, Any] | None:
    """Nejlevnější cesta tam i zpět pro spoj; ``None``, když cenu nejde určit."""
    mode = query.round_trip
    if mode not in MODES or not journey.legs:
        return None
    minutes = valid_minutes(mode, journey.departure)
    options: list[tuple[Decimal, list[str]]] = []

    pieces = journey.segments or [journey]
    total, lines = Decimal(0), []
    for piece in pieces:
        if piece.price is None:
            total = None
            break
        twice = piece.price * 2
        ticket = day_ticket(query, piece.legs, minutes) if piece.legs else None
        if ticket is not None and ticket.price < twice:
            total += ticket.price
            lines.append(f"{piece.origin} → {piece.destination}: {_ticket_name(ticket)} "
                         f"{ticket.price:.0f} Kč (platí tam i zpět)")
        else:
            total += twice
            lines.append(f"{piece.origin} → {piece.destination}: 2× {piece.provider} {piece.price:.0f} Kč")
    if total is not None:
        options.append((total, lines))

    whole = day_ticket(query, journey.legs, minutes)
    if whole is not None:
        options.append((whole.price, [f"celá cesta: {_ticket_name(whole)} {whole.price:.0f} Kč "
                                      "(platí tam i zpět)"]))
    if not options:
        return None
    price, how = min(options, key=lambda o: o[0])
    single = journey.price
    saving = (single * 2 - price) if single is not None else None
    return {"price": price, "how": how, "mode": mode,
            "saving": saving if saving is not None and saving > 0 else None}


def annotate(journeys: Sequence[Journey], query: SearchQuery) -> None:
    """Doplní ke spojům ``raw["round_trip"]`` a poznámku s cenou tam i zpět."""
    if query.round_trip not in MODES:
        return
    for journey in journeys:
        info = compute(journey, query)
        if info is None:
            continue
        journey.raw["round_trip"] = info
        note = (f"↔ Tam i zpět ({MODES[info['mode']]}): {info['price']:.0f} Kč – " + "; ".join(info["how"]))
        if info["saving"]:
            note += f". Oproti dvěma jednosměrným ušetříte {info['saving']:.0f} Kč."
        journey.notes.append(note)
