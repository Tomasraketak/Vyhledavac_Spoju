"""GUI – hlavně odolnost proti selhání dopravců.

Testy se spustí jen tam, kde je k dispozici tkinter a displej. Na CI bez
X serveru se přeskočí; lokálně se dají spustit pod Xvfb::

    xvfb-run -a python -m pytest tests/test_gui.py
"""

from __future__ import annotations

import os

import pytest

tk = pytest.importorskip("tkinter", reason="tkinter není nainstalován")

from vyhledavac.models import ProviderResult, SearchQuery  # noqa: E402
from tests.conftest import at, make_journey  # noqa: E402


@pytest.fixture
def app():
    try:
        root = tk.Tk()
        root.destroy()
    except Exception:
        pytest.skip("není dostupný displej")

    from vyhledavac.gui import App

    instance = App()
    instance.query = SearchQuery(origin="Praha", destination="Vídeň", departure=at(8))
    yield instance
    try:
        instance.runner.stop()
        instance.destroy()
    except Exception:
        pass


def test_failing_provider_does_not_hide_results_from_others(app):
    """Klíčová vlastnost: jeden padlý dopravce nesmí sebrat výsledky ostatních."""
    app._on_partial(ProviderResult(provider="RegioJet",
                                   journeys=[make_journey(price=240)], elapsed=1.0))
    app._on_partial(ProviderResult(provider="ČD", error="web se změnil"))

    assert len(app.tree.get_children()) == 1        # spoj od RegioJetu zůstal
    assert app.provider_rows["ČD"].state.cget("text") == "chyba"
    assert app.provider_rows["RegioJet"].dot.cget("text") == "●"


def test_blocked_provider_is_marked_and_explained(app):
    app._on_partial(ProviderResult(provider="OneTicket", error="blokováno ochranou",
                                   blocked=True))
    assert app.provider_rows["OneTicket"].state.cget("text") == "blokováno"
    # Panel problémů se zobrazil a nese srozumitelné vysvětlení.
    assert app.problem_card.winfo_manager() != ""


def test_all_providers_failing_keeps_window_usable(app):
    for name in ("RegioJet", "FlixBus", "ÖBB"):
        app._on_partial(ProviderResult(provider=name, error="timeout"))
    app._finish()

    assert app.btn_search.cget("state") == "normal"   # dá se hledat znovu
    assert app.empty_hint.cget("text")                # a je vysvětleno proč nic není


def test_results_stream_in_without_waiting_for_everyone(app):
    """Spoje se ukazují průběžně, nečeká se na nejpomalejšího dopravce.

    Obě varianty musí být "nedominované" (levnější je zároveň pomalejší),
    jinak je filtr ``pareto`` zcela správně sloučí na jednu.
    """
    app._on_partial(ProviderResult(provider="FlixBus",
                                   journeys=[make_journey(price=339, dep=at(9),
                                                          arr=at(12))]))
    first = len(app.tree.get_children())
    app._on_partial(ProviderResult(provider="RegioJet",
                                   journeys=[make_journey(price=240, dep=at(10),
                                                          arr=at(15))]))
    assert first == 1 and len(app.tree.get_children()) == 2


def test_summary_reports_missing_prices(app):
    app._on_partial(ProviderResult(provider="RegioJet", journeys=[
        make_journey(price=240, dep=at(9)),
        make_journey(price=None, dep=at(11)),
    ]))
    assert "pokrytí" in app.summary.cget("text")


def test_stale_results_from_cancelled_search_are_ignored(app):
    """Po zrušení nesmí do tabulky spadnout výsledky předchozího hledání."""
    app._generation = 5
    app.events.put((4, "partial", ProviderResult(provider="RegioJet",
                                                 journeys=[make_journey(price=100)])))
    app._drain()
    assert len(app.tree.get_children()) == 0


def test_problem_messages_are_translated_to_advice():
    from vyhledavac.gui import _explain

    assert "robota" in _explain("blokováno ochranou", True)
    assert "playwright" in _explain("chybí Playwright", False).lower()
    assert "nejezdí" in _explain("stanice 'Kúty' nenalezena", False)


def test_long_option_labels_are_shortened():
    from vyhledavac.gui import _short_label

    assert _short_label("Skryté město (vystoupíte dřív, než končí jízdenka)") == "Skryté město"
    assert len(_short_label("x" * 80)) <= 32
