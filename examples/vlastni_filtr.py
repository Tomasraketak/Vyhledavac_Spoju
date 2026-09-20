"""Ukázka: vlastní filtr a vlastní travel hack."""

from __future__ import annotations

import datetime as dt

from vyhledavac.filters.base import Filter
from vyhledavac.models import Journey, SearchQuery
from vyhledavac.registry import FILTERS


@FILTERS.register()
class PouzeDenniSpoje(Filter):
    """Nechá jen spoje, které nejedou přes noc.

    Volitelné parametry se předávají při sestavení pipeline::

        FilterPipeline.from_spec([("pouze_denni", {"od": 6, "do": 22})])
    """

    name = "pouze_denni"
    label = "Jen denní spoje"

    def apply(self, journeys: list[Journey], query: SearchQuery) -> list[Journey]:
        start = int(self.options.get("od", 6))
        end = int(self.options.get("do", 22))
        return [
            j for j in journeys
            if start <= j.departure.hour < end and start <= j.arrival.hour < end
        ]
