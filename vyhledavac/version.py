"""Která verze programu právě běží – pro diagnostiku a pro restart serveru po aktualizaci.

``fingerprint()`` je otisk zdrojových souborů (jméno, velikost, čas změny).
Po ``git pull`` se změní, takže spouštěč pozná, že na portu běží ještě starý
server, a vymění ho za nový. ``describe()`` k tomu přidá větev a commit
z ``.git`` (čte se přímo soubor, git nemusí být nainstalovaný).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
STARTED = dt.datetime.now().astimezone()


@lru_cache(maxsize=1)
def fingerprint() -> str:
    digest = hashlib.sha1()
    for path in sorted(PACKAGE.rglob("*")):
        if path.suffix not in (".py", ".js", ".css", ".html", ".gz") or "__pycache__" in path.parts:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        digest.update(f"{path.relative_to(PACKAGE).as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:12]


def git_info() -> dict[str, str]:
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not head.startswith("ref:"):
        return {"branch": "(odpojená HEAD)", "commit": head[:7]}
    ref = head[4:].strip()
    branch = ref.removeprefix("refs/heads/")
    commit = ""
    try:
        commit = (git / ref).read_text(encoding="utf-8").strip()
    except OSError:
        try:          # ref může být jen v packed-refs
            for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    commit = line.split()[0]
        except OSError:
            pass
    return {"branch": branch, "commit": commit[:7]}


def describe() -> dict[str, Any]:
    return {"fingerprint": fingerprint(), **git_info(),
            "started": STARTED.isoformat(timespec="seconds")}
