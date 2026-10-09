"""VDV (Veřejná doprava Vysočiny) – ceny z tarifního počítadla kraje, viz ``online``."""

from .online import vdv as _vdv

SYSTEM, URL = _vdv.SYSTEM, _vdv.URL
prefetch = _vdv.prefetch
fares = _vdv.fares
best_fare = _vdv.best_fare
best_between = _vdv.best_between
_prices = _vdv._prices
parse = _vdv.parse
