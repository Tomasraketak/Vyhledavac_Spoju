"""Registr zásuvných modulů.

Dopravci, filtry i travel hacky se registrují dekorátorem. Přidat dalšího
dopravce tak znamená jediný nový soubor ve ``vyhledavac/providers/`` – nikde
se nemusí upravovat seznam ani ``if``. Registr navíc umí načíst moduly
uživatele přes proměnnou ``VYHLEDAVAC_PLUGINS``.
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
from typing import Callable, Generic, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Registry(Generic[T]):
    """Jmenný registr tříd."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._items: dict[str, type[T]] = {}

    def register(self, name: str | None = None) -> Callable[[type[T]], type[T]]:
        """Dekorátor. Jméno se bere z atributu ``name`` třídy, pokud se neurčí."""

        def wrapper(cls: type[T]) -> type[T]:
            key = name or getattr(cls, "name", None) or cls.__name__
            key = str(key)
            if key in self._items and self._items[key] is not cls:
                log.warning("%s: přepisuji existující registraci %r", self.label, key)
            self._items[key] = cls
            return cls

        return wrapper

    def add(self, name: str, cls: type[T]) -> None:
        self._items[str(name)] = cls

    def get(self, name: str) -> type[T]:
        try:
            return self._items[name]
        except KeyError:
            raise KeyError(
                f"{self.label}: {name!r} není registrován. K dispozici: {sorted(self._items)}"
            ) from None

    def try_get(self, name: str) -> type[T] | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> Iterable[tuple[str, type[T]]]:
        return sorted(self._items.items())

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)


PROVIDERS: Registry = Registry("providers")
FILTERS: Registry = Registry("filters")
HACKS: Registry = Registry("hacks")

_loaded = False

#: Moduly, které se nepodařilo načíst: ``{"vyhledavac.providers.obb": "chyba"}``.
#: GUI je ukáže – dřív se chyba jen zalogovala a dopravce potichu zmizel.
LOAD_ERRORS: dict[str, str] = {}


def load_plugins(force: bool = False) -> None:
    """Naimportuje vestavěné moduly a případné externí pluginy.

    Import má vedlejší efekt registrace, proto stačí projít balíčky.
    """
    global _loaded
    if _loaded and not force:
        return
    _loaded = True

    for package in ("vyhledavac.providers", "vyhledavac.filters", "vyhledavac.hacks"):
        try:
            module = importlib.import_module(package)
        except Exception as exc:  # pragma: no cover
            log.error("nelze načíst %s: %s", package, exc)
            continue
        for info in pkgutil.iter_modules(module.__path__):
            if info.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{package}.{info.name}")
            except Exception as exc:
                # Jeden rozbitý dopravce nesmí shodit celou aplikaci.
                log.error("modul %s.%s se nenačetl: %s", package, info.name, exc)
                LOAD_ERRORS[f"{package}.{info.name}"] = f"{type(exc).__name__}: {exc}"

    extra = os.environ.get("VYHLEDAVAC_PLUGINS", "")
    for dotted in filter(None, (p.strip() for p in extra.split(","))):
        try:
            importlib.import_module(dotted)
            log.info("načten externí plugin %s", dotted)
        except Exception as exc:
            log.error("externí plugin %s se nenačetl: %s", dotted, exc)
            LOAD_ERRORS[dotted] = f"{type(exc).__name__}: {exc}"
