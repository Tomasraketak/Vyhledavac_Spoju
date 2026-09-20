"""Vyhledávač spojů – modulární metavyhledávač vlaků a autobusů v Evropě."""

from .models import (
    DiscountCard,
    Fare,
    FareStatus,
    Journey,
    Leg,
    Mode,
    Passenger,
    SearchQuery,
    SearchResult,
    Stop,
)

__version__ = "2.0.0"

__all__ = [
    "DiscountCard", "Fare", "FareStatus", "Journey", "Leg", "Mode",
    "Passenger", "SearchQuery", "SearchResult", "Stop", "__version__",
]
