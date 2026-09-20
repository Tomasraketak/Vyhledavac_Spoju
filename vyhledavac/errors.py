"""Chybové typy sdílené celým jádrem.

Rozlišujeme mezi chybami, které má smysl zkusit znovu (dočasný výpadek,
rate-limit) a těmi, které jsou trvalé (neznámá stanice). Retry logika v
``vyhledavac.http`` se řídí právě tímto rozdělením.
"""

from __future__ import annotations


class VyhledavacError(Exception):
    """Základ pro všechny chyby aplikace."""


class ProviderError(VyhledavacError):
    """Dopravce selhal. Nese jméno dopravce kvůli logování."""

    retryable = False

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message


class TransientProviderError(ProviderError):
    """Dočasná chyba – síť, 5xx, timeout. Má smysl opakovat."""

    retryable = True


class RateLimitedError(TransientProviderError):
    """HTTP 429 nebo ekvivalent. ``retry_after`` je v sekundách."""

    def __init__(self, provider: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class BlockedError(ProviderError):
    """Antibot ochrana (Cloudflare, DataDome, captcha, 403).

    Není retryable se stejnou identitou – volající musí změnit strategii
    (jiná identita, delší prodleva, nebo přepnutí na prohlížeč).
    """

    retryable = True


class StationNotFoundError(ProviderError):
    """Dopravce nezná zadanou stanici/město. Opakování nepomůže."""


class ProviderUnavailableError(ProviderError):
    """Dopravce není nakonfigurován nebo mu chybí závislost (např. Playwright)."""
