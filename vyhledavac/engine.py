"""Vyhledávací engine.

Orchestruje dopravce, filtry a travel hacky. Klíčové vlastnosti:

* **asyncio místo procesů.** Původní kód spouštěl ``ProcessPoolExecutor``
  se šesti procesy, z nichž každý startoval vlastní prohlížeč. Scraping je
  ale čekání na síť, ne výpočet – async zvládne totéž v jednom procesu,
  bez nutnosti picklovat data a bez rizika, že se GUI rozsype.
* **Izolace chyb.** Každý dopravce běží ve vlastní úloze a jeho selhání se
  zaznamená do ``ProviderResult``. Jeden rozbitý dopravce nikdy neshodí
  hledání ani nezmizí bez upozornění.
* **Průběžné výsledky.** Přes ``on_partial`` dostane UI výsledky hned,
  jak dorazí první dopravce.
* **Sdílená cache dotazů** – travel hacky se hodně doptávají na dílčí
  úseky a bez toho by se stejný dotaz posílal opakovaně.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .cache import Cache, get_cache
from .filters.base import Filter, FilterPipeline
from .hacks.base import TravelHack
from .models import Journey, ProviderResult, SearchQuery, SearchResult, sort_journeys
from .money import CurrencyConverter, get_converter
from .providers.base import Provider, build_providers
from .registry import HACKS, PROVIDERS, load_plugins

log = logging.getLogger(__name__)

PartialCallback = Callable[[ProviderResult], None]


class SearchEngine:
    """Spojuje dopravce, filtry a hacky do jednoho hledání."""

    def __init__(
        self,
        providers: Sequence[Provider] | None = None,
        *,
        filters: FilterPipeline | Sequence[Filter] | None = None,
        hacks: Sequence[TravelHack] = (),
        cache: Cache | None = None,
        converter: CurrencyConverter | None = None,
        concurrency: int = 6,
        provider_timeout: float = 90.0,
        skip_browser_in_hacks: bool = True,
    ) -> None:
        load_plugins()
        self.cache = cache or get_cache()
        self.converter = converter or get_converter()
        self.providers = list(providers) if providers is not None else []
        if isinstance(filters, FilterPipeline):
            self.pipeline = filters
        else:
            self.pipeline = FilterPipeline(filters or [])
        self.hacks = list(hacks)
        self.concurrency = concurrency
        self.provider_timeout = provider_timeout
        self.skip_browser_in_hacks = skip_browser_in_hacks
        self._semaphore = asyncio.Semaphore(concurrency)
        self._leg_cache: dict[tuple, list[Journey]] = {}
        self._leg_locks: dict[tuple, asyncio.Lock] = {}

    # ---- tovární metody ------------------------------------------------------
    @classmethod
    def build(
        cls,
        provider_names: Sequence[str],
        *,
        filter_spec: Sequence[Any] = (),
        hack_names: Sequence[str] = (),
        provider_options: dict[str, dict[str, Any]] | None = None,
        cache: Cache | None = None,
        **kwargs: Any,
    ) -> "SearchEngine":
        load_plugins()
        cache = cache or get_cache()
        converter = get_converter()
        providers = build_providers(
            provider_names, options=provider_options, cache=cache, converter=converter
        )
        pipeline = FilterPipeline.from_spec(list(filter_spec))
        hacks: list[TravelHack] = []
        for name in hack_names:
            hack_cls = HACKS.try_get(name)
            if hack_cls is None:
                log.error("neznámý hack %r (dostupné: %s)", name, ", ".join(HACKS.names()))
                continue
            hacks.append(hack_cls())
        return cls(providers, filters=pipeline, hacks=hacks,
                   cache=cache, converter=converter, **kwargs)

    # ---- hlavní běh ----------------------------------------------------------
    async def search(
        self,
        query: SearchQuery,
        *,
        on_partial: PartialCallback | None = None,
        sort_by: str = "departure",
        enable_hacks: bool = True,
    ) -> SearchResult:
        started = time.monotonic()
        await self.converter.load()

        result = SearchResult(query=query)
        direct = await self._run_providers(query, on_partial=on_partial)
        result.extend(direct)

        if enable_hacks and self.hacks:
            base = [j for r in direct for j in r.journeys]
            hacked = await self._run_hacks(query, base)
            if hacked:
                result.journeys.extend(hacked)

        # Řadíme *před* filtry: jinak by filtr ``limit`` uřízl náhodnou
        # pětici a teprve ji seřadil – "5 nejlevnějších" by nebylo nejlevnějších.
        result.journeys = sort_journeys(result.journeys, by=sort_by)
        result.journeys = self.pipeline.apply(result.journeys, query)
        # Deduplikace pracuje přes slovník a pořadí nezachovává, proto znovu.
        result.journeys = sort_journeys(result.journeys, by=sort_by)

        log.info(
            "hledání hotovo za %.1fs: %d spojů, %d dopravců selhalo",
            time.monotonic() - started,
            len(result.journeys),
            len(result.failed_providers),
        )
        return result

    async def _run_providers(
        self,
        query: SearchQuery,
        *,
        on_partial: PartialCallback | None = None,
        providers: Sequence[Provider] | None = None,
    ) -> list[ProviderResult]:
        providers = self.providers if providers is None else providers
        if not providers:
            log.warning("není vybrán žádný dopravce")
            return []

        async def one(provider: Provider) -> ProviderResult:
            async with self._semaphore:
                try:
                    return await asyncio.wait_for(
                        provider.run(query), timeout=self.provider_timeout
                    )
                except asyncio.TimeoutError:
                    log.warning("%s: překročen časový limit %.0fs",
                                provider.name, self.provider_timeout)
                    return ProviderResult(
                        provider=provider.name,
                        error=f"časový limit {self.provider_timeout:.0f}s vypršel",
                    )

        results: list[ProviderResult] = []
        tasks = [asyncio.create_task(one(p)) for p in providers]
        for finished in asyncio.as_completed(tasks):
            provider_result = await finished
            results.append(provider_result)
            if on_partial is not None:
                try:
                    on_partial(provider_result)
                except Exception:
                    log.exception("callback on_partial selhal")
        return results

    # ---- travel hacky --------------------------------------------------------
    async def _run_hacks(self, query: SearchQuery, base: list[Journey]) -> list[Journey]:
        if not base:
            return []

        found: list[Journey] = []
        for hack in self.hacks:
            try:
                produced = await hack.find(query, base, self._leg_search)
            except Exception:
                log.exception("hack %s selhal", hack.name)
                continue
            if produced:
                log.info("hack %s našel %d variant", hack.name, len(produced))
                found.extend(produced)
        return found

    async def _leg_search(self, query: SearchQuery) -> list[Journey]:
        """Hledání dílčího úseku pro hacky – s deduplikací souběžných dotazů."""
        key = (query.origin.lower(), query.destination.lower(),
               query.departure.isoformat())
        if key in self._leg_cache:
            return self._leg_cache[key]

        lock = self._leg_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._leg_cache:
                return self._leg_cache[key]
            # Na dílčí úseky pouštíme jen dopravce, kteří vracejí ceny –
            # jízdní řád bez ceny je pro výpočet úspory k ničemu. Zároveň
            # vynecháme ty, co potřebují prohlížeč: hacků je hodně a každý
            # start prohlížeče by trval déle než celé hledání.
            sellers = [
                p for p in self.providers
                if p.provides_prices and not (p.needs_browser and self.skip_browser_in_hacks)
            ]
            results = await self._run_providers(query, providers=sellers)
            journeys = [j for r in results for j in r.journeys]
            self._leg_cache[key] = journeys
            return journeys

    async def aclose(self) -> None:
        await asyncio.gather(
            *(p.aclose() for p in self.providers), return_exceptions=True
        )

    async def __aenter__(self) -> "SearchEngine":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def available_providers() -> list[str]:
    load_plugins()
    return PROVIDERS.names()
