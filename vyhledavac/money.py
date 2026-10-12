"""Převod měn podle kurzu ČNB.

Původní kód volal ČNB synchronně v každém procesu zvlášť a při chybě
tiše použil natvrdo zadaný kurz 25,30. Tady se kurz stáhne jednou, uloží
do cache na den a fallback je vidět v logu.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal, InvalidOperation

from .cache import Cache, get_cache

log = logging.getLogger(__name__)

CNB_URL = ("https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/"
           "kurzy-devizoveho-trhu/denni_kurz.txt")

# Nouzové kurzy, když je ČNB nedostupná. Jsou jen hrubý odhad – proto se
# jejich použití loguje a ceny z nich označujeme jako přibližné.
FALLBACK_RATES: dict[str, Decimal] = {
    "CZK": Decimal("1"),
    "EUR": Decimal("25.30"),
    "PLN": Decimal("5.90"),
    "HUF": Decimal("0.064"),
    "GBP": Decimal("30.50"),
    "CHF": Decimal("27.00"),
    "USD": Decimal("23.00"),
    "RON": Decimal("4.95"),
    "NOK": Decimal("2.15"),
    "SEK": Decimal("2.20"),
    "DKK": Decimal("3.39"),
    "RSD": Decimal("0.216"),
}

CACHE_NS = "fx"
CACHE_TTL = 12 * 3600


class CurrencyConverter:
    """Převádí částky na CZK. Kurzy drží v cache."""

    def __init__(self, cache: Cache | None = None) -> None:
        self._cache = cache or get_cache()
        self._rates: dict[str, Decimal] | None = None
        self._is_fallback = False

    @property
    def is_fallback(self) -> bool:
        """True, když se nepodařilo stáhnout aktuální kurzy."""
        return self._is_fallback

    async def load(self, client=None) -> dict[str, Decimal]:
        """Načte kurzovní lístek – z cache, jinak z ČNB."""
        if self._rates is not None:
            return self._rates

        cached = self._cache.get(CACHE_NS, "cnb")
        if cached:
            self._rates = {k: Decimal(v) for k, v in cached.items()}
            return self._rates

        text = ""
        try:
            if client is None:
                from .http import HttpClient

                async with HttpClient("cnb") as own:
                    text = await own.get_text(CNB_URL)
            else:
                text = await client.get_text(CNB_URL)
        except Exception as exc:
            log.warning("ČNB kurzy nedostupné (%s), používám nouzové kurzy", exc)

        rates = _parse_cnb(text) if text else {}
        if rates:
            self._rates = rates
            self._is_fallback = False
            self._cache.set(CACHE_NS, "cnb", {k: str(v) for k, v in rates.items()}, ttl=CACHE_TTL)
        else:
            self._rates = dict(FALLBACK_RATES)
            self._is_fallback = True
        return self._rates

    def to_czk(self, amount: Decimal, currency: str) -> Decimal | None:
        """Převede na CZK. ``None``, když kurz neznáme."""
        currency = (currency or "CZK").upper()
        if currency == "CZK":
            return amount
        rates = self._rates or FALLBACK_RATES
        # ČNB nevede všechny měny (srbský dinár) – pak nouzový kurz
        rate = rates.get(currency) or FALLBACK_RATES.get(currency)
        if rate is None:
            log.debug("neznámá měna %s", currency)
            return None
        return (amount * rate).quantize(Decimal("0.01"))


def _parse_cnb(text: str) -> dict[str, Decimal]:
    """Rozparsuje textový kurzovní lístek ČNB.

    Formát: ``země|měna|množství|kód|kurz`` – kurz je za ``množství`` jednotek
    (např. 100 HUF), což původní kód ignoroval.
    """
    rates: dict[str, Decimal] = {"CZK": Decimal("1")}
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        _, _, amount_raw, code, rate_raw = parts
        code = code.strip().upper()
        if len(code) != 3 or not code.isalpha():
            continue
        try:
            quantity = Decimal(amount_raw.strip().replace(",", "."))
            rate = Decimal(rate_raw.strip().replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
        if quantity > 0:
            rates[code] = (rate / quantity).quantize(Decimal("0.0001"))
    return rates if len(rates) > 1 else {}


_default: CurrencyConverter | None = None


def get_converter() -> CurrencyConverter:
    global _default
    if _default is None:
        _default = CurrencyConverter()
    return _default
