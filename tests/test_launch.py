"""Start webu bez konzole (pythonw): chyba nesmí zmizet beze stopy."""

from __future__ import annotations

import logging
import sys

import pytest

from vyhledavac.web import _launch


def test_failure_without_console_is_logged_and_shown(monkeypatch, tmp_path):
    shown = []
    log_file = tmp_path / "web.log"
    monkeypatch.setattr(_launch, "has_console", lambda: False)
    monkeypatch.setattr(_launch, "log_path", lambda: log_file)
    monkeypatch.setattr(_launch, "show_error", lambda title, text: shown.append(text))
    monkeypatch.setitem(sys.modules, "vyhledavac.web.server", None)   # import selže
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        with pytest.raises(SystemExit) as exc:
            _launch.run([])
    finally:
        for handler in root.handlers[len(before):]:
            handler.close()
            root.removeHandler(handler)
    assert exc.value.code == 1
    assert shown and "pip install" in shown[0] and str(log_file) in shown[0]
    assert "Spuštění selhalo" in log_file.read_text(encoding="utf-8")


def test_failure_with_console_is_raised(monkeypatch):
    monkeypatch.setattr(_launch, "has_console", lambda: True)
    monkeypatch.setitem(sys.modules, "vyhledavac.web.server", None)
    with pytest.raises(ImportError):
        _launch.run([])
