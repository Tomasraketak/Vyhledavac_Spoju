"""Převod modelů na JSON pro webové rozhraní."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from .. import stations
from ..models import Fare, Journey, Leg, Stop
from ..normalize import carrier_name


def _time(value: dt.datetime | None) -> str | None:
    return value.isoformat(timespec="minutes") if value is not None else None


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def stop_to_dict(stop: Stop) -> dict[str, Any]:
    return {"name": stop.name, "arr": _time(stop.arrival), "dep": _time(stop.departure),
            "platform": stop.platform}


def leg_to_dict(leg: Leg) -> dict[str, Any]:
    return {
        "mode": leg.mode.value,
        "line": leg.line,
        "carrier": carrier_name(leg.carrier),
        "from": leg.origin,
        "to": leg.destination,
        "dep": _time(leg.departure),
        "arr": _time(leg.arrival),
        "reservation": leg.reservation_required,
        "stops": [stop_to_dict(s) for s in leg.stops],
        # kraj, integrovaný systém a tarifní zóna nástupní a výstupní stanice
        "fromInfo": stations.info(leg.origin),
        "toInfo": stations.info(leg.destination),
    }


def fare_to_dict(fare: Fare) -> dict[str, Any]:
    return {
        "amount": _num(fare.amount),
        "currency": fare.currency,
        "czk": _num(fare.amount_czk),
        "label": fare.label,
        "tariff": fare.tariff,
        "bookable": fare.bookable,
        "seats": fare.seats_left,
    }


def _round_trip(journey: Journey) -> dict[str, Any] | None:
    info = journey.raw.get("round_trip") if isinstance(journey.raw, dict) else None
    if not info:
        return None
    return {"price": _num(info["price"]), "how": list(info["how"]), "mode": info["mode"],
            "saving": _num(info["saving"]) if info.get("saving") is not None else None}


def journey_to_dict(journey: Journey, key: str, *, dominated: bool = False) -> dict[str, Any]:
    price = journey.price
    return {
        "id": key,
        "provider": journey.provider,
        "dep": _time(journey.departure),
        "arr": _time(journey.arrival),
        "minutes": int(journey.duration.total_seconds() // 60),
        "transfers": journey.transfers,
        "price": _num(price),
        "priceLabel": journey.price_label,
        "fareStatus": journey.fare_status.value,
        "fares": [fare_to_dict(f) for f in journey.fares],
        "link": journey.deep_link,
        "notes": list(journey.notes),
        "hack": journey.is_hack,
        "dominated": dominated,
        "roundTrip": _round_trip(journey),
        "legs": [leg_to_dict(leg) for leg in journey.legs],
        "segments": [{
            "provider": s.provider,
            "from": s.origin,
            "to": s.destination,
            "priceLabel": s.price_label,
            "link": s.deep_link,
        } for s in journey.segments],
    }


def journey_key(journey: Journey) -> str:
    """Stabilní klíč spoje napříč průběžnými aktualizacemi.

    Rozbalený detail v prohlížeči tak zůstane otevřený, i když dopravce
    mezitím doplní cenu.
    """
    lines = "|".join(leg.line for leg in journey.legs)
    hack = "H" if journey.is_hack else ""
    return f"{hack}{journey.provider}|{journey.departure:%Y%m%d%H%M}|{journey.arrival:%H%M}|{lines}"
