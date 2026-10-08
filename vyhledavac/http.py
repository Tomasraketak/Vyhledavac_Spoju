"""Odolný asynchronní HTTP klient.

Tohle je hlavní obrana proti antibot ochraně. Většinu dopravců vůbec
nemusíme scrapovat prohlížečem – jejich vlastní frontend volá JSON API a
to samé umíme zavolat i my. Aby nás ochrana nevyhodila, klient:

* posílá kompletní a konzistentní sadu hlaviček skutečného prohlížeče
  (včetně ``sec-ch-ua`` / ``Sec-Fetch-*``), ne jen ``User-Agent``;
* drží cookies napříč požadavky, takže se chová jako jedna relace;
* omezuje souběh a drží minimální rozestup mezi požadavky na stejný host;
* pozná blokaci (403, captcha, Cloudflare/DataDome stránka) a odliší ji od
  běžné chyby, aby engine mohl zareagovat jinak než slepým opakováním;
* opakuje s exponenciálním backoffem a jitterem a respektuje ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from .errors import BlockedError, RateLimitedError, TransientProviderError

log = logging.getLogger(__name__)

# Konzistentní profily prohlížečů. Míchat UA z jednoho a sec-ch-ua z jiného
# je klasický způsob, jak se prozradit – proto jsou svázané dohromady.
BROWSER_PROFILES: list[dict[str, str]] = [
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": '"Windows"',
    },
    {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": '"macOS"',
    },
    {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
                      "Gecko/20100101 Firefox/133.0",
        "sec_ch_ua": "",
        "platform": '"Windows"',
    },
]

# Otisky v těle odpovědi, které znamenají "chytila nás ochrana".
BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "attention required",
    "cf-browser-verification",
    "cf_chl_opt",
    "datadome",
    "px-captcha",
    "just a moment",
    "access denied",
    "request unsuccessful",
    "incapsula",
    "zugriff vorübergehend eingeschränkt",   # ÖBB
    "sicherheitsüberprüfung wird durchgeführt",  # Cloudflare (německy)
    "nur einen moment",
    "access temporarily restricted",
)


@dataclass
class RetryPolicy:
    """Pravidla opakování. Jitter brání tomu, aby se všichni klienti
    strefili do stejné vteřiny."""

    attempts: int = 3
    base_delay: float = 0.8
    max_delay: float = 12.0
    jitter: float = 0.4

    def delay_for(self, attempt: int) -> float:
        raw = min(self.base_delay * (2 ** attempt), self.max_delay)
        return raw * (1.0 + random.uniform(-self.jitter, self.jitter))


@dataclass
class HostLimiter:
    """Minimální rozestup mezi požadavky na jeden host + strop souběhu."""

    min_interval: float = 0.35
    concurrency: int = 4
    _last: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _sem: asyncio.Semaphore | None = field(default=None, init=False)

    def semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        return self._sem

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            gap = now - self._last
            if gap < self.min_interval:
                await asyncio.sleep(self.min_interval - gap + random.uniform(0, 0.12))
            self._last = time.monotonic()


def build_headers(
    profile: Mapping[str, str],
    *,
    language: str = "cs",
    json_api: bool = True,
    referer: str | None = None,
) -> dict[str, str]:
    """Hlavičky, které spolu drží – tak, jak je posílá skutečný prohlížeč."""
    accept_language = {
        "cs": "cs-CZ,cs;q=0.9,sk;q=0.8,en;q=0.7",
        "sk": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
        "de": "de-AT,de;q=0.9,en;q=0.7",
        "en": "en-US,en;q=0.9",
    }.get(language, "cs-CZ,cs;q=0.9,en;q=0.7")

    headers = {
        "User-Agent": profile["user_agent"],
        "Accept": "application/json, text/plain, */*" if json_api
                  else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty" if json_api else "document",
        "Sec-Fetch-Mode": "cors" if json_api else "navigate",
        "Sec-Fetch-Site": "same-origin" if json_api else "none",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if profile.get("sec_ch_ua"):
        headers["sec-ch-ua"] = profile["sec_ch_ua"]
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = profile.get("platform", '"Windows"')
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = "/".join(referer.split("/")[:3])
    return headers


def looks_blocked(status: int, body: str) -> bool:
    """Rozpozná antibot stránku i tam, kde server vrací 200."""
    if status in (401, 403, 405, 406, 409, 418, 451):
        return True
    if status == 200 and body:
        low = body[:4000].lower()
        # Krátká HTML odpověď na JSON endpoint je typicky challenge stránka.
        if any(marker in low for marker in BLOCK_MARKERS):
            return True
    return False


class HttpClient:
    """Obálka nad ``httpx.AsyncClient`` s retry, limity a detekcí blokace."""

    def __init__(
        self,
        provider: str = "http",
        *,
        language: str = "cs",
        timeout: float = 25.0,
        retry: RetryPolicy | None = None,
        limiter: HostLimiter | None = None,
        proxy: str | None = None,
        profile: Mapping[str, str] | None = None,
        verify: bool | str = True,
    ) -> None:
        self.provider = provider
        self.language = language
        self.retry = retry or RetryPolicy()
        self.limiter = limiter or HostLimiter()
        self.profile = dict(profile or random.choice(BROWSER_PROFILES))
        self._timeout = timeout
        self._proxy = proxy
        self._verify = verify
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HttpClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                follow_redirects=True,
                headers=build_headers(self.profile, language=self.language),
                proxy=self._proxy,
                verify=self._verify,
                # http2 vyžaduje balíček h2; bez něj httpx spadne při startu.
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def rotate_identity(self) -> None:
        """Vybere nový profil prohlížeče a zahodí cookies.

        Volá se po blokaci – pokračovat se stejným otiskem nemá smysl.
        """
        self.profile = dict(random.choice(BROWSER_PROFILES))
        if self._client is not None:
            self._client.cookies.clear()
            self._client.headers.update(build_headers(self.profile, language=self.language))

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        referer: str | None = None,
        expect_json: bool = True,
    ) -> httpx.Response:
        """Provede požadavek s opakováním. Vyhazuje ``ProviderError`` potomky."""
        await self.start()
        assert self._client is not None

        merged = dict(build_headers(self.profile, language=self.language,
                                    json_api=expect_json, referer=referer))
        if headers:
            merged.update(headers)

        last_exc: Exception | None = None
        for attempt in range(self.retry.attempts):
            if attempt:
                delay = self.retry.delay_for(attempt - 1)
                log.debug("%s: pokus %d/%d za %.1fs", self.provider,
                          attempt + 1, self.retry.attempts, delay)
                await asyncio.sleep(delay)

            await self.limiter.wait()
            try:
                async with self.limiter.semaphore():
                    response = await self._client.request(
                        method, url, headers=merged, params=params,
                        json=json_body, data=data,
                    )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = TransientProviderError(self.provider, f"síť: {exc!r}")
                continue
            except httpx.HTTPError as exc:
                raise TransientProviderError(self.provider, f"HTTP chyba: {exc!r}") from exc

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                last_exc = RateLimitedError(self.provider, "rate limit (429)", retry_after)
                await asyncio.sleep(retry_after or self.retry.delay_for(attempt))
                self.rotate_identity()
                continue

            body_preview = ""
            if response.status_code >= 400 or expect_json:
                try:
                    body_preview = response.text[:4000]
                except Exception:
                    body_preview = ""

            if looks_blocked(response.status_code, body_preview):
                last_exc = BlockedError(
                    self.provider,
                    f"blokováno ochranou (HTTP {response.status_code})",
                )
                self.rotate_identity()
                continue

            if response.status_code >= 500:
                last_exc = TransientProviderError(
                    self.provider, f"server vrátil {response.status_code}"
                )
                continue

            if response.status_code >= 400:
                # 4xx mimo blokaci je chyba dotazu – opakování nepomůže.
                raise TransientProviderError(
                    self.provider,
                    f"HTTP {response.status_code}: {body_preview[:200]}",
                )

            return response

        raise last_exc or TransientProviderError(self.provider, "vyčerpány pokusy")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return _decode_json(self.provider, response)

    async def post_json(self, url: str, json_body: Any, **kwargs: Any) -> Any:
        response = await self.request("POST", url, json_body=json_body, **kwargs)
        return _decode_json(self.provider, response)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        kwargs.setdefault("expect_json", False)
        response = await self.request("GET", url, **kwargs)
        return response.text


def _decode_json(provider: str, response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        snippet = response.text[:200].replace("\n", " ")
        if looks_blocked(200, response.text):
            raise BlockedError(provider, "místo JSON přišla ochranná stránka") from exc
        raise TransientProviderError(provider, f"nevalidní JSON: {snippet!r}") from exc


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
