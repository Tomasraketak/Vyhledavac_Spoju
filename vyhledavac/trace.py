"""Trasování úvah: kde program zkouší lomy jízdenek, u kterých dopravců a s jakým výsledkem.

Zprávy jdou do loggeru ``vyhledavac.trace`` (úroveň INFO). Zapnutí:

* příkazová řádka: ``python -m vyhledavac … --trace`` – vypisuje do terminálu
  (stderr) průběžně;
* webové rozhraní: každé hledání si zprávy sbírá (``collect``) a ukazuje je
  v panelu *Diagnostika* → *Jak program přemýšlel*; server je zároveň píše
  do svého záznamu.

Formát: odsazení podle hloubky úvahy, např.::

    ▶ spoj z IDOS 07:05 Třeboň → Ljubljana: 4 úseky, 2 rozpisy jízdenek
      rozpis 1: IDESKA Třeboň → České Budějovice 49 Kč | koupit České Budějovice → Ljubljana
      ? České Budějovice → Ljubljana (CZ, SI) – ptám se: ÖBB, MÁV | prohlížeč: ČD
        ✗ nikdo neprodává tentýž spoj
      ✂ lom ve Villach Hbf: České Budějovice → Villach Hbf + Villach Hbf → Ljubljana
"""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger("vyhledavac.trace")

_depth: contextvars.ContextVar[int] = contextvars.ContextVar("trace_depth", default=0)
_sink: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("trace_sink", default=None)


_tag: contextvars.ContextVar[str] = contextvars.ContextVar("trace_tag", default="")


def set_tag(tag: str) -> None:
    """Značka souběžné úlohy („#2“ = druhý spoj) – předsadí se každé zprávě."""
    _tag.set(tag)


def trace(message: str) -> None:
    """Zapíše jednu úvahu (s odsazením podle aktuální hloubky)."""
    tag = _tag.get()
    line = (f"{tag} " if tag else "") + "  " * _depth.get() + message
    sink = _sink.get()
    if sink is not None:
        sink.append(line)
    log.info("%s", line)


@contextmanager
def nested() -> Iterator[None]:
    """Vnořená úvaha – zprávy uvnitř se odsadí."""
    token = _depth.set(_depth.get() + 1)
    try:
        yield
    finally:
        _depth.reset(token)


@contextmanager
def collect(lines: list[str]) -> Iterator[list[str]]:
    """Sbírá zprávy hledání do ``lines`` (pro webové rozhraní). Platí pro
    všechny asynchronní úlohy spuštěné uvnitř (kopírují si kontext)."""
    token = _sink.set(lines)
    try:
        yield lines
    finally:
        _sink.reset(token)


def enable_terminal(stream=None) -> None:
    """Vypisuje úvahy průběžně do terminálu (``--trace``)."""
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s · %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
