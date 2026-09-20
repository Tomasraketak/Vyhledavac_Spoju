"""Perzistentní cache nad SQLite.

Nahrazuje původní CSV soubory (``border_cache.csv``, ``lokace_rj.csv``).
Ty se jen připisovaly, nikdy nevypršely a při souběhu procesů se mohly
poškodit. SQLite řeší obojí a přidává TTL – ceny jízdenek stárnou rychle,
ID stanic pomalu, takže každý záznam má vlastní dobu platnosti.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PATH = Path(os.environ.get("VYHLEDAVAC_CACHE", "~/.cache/vyhledavac/cache.sqlite")).expanduser()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    namespace TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT NOT NULL,
    expires   REAL,
    stored    REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);
CREATE INDEX IF NOT EXISTS idx_entries_expires ON entries(expires);
"""


class Cache:
    """Thread-safe key/value cache s TTL, oddělená do jmenných prostorů."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_PATH
        self._lock = threading.RLock()
        self._memory: dict[tuple[str, str], tuple[Any, float | None]] = {}
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ---- nastavení ----------------------------------------------------------
    def _init_db(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False + zámek: sdílíme jedno spojení mezi vlákny.
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except Exception:
            # Cache nikdy nesmí shodit vyhledávání – spadneme na paměťovou.
            self._conn = None

    # ---- veřejné API --------------------------------------------------------
    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        now = time.time()
        ck = (namespace, key)
        with self._lock:
            if ck in self._memory:
                value, expires = self._memory[ck]
                if expires is None or expires > now:
                    return value
                del self._memory[ck]

            if self._conn is None:
                return default
            try:
                row = self._conn.execute(
                    "SELECT value, expires FROM entries WHERE namespace=? AND key=?",
                    (namespace, key),
                ).fetchone()
            except Exception:
                return default
            if row is None:
                return default
            raw, expires = row
            if expires is not None and expires <= now:
                self.delete(namespace, key)
                return default
            try:
                value = json.loads(raw)
            except ValueError:
                return default
            self._memory[ck] = (value, expires)
            return value

    def set(self, namespace: str, key: str, value: Any, ttl: float | None = None) -> None:
        expires = time.time() + ttl if ttl else None
        with self._lock:
            self._memory[(namespace, key)] = (value, expires)
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO entries(namespace,key,value,expires,stored)"
                    " VALUES (?,?,?,?,?)",
                    (namespace, key, json.dumps(value, ensure_ascii=False), expires, time.time()),
                )
                self._conn.commit()
            except Exception:
                pass

    def delete(self, namespace: str, key: str) -> None:
        with self._lock:
            self._memory.pop((namespace, key), None)
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "DELETE FROM entries WHERE namespace=? AND key=?", (namespace, key)
                )
                self._conn.commit()
            except Exception:
                pass

    def purge_expired(self) -> int:
        """Smaže prošlé záznamy. Vrací počet smazaných řádků."""
        if self._conn is None:
            return 0
        with self._lock:
            try:
                cur = self._conn.execute(
                    "DELETE FROM entries WHERE expires IS NOT NULL AND expires <= ?",
                    (time.time(),),
                )
                self._conn.commit()
                return cur.rowcount
            except Exception:
                return 0

    def iter_namespace(self, namespace: str) -> Iterator[tuple[str, Any]]:
        if self._conn is None:
            return
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT key, value FROM entries WHERE namespace=?"
                    " AND (expires IS NULL OR expires > ?)",
                    (namespace, time.time()),
                ).fetchall()
            except Exception:
                return
        for key, raw in rows:
            try:
                yield key, json.loads(raw)
            except ValueError:
                continue

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None


# Sdílená instance – dopravci ji berou přes ``get_cache()``.
_default: Cache | None = None
_default_lock = threading.Lock()


def get_cache() -> Cache:
    global _default
    with _default_lock:
        if _default is None:
            _default = Cache()
        return _default


class MemoryCache(Cache):
    """Cache pouze v paměti – pro testy."""

    def __init__(self) -> None:  # noqa: D107 - záměrně nevolá rodiče
        self.path = Path(":memory:")
        self._lock = threading.RLock()
        self._memory = {}
        self._conn = None
