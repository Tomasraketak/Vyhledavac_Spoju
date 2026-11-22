"""Cenový kalendář – rychlé srovnání nejnižších cen napříč okolními dny.

Ceny u komerčních dopravců (RegioJet, FlixBus, ÖBB, PKP, MÁV, DB, Eurostar …)
se mezi dny v týdnu liší i násobně. Tenhle modul projede zvolené dny (výchozí
7 dní okolo zadaného data) přes **rychlé zdroje s cenami** bez prohlížečových
scraperů a bez travel hacků, sdílí jednu sadu HTTP relací, tokenů i cache
stanic a hlásí průběžně každý dokončený den přes ``on_day``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Sequence

from .cache import Cache, get_cache
from .engine import SearchEngine
from .filters.base import FilterPipeline
from .models import DEFAULT_TZ, Journey, Mode, SearchQuery
from .money import CurrencyConverter, get_converter
from .providers.base import Provider, build_providers
from .registry import PROVIDERS, load_plugins

log = logging.getLogger(__name__)

#: Výchozí počet dnů v kalendáři (±3 dny).
DEFAULT_DAYS = 7
MAX_DAYS = 14
#: Rychlá nastavení pro dopravce v kalendáři – stačí nejnižší ceny, ne detaily zastávek.
FAST_OPTIONS: dict[str, dict[str, Any]] = {
    "RegioJet": {"detail_limit": 0},
    "ÖBB": {"price_pages": 1, "browser_fallback": False},
}
#: Zdroje, které pro cenový kalendář nedávají smysl (pomalé HTML detailové skrapování
#: pevných krajských tarifů, které se mezi dny nemění).
SKIP_IN_CALENDAR = {"IDS", "Plánovač", "MHD", "Transitous"}


@dataclass(slots=True)
class DayPrice:
    """Nejnižší nalezená cena za jeden kalendářní den."""

    date: dt.date
    status: str = "pending"          # pending | running | ok | empty | error
    min_price: Decimal | None = None
    provider: str = ""
    departure: dt.datetime | None = None
    duration_minutes: int | None = None
    transfers: int | None = None
    journey_count: int = 0
    priced_count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "weekday": self.date.weekday(),     # 0 = pondělí .. 6 = neděle
            "status": self.status,
            "price": float(self.min_price) if self.min_price is not None else None,
            "provider": self.provider,
            "departure": self.departure.isoformat(timespec="minutes") if self.departure else None,
            "durationMinutes": self.duration_minutes,
            "transfers": self.transfers,
            "journeys": self.journey_count,
            "priced": self.priced_count,
            "error": self.error,
        }


def date_window(
    center: dt.date,
    days: int = DEFAULT_DAYS,
    *,
    today: dt.date | None = None,
) -> list[dt.date]:
    """Seznam ``days`` po sobě jdoucích dnů kolem ``center``, nikdy v minulosti."""
    count = max(1, min(MAX_DAYS, int(days)))
    floor = today or dt.datetime.now(DEFAULT_TZ).date()
    start = center - dt.timedelta(days=count // 2)
    if start < floor:
        start = floor
    return [start + dt.timedelta(days=i) for i in range(count)]


def select_fast_providers(provider_names: Sequence[str]) -> list[str]:
    """Vybere z předaných zdrojů rychlé dopravce, kteří vrací ceny bez prohlížeče."""
    load_plugins()
    out: list[str] = []
    for name in dict.fromkeys(provider_names):
        if name in SKIP_IN_CALENDAR:
            continue
        cls = PROVIDERS.try_get(name)
        if cls is None:
            continue
        if not getattr(cls, "provides_prices", True) or getattr(cls, "needs_browser", False):
            continue
        out.append(name)
    return out


def _day_query(base: SearchQuery, day: dt.date) -> SearchQuery:
    """Dotaz pro jeden den kalendáře.

    Pro zadaný den respektuje zvolený čas odjezdu (aby nenabízel už odjeté
    spoje), pro okolní dny hledá od 05:00 s celodenním oknem, aby našel
    skutečně nejlevnější spoj toho dne.
    """
    tz = base.departure.tzinfo or DEFAULT_TZ
    if day == base.date:
        when = base.departure
        window = max(base.window, dt.timedelta(hours=16))
    else:
        when = dt.datetime(day.year, day.month, day.day, 5, 0, tzinfo=tz)
        window = dt.timedelta(hours=19)
    return base.replace(departure=when, window=window, round_trip=None)


def _calendar_filters(query: SearchQuery) -> FilterPipeline:
    spec: list[Any] = ["departure_window", "drop_sold_out", "regional_only"]
    if query.trains_only:
        spec.append(("only_modes", {"modes": [Mode.TRAIN.value]}))
    return FilterPipeline.from_spec(spec)


async def scan_calendar(
    query: SearchQuery,
    provider_names: Sequence[str] | None = None,
    *,
    days: int = DEFAULT_DAYS,
    providers: Sequence[Provider] | None = None,
    cache: Cache | None = None,
    converter: CurrencyConverter | None = None,
    on_day: Callable[[DayPrice], None] | None = None,
    concurrency: int = 3,
    per_day_timeout: float = 35.0,
    today: dt.date | None = None,
) -> list[DayPrice]:
    """Zjistí nejnižší ceny pro ``days`` dnů kolem ``query.date``.

    Sdílí jednu sadu instancí dopravců (HTTP keep-alive, tokeny, cache stanic)
    a omezuje souběh na ``concurrency`` dnů najednou, aby nepřetěžovala API.
    """
    load_plugins()
    target_days = date_window(query.date, days, today=today)
    own_providers = providers is None
    if own_providers:
        chosen = select_fast_providers(provider_names or [])
        cache = cache or get_cache()
        converter = converter or get_converter()
        built = build_providers(
            chosen,
            options={name: dict(FAST_OPTIONS[name]) for name in chosen if name in FAST_OPTIONS},
            cache=cache,
            converter=converter,
        )
    else:
        built = list(providers or ())
        cache = cache or get_cache()
        converter = converter or get_converter()

    engine = SearchEngine(
        built,
        filters=_calendar_filters(query),
        cache=cache,
        converter=converter,
        provider_timeout=per_day_timeout,
    )
    await engine.converter.load()

    # Vyřadíme dopravce, kteří na tuhle trasu nejezdí – jednou pro celý kalendář.
    from . import city

    city_trip = city.trip(query.origin, query.destination)
    if city_trip is not None:
        active: list[Provider] = []
    else:
        engine._skipped = engine._domestic_skips(query)
        route_skips = set(engine._route_skips(query))
        active = [p for p in engine.providers if p.name not in engine._skipped and p.name not in route_skips]

    results_by_date: dict[dt.date, DayPrice] = {d: DayPrice(date=d) for d in target_days}

    if not active:
        reason = "Pro tuto trasu není zapnutý žádný rychlý zdroj s cenami."
        for d in target_days:
            entry = DayPrice(date=d, status="empty", error=reason)
            results_by_date[d] = entry
            if on_day is not None:
                on_day(entry)
        if own_providers:
            await engine.aclose()
        return [results_by_date[d] for d in target_days]

    sem = asyncio.Semaphore(max(1, concurrency))
    pipeline = _calendar_filters(query)

    async def scan_one(day: dt.date) -> DayPrice:
        async with sem:
            if on_day is not None:
                on_day(DayPrice(date=day, status="running"))
            dq = _day_query(query, day)
            try:
                provider_results = await engine._run_providers(dq, providers=active, cap=per_day_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("cenový kalendář %s selhal", day)
                entry = DayPrice(date=day, status="error", error=str(exc))
                results_by_date[day] = entry
                if on_day is not None:
                    on_day(entry)
                return entry

            journeys: list[Journey] = [j for r in provider_results for j in r.journeys]
            journeys = pipeline.apply(journeys, dq)
            priced = [j for j in journeys if j.price is not None]
            if priced:
                cheapest = min(priced, key=lambda j: (j.price, j.duration))
                entry = DayPrice(
                    date=day,
                    status="ok",
                    min_price=cheapest.price,
                    provider=cheapest.provider,
                    departure=cheapest.departure,
                    duration_minutes=int(cheapest.duration.total_seconds() // 60),
                    transfers=cheapest.transfers,
                    journey_count=len(journeys),
                    priced_count=len(priced),
                )
            else:
                errors = [r.error for r in provider_results if r.error and not r.not_served]
                entry = DayPrice(
                    date=day,
                    status="error" if errors and not journeys else "empty",
                    journey_count=len(journeys),
                    priced_count=0,
                    error=errors[0] if errors and not journeys else None,
                )
            results_by_date[day] = entry
            if on_day is not None:
                on_day(entry)
            return entry

    # Zadaný den a nejbližší okolí spustíme jako první.
    ordered = sorted(target_days, key=lambda d: (abs((d - query.date).days), d))
    try:
        await asyncio.gather(*(scan_one(day) for day in ordered))
    finally:
        if own_providers:
            await engine.aclose()

    return [results_by_date[d] for d in target_days]
