"""Spuštění serveru tak, aby chyba nikdy nebyla němá.

``pythonw.exe`` na Windows nemá konzoli: ``sys.stderr`` je ``None`` a každá
chyba při startu (chybějící knihovna, obsazený port, vadná instalace)
zmizí beze stopy – příkaz „nic neudělá“. Proto se tu bez konzole píše
záznam do souboru a chyba se ukáže v okně se zprávou.

Modul smí importovat jen standardní knihovnu – musí fungovat, i když
zbytek programu nejde ani načíst.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import traceback
from pathlib import Path

LOG_NAME = "vyhledavac-web.log"


def has_console() -> bool:
    return sys.stderr is not None and sys.stdout is not None


def log_path() -> Path:
    """Záznam vedle programu; když tam nejde zapisovat, do dočasné složky."""
    for folder in (Path(__file__).resolve().parents[2], Path(tempfile.gettempdir())):
        target = folder / LOG_NAME
        try:
            with target.open("a", encoding="utf-8"):
                pass
            return target
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / LOG_NAME


def _setup_file_logging(path: Path) -> None:
    # Starý záznam nenechat růst donekonečna.
    try:
        if path.stat().st_size > 1_000_000:
            path.write_text("", encoding="utf-8")
    except OSError:
        pass
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def show_error(title: str, text: str) -> None:
    """Okno se zprávou na Windows; jinde (nebo když okno nejde) aspoň stderr."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:
            pass
    if sys.stderr is not None:
        print(f"{title}\n{text}", file=sys.stderr)


def run(argv: list[str] | None = None) -> None:
    console = has_console()
    path = log_path()
    if not console:
        _setup_file_logging(path)
    try:
        from .server import main

        main(argv)
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:
        detail = traceback.format_exc()
        logging.getLogger("vyhledavac.web").error("Spuštění selhalo:\n%s", detail)
        if console:
            raise
        hint = ""
        if isinstance(exc, ImportError):
            hint = ("\n\nNejspíš chybí knihovna. V příkazovém řádku ve složce programu spusťte:\n"
                    ".venv\\Scripts\\python -m pip install --upgrade -r requirements.txt")
        show_error("Vyhledávač spojů se nespustil",
                   f"{type(exc).__name__}: {exc}{hint}\n\nCelý výpis je v souboru:\n{path}")
        sys.exit(1)
