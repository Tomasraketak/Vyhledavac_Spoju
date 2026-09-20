"""Rozhraní filtrů a jejich řetězení.

Filtr dostane seznam spojů a vrátí seznam spojů. Nic víc. Díky tomu jdou
libovolně skládat a přidání nového znamená jednu třídu s dekorátorem::

    @FILTERS.register()
    class BezNocnichPrestupu(Filter):
        name = "no_night_transfers"

        def apply(self, journeys, query):
            return [j for j in journeys if ...]
"""

from __future__ import annotations

import abc
import logging
from typing import Any, ClassVar, Iterable, Sequence

from ..models import Journey, SearchQuery

log = logging.getLogger(__name__)


class Filter(abc.ABC):
    """Jeden krok zpracování výsledků."""

    name: ClassVar[str] = "filter"
    label: ClassVar[str] = ""

    def __init__(self, **options: Any) -> None:
        self.options = options

    @abc.abstractmethod
    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        """Vrátí upravený seznam spojů."""

    def __call__(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        return self.apply(journeys, query)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.options or ''}>"


class FilterPipeline:
    """Postupně aplikuje filtry a loguje, kolik spojů který ubral."""

    def __init__(self, filters: Sequence[Filter] = ()) -> None:
        self.filters: list[Filter] = list(filters)

    def add(self, *filters: Filter) -> "FilterPipeline":
        self.filters.extend(filters)
        return self

    def apply(self, journeys: Iterable[Journey], query: SearchQuery) -> list[Journey]:
        current = list(journeys)
        for step in self.filters:
            before = len(current)
            try:
                current = step.apply(current, query)
            except Exception as exc:
                # Rozbitý filtr nesmí zahodit výsledky.
                log.error("filtr %s selhal (%s) – přeskakuji", step.name, exc)
                continue
            log.debug("filtr %s: %d -> %d", step.name, before, len(current))
        return current

    @classmethod
    def from_spec(cls, spec: Sequence[str | tuple[str, dict[str, Any]]]) -> "FilterPipeline":
        """Postaví pipeline z názvů filtrů (např. z konfigurace či CLI)."""
        from ..registry import FILTERS, load_plugins

        load_plugins()
        built: list[Filter] = []
        for entry in spec:
            name, options = (entry, {}) if isinstance(entry, str) else entry
            filter_cls = FILTERS.try_get(name)
            if filter_cls is None:
                log.error("neznámý filtr %r (dostupné: %s)", name, ", ".join(FILTERS.names()))
                continue
            built.append(filter_cls(**options))
        return cls(built)

    def __len__(self) -> int:
        return len(self.filters)
