"""Filtry výsledků v prohlížeči (app.js) – běží přes Node, když je k dispozici."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "js" / "same_departure.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js není nainstalovaný")
def test_same_departure_filter():
    run = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
