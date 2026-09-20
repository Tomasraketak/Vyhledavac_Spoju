"""Prohlížečová relace pro dopravce bez použitelného API.

Prohlížeč používáme až jako druhou volbu – je pomalý a ochrany na něj cílí
nejvíc. Když už ho ale spustíme, chceme, aby vypadal jako skutečný
uživatel. Proti původnímu kódu se liší v těchto bodech:

* ``headless`` je výchozí (původní kód otevíral okno a pak řešil, že se
  karta na pozadí uspává – headless ten problém nemá vůbec);
* maskování automatizace je na jednom místě a kompletní: ``navigator.webdriver``,
  ``navigator.plugins``, ``languages``, ``chrome`` objekt, WebGL vendor i
  ``permissions.query`` – jeden chybějící znak stačí k odhalení;
* blokujeme obrázky, fonty a analytiku, takže je načtení několikrát rychlejší;
* umíme odchytávat odpovědi XHR – často je jednodušší přečíst JSON, který si
  stránka sama stáhne, než parsovat DOM;
* detekce blokace je společná pro všechny dopravce.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable

from .errors import BlockedError, ProviderUnavailableError, TransientProviderError
from .http import BROWSER_PROFILES, BLOCK_MARKERS

log = logging.getLogger(__name__)

# Zdroje, které pro scraping nepotřebujeme – blokujeme kvůli rychlosti.
_BLOCKED_RESOURCES = {"image", "media", "font"}
_BLOCKED_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "facebook.com", "hotjar.com", "clarity.ms",
    "seznam.cz/rc", "gemius.pl", "smartlook.com", "adsman",
)

# Skript se vstřikuje před každým dokumentem – musí proběhnout dřív než
# detekční kód stránky.
_STEALTH_JS = """
(() => {
  const patch = (obj, prop, value) => {
    try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); }
    catch (e) {}
  };
  patch(navigator, 'webdriver', undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}

  patch(navigator, 'languages', ['cs-CZ', 'cs', 'sk', 'en-US', 'en']);

  // Prázdné pole pluginů je jasný signál headless prohlížeče.
  const fakePlugins = [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '' },
  ];
  patch(navigator, 'plugins', fakePlugins);
  patch(navigator, 'mimeTypes', [{ type: 'application/pdf' }]);
  patch(navigator, 'hardwareConcurrency', 8);
  patch(navigator, 'deviceMemory', 8);

  if (!window.chrome) { window.chrome = {}; }
  window.chrome.runtime = window.chrome.runtime || {};
  window.chrome.app = window.chrome.app || { isInstalled: false };

  // Headless Chrome vrací pro notifikace 'denied' i když je stav 'prompt'.
  const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
  if (originalQuery) {
    window.navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
  }

  // WebGL prozradí 'SwiftShader' / 'Google Inc.' u softwarového renderingu.
  const getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (parameter) {
    if (parameter === 37445) { return 'Intel Inc.'; }
    if (parameter === 37446) { return 'Intel Iris OpenGL Engine'; }
    return getParameter.apply(this, arguments);
  };

  // Stránka na pozadí jinak uspí časovače a vyhledávání se nedokončí.
  patch(document, 'visibilityState', 'visible');
  patch(document, 'hidden', false);
})();
"""


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


class BrowserSession:
    """Jedna instance prohlížeče se stealth kontextem."""

    def __init__(
        self,
        provider: str,
        *,
        headless: bool = True,
        locale: str = "cs-CZ",
        timezone: str = "Europe/Prague",
        proxy: str | None = None,
        engine: str = "chromium",
    ) -> None:
        self.provider = provider
        self.headless = headless
        self.locale = locale
        self.timezone = timezone
        self.proxy = proxy
        self.engine = engine
        self._pw = None
        self._browser = None
        self._context = None
        self.profile = random.choice([p for p in BROWSER_PROFILES if "Firefox" not in p["user_agent"]])

    async def start(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise ProviderUnavailableError(self.provider, "Playwright není nainstalován") from exc

        self._pw = await async_playwright().start()
        launcher = getattr(self._pw, self.engine)
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-features=IsolateOrigins,site-per-process",
            "--window-size=1920,1080",
        ]
        launch_kwargs: dict[str, Any] = {"headless": self.headless, "args": args}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        try:
            self._browser = await launcher.launch(**launch_kwargs)
        except Exception as exc:
            raise ProviderUnavailableError(
                self.provider,
                f"prohlížeč {self.engine} nelze spustit: {exc}. "
                "Spusťte 'playwright install chromium'.",
            ) from exc

        self._context = await self._browser.new_context(
            user_agent=self.profile["user_agent"],
            locale=self.locale,
            timezone_id=self.timezone,
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            ignore_https_errors=True,
            java_script_enabled=True,
            extra_http_headers={"Accept-Language": f"{self.locale},cs;q=0.9,en;q=0.7"},
        )
        await self._context.add_init_script(_STEALTH_JS)
        await self._context.route("**/*", self._route)

    async def _route(self, route, request) -> None:
        """Zahodí zbytečné zdroje – načtení je pak výrazně rychlejší."""
        try:
            if request.resource_type in _BLOCKED_RESOURCES:
                await route.abort()
                return
            url = request.url.lower()
            if any(host in url for host in _BLOCKED_HOSTS):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    async def new_page(self):
        await self.start()
        assert self._context is not None
        page = await self._context.new_page()
        page.set_default_timeout(20000)
        page.set_default_navigation_timeout(30000)
        return page

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._browser = self._pw = None

    # ---- pomůcky pro dopravce ------------------------------------------------
    async def goto(self, page, url: str, wait_until: str = "domcontentloaded") -> None:
        try:
            await page.goto(url, wait_until=wait_until)
        except Exception as exc:
            raise TransientProviderError(self.provider, f"načtení {url} selhalo: {exc}") from exc
        await self.assert_not_blocked(page)

    async def assert_not_blocked(self, page) -> None:
        """Pozná ochrannou stránku dřív, než se dopravce zamotá do selektorů."""
        try:
            title = (await page.title() or "").lower()
            body = (await page.inner_text("body"))[:2500].lower()
        except Exception:
            return
        haystack = f"{title} {body}"
        if any(marker in haystack for marker in BLOCK_MARKERS):
            raise BlockedError(self.provider, "prohlížeč narazil na antibot stránku")

    async def accept_cookies(self, page, selectors: list[str]) -> bool:
        """Zavře cookie lištu. Vrací True, když se něco kliklo."""
        for selector in selectors:
            try:
                element = page.locator(selector).first
                if await element.is_visible(timeout=1500):
                    await element.click(timeout=3000)
                    await page.wait_for_timeout(random.randint(250, 600))
                    return True
            except Exception:
                continue
        return False

    async def human_type(self, locator, text: str, page) -> None:
        """Píše s proměnlivou prodlevou – konstantní rytmus je podezřelý."""
        await locator.click()
        try:
            await locator.fill("")
        except Exception:
            pass
        for char in text:
            await locator.press_sequentially(char, delay=random.randint(45, 130))
        await page.wait_for_timeout(random.randint(400, 900))

    async def capture_json(
        self, page, url_predicate: Callable[[str], bool], timeout: float = 20.0
    ) -> Any:
        """Počká na XHR odpověď odpovídající predikátu a vrátí její JSON.

        Často spolehlivější než čtení DOMu – data čteme ze stejného zdroje,
        ze kterého je bere samotná stránka.
        """
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def on_response(response) -> None:
            if future.done():
                return
            try:
                if url_predicate(response.url) and response.status < 400:
                    data = await response.json()
                    if not future.done():
                        future.set_result(data)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
