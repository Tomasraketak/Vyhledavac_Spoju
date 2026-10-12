"""Záznam a přehrání HTTP odpovědí dopravce pro testy bez sítě.

Nahrání (jednorázově, živě)::

    .venv/bin/python -m tests.replay record Vy Oslo Bergen 2026-10-15T07:00 tests/data/vy_oslo_bergen.json.gz

V testu se pak ``HttpClient.request`` nahradí přehrávačem: každý požadavek
dostane další nahranou odpověď se stejnou metodou a cestou URL (parametry
a tělo se neporovnávají – dotaz se mění s časem).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from vyhledavac.http import HttpClient

KEEP_HEADERS = ("content-type", "x-amzn-waf-action", "x-api-proof-required")


def _key(method: str, url: str) -> str:
    parts = urlsplit(url)
    return f"{method.upper()} {parts.netloc}{parts.path}"


class Replay:
    """Náhrada ``HttpClient.request`` – vrací nahrané odpovědi v pořadí."""

    def __init__(self, path: Path) -> None:
        self.queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for item in json.load(gzip.open(path, "rt", encoding="utf-8")):
            self.queues[item["key"]].append(item)

    async def request(self, client: HttpClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        queue = self.queues.get(_key(method, url))
        if not queue:
            raise AssertionError(f"nenahraný požadavek {_key(method, url)}")
        item = queue.popleft() if len(queue) > 1 else queue[0]
        response = httpx.Response(item["status"], headers=item["headers"], text=item["text"],
                                  request=httpx.Request(method, url))
        if response.status_code >= 400:
            from vyhledavac.errors import TransientProviderError

            raise TransientProviderError(client.provider, f"HTTP {response.status_code}")
        return response


def install(monkeypatch: Any, path: Path) -> Replay:
    replay = Replay(path)

    async def fake(self: HttpClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await replay.request(self, method, url, **kwargs)

    monkeypatch.setattr(HttpClient, "request", fake)
    return replay


async def _record(provider_name: str, origin: str, destination: str, when: str, out: str) -> None:
    import datetime as dt

    from vyhledavac.cache import MemoryCache
    from vyhledavac.models import DEFAULT_TZ, SearchQuery
    from vyhledavac.registry import PROVIDERS, load_plugins

    load_plugins()

    log: list[dict[str, Any]] = []
    original = HttpClient.request

    async def recording(self: HttpClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = await original(self, method, url, **kwargs)
        log.append({"key": _key(method, url), "status": response.status_code, "text": response.text,
                    "headers": {k: v for k, v in response.headers.items() if k.lower() in KEEP_HEADERS}})
        return response

    HttpClient.request = recording  # type: ignore[method-assign]
    provider = PROVIDERS.get(provider_name)(cache=MemoryCache())
    try:
        query = SearchQuery(origin=origin, destination=destination,
                            departure=dt.datetime.fromisoformat(when).replace(tzinfo=DEFAULT_TZ))
        journeys = await provider.search(query)
    finally:
        await provider.aclose()
        HttpClient.request = original  # type: ignore[method-assign]
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(log, fh, ensure_ascii=False)
    print(f"{len(log)} odpovědí, {len(journeys)} spojů -> {out}")
    for j in journeys[:5]:
        print(j.departure, j.arrival, [leg.line for leg in j.legs], j.price, j.fare_status)


if __name__ == "__main__":
    if len(sys.argv) != 7 or sys.argv[1] != "record":
        sys.exit(__doc__)
    asyncio.run(_record(*sys.argv[2:]))
