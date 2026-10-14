"""Společné zázemí grafických rozhraní (tkinter i web).

Nic odsud nesahá na tkinter – webový server to importuje i tam, kde
tkinter není.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class AsyncRunner:
    """Asyncio smyčka ve vlastním vlákně.

    Rozhraní běží ve svém vlákně (tkinter mainloop, HTTP server) a hledání
    do smyčky jen posílá přes ``submit``.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def filter_spec(*, direct: bool = False, only_priced: bool = False,
                pareto: bool = False, limit: int = 80) -> list[Any]:
    """Filtry výsledků, jak je nastavuje uživatel v rozhraní."""
    spec: list[Any] = [("departure_window", {"hours": 12})]
    if direct:
        spec.append(("max_transfers", {"limit": 0}))
    spec += ["drop_sold_out", "deduplicate", "price_backfill", "same_trip"]
    if only_priced:
        spec.append("require_price")
    if pareto:
        spec.append("pareto")
    spec.append(("limit", {"count": limit}))
    return spec


def short_label(label: str) -> str:
    """Zkrátí popisek hacku na to, co se vejde do úzkého panelu."""
    text = label.split("(")[0].strip()
    return text if len(text) <= 32 else text[:31] + "…"


def explain(message: str, blocked: bool) -> str:
    """Přeloží technickou chybu do věty, se kterou uživatel něco udělá."""
    low = message.lower()
    if blocked or "blok" in low:
        return ("Dopravce vyhodnotil dotaz jako robota. Zkuste to za chvíli, "
                "nebo tento zdroj vypněte.")
    if "časový limit" in low or "timeout" in low:
        return "Dopravce neodpověděl včas. Ostatní zdroje výsledky dodaly."
    # Jen skutečně chybějící prohlížeč. Dřív stačilo, aby chyba obsahovala
    # slovo "prohlížeč", a každá chyba prohlížečového dopravce se tvářila
    # jako "chybí prohlížeč" – i když se prohlížeč normálně otevřel.
    if "chybí playwright" in low or "playwright není nainstalován" in low:
        return ("Chybí Playwright. Nainstalujte: pip install playwright "
                "&& playwright install chromium")
    if "nelze spustit" in low:
        return ("Prohlížeč se nepodařilo spustit – nejspíš chybí stažené Chromium. "
                "Spusťte: python -m playwright install chromium")
    if "nenalezena" in low or "nenalezeno" in low or "nezná stanici" in low:
        return "Tento dopravce zadanou stanici nezná – do téhle destinace nejezdí."
    if "selektor" in low or "změnil" in low:
        return "Web dopravce se změnil, scraper potřebuje aktualizovat."
    return message[:200]
