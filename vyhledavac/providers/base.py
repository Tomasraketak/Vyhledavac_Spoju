"""Základní třídy pro dopravce.

Nový dopravce = jeden soubor s potomkem ``HttpProvider`` (nebo
``BrowserProvider``), dekorovaný ``@PROVIDERS.register()``. Musí umět
jediné: ze ``SearchQuery`` vyrobit seznam ``Journey``. Všechno ostatní –
retry, cache, měny, izolace chyb – řeší jádro.
"""

from __future__ import annotations

import abc
import contextvars
import datetime as dt
import logging
import time
from decimal import Decimal
from typing import Any, ClassVar, Sequence

from ..cache import Cache, get_cache
from ..errors import ProviderError, ProviderUnavailableError
from ..models import Fare, Journey, ProviderResult, SearchQuery
from ..money import CurrencyConverter, get_converter
from ..registry import PROVIDERS

log = logging.getLogger(__name__)

# Poznámky se sbírají per volání ``run``, ne per instance: travel hacky volají
# stejného dopravce souběžně pro víc úseků a poznámky by se jinak míchaly.
_NOTES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "provider_notes", default=None
)


class Provider(abc.ABC):
    """Rozhraní dopravce."""

    #: Jméno v registru a v UI.
    name: ClassVar[str] = "provider"
    #: Popis pro nápovědu.
    label: ClassVar[str] = ""
    #: Měna, ve které dopravce obvykle vrací ceny.
    currency: ClassVar[str] = "CZK"
    #: Vrací dopravce ceny, nebo jen jízdní řád?
    provides_prices: ClassVar[bool] = True
    #: Vrací seznam zastávek? Travel hacky to potřebují.
    provides_stops: ClassVar[bool] = False
    #: Potřebuje prohlížeč (pomalejší, náchylnější na blokaci).
    needs_browser: ClassVar[bool] = False
    #: Nejmenší časový limit, který dopravce od enginu potřebuje (s).
    min_timeout: ClassVar[float] = 0.0
    #: Země, které dopravce obsluhuje (informativní, pro filtrování fronty).
    countries: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        cache: Cache | None = None,
        converter: CurrencyConverter | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.cache = cache or get_cache()
        self.converter = converter or get_converter()
        self.options = options or {}

    # ---- rozhraní k implementaci -------------------------------------------
    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[Journey]:
        """Vrátí spoje pro dotaz. Chyby hlásí výjimkou, ne prázdným seznamem."""

    async def healthcheck(self) -> bool:
        """Rychlé ověření, že je dopravce dostupný. Nepovinné."""
        return True

    async def aclose(self) -> None:
        """Úklid zdrojů (HTTP session, prohlížeč)."""

    # ---- servis pro potomky -------------------------------------------------
    def note(self, message: str) -> None:
        """Zaznamená dílčí potíž, která hledání nezastavila.

        Skončí v ``ProviderResult.notes`` a UI ji ukáže jako varování,
        ne jako chybu – spoje od dopravce přesto jsou.
        """
        log.info("%s: %s", self.name, message)
        notes = _NOTES.get()
        if notes is not None and message not in notes:
            notes.append(message)
    def make_fare(
        self,
        amount: Decimal | float | int | str,
        currency: str | None = None,
        **kwargs: Any,
    ) -> Fare:
        """Vyrobí ``Fare`` a rovnou doplní přepočet na CZK."""
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        cur = (currency or self.currency).upper()
        fare = Fare(amount=value, currency=cur, **kwargs)
        fare.amount_czk = self.converter.to_czk(value, cur)
        return fare

    async def run(self, query: SearchQuery) -> ProviderResult:
        """Spustí hledání a zabalí výsledek i chybu do ``ProviderResult``.

        Engine tohle volá místo ``search`` – díky tomu jeden padající
        dopravce nikdy neshodí ostatní.
        """
        started = time.monotonic()
        notes: list[str] = []
        token = _NOTES.set(notes)
        try:
            journeys = await self.search(query)
        except ProviderError as exc:
            # Neznámá stanice je při hledání bodů lomu běžná (autobusový
            # dopravce prostě nejede do každé železniční stanice) – nemá smysl
            # tím zaplavovat log jako chybou.
            level = logging.DEBUG if type(exc).__name__ == "StationNotFoundError" else logging.WARNING
            log.log(level, "%s selhal: %s", self.name, exc.message)
            return ProviderResult(
                provider=self.name,
                error=exc.message,
                blocked=exc.__class__.__name__ == "BlockedError",
                not_served=type(exc).__name__ == "StationNotFoundError",
                elapsed=time.monotonic() - started,
                notes=notes,
            )
        except Exception as exc:  # noqa: BLE001 - poslední záchrana
            log.exception("%s: neočekávaná chyba", self.name)
            return ProviderResult(
                provider=self.name,
                error=f"neočekávaná chyba: {exc!r}",
                elapsed=time.monotonic() - started,
                notes=notes,
            )
        finally:
            _NOTES.reset(token)

        journeys = [j for j in journeys if _is_sane(j)]
        return ProviderResult(
            provider=self.name,
            journeys=journeys,
            elapsed=time.monotonic() - started,
            notes=notes,
        )


class HttpProvider(Provider):
    """Dopravce postavený na JSON API. Preferovaná varianta.

    Nevolá prohlížeč, takže je rychlý a antibot ochrana na něj z principu
    tolik neplatí – posíláme stejný požadavek, jaký posílá jejich web.
    """

    #: Host pro ``Referer`` – bez něj některá API odmítají odpovídat.
    referer: ClassVar[str | None] = None
    #: Minimální rozestup požadavků v sekundách.
    min_interval: ClassVar[float] = 0.3

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = None

    @property
    def client(self):
        from ..http import HostLimiter, HttpClient

        if self._client is None:
            self._client = HttpClient(
                self.name,
                language=self.options.get("language", "cs"),
                limiter=HostLimiter(min_interval=self.min_interval),
                proxy=self.options.get("proxy"),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


class BrowserProvider(Provider):
    """Dopravce, kterého je nutné obsloužit prohlížečem.

    Používá se jen tam, kde se nepodařilo najít použitelné API. Prohlížeč
    je pomalý a nejvíc se na něj cílí ochranami, takže je to až druhá volba.
    """

    needs_browser: ClassVar[bool] = True
    #: Výchozí ``headless`` – lze přepsat volbou.
    headless: ClassVar[bool] = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = None

    async def browser(self):
        from ..browser import BrowserSession, playwright_available

        if not playwright_available():
            raise ProviderUnavailableError(
                self.name,
                "chybí Playwright – nainstalujte 'pip install playwright' "
                "a 'playwright install chromium'",
            )
        if self._session is None:
            self._session = BrowserSession(
                self.name,
                headless=self.options.get("headless", self.headless),
                locale=self.options.get("locale", "cs-CZ"),
                proxy=self.options.get("proxy"),
            )
            await self._session.start()
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _is_sane(journey: Journey) -> bool:
    """Zahodí zjevně vadné spoje, aby se nedostaly do řazení."""
    if not journey.legs:
        return False
    if journey.arrival < journey.departure:
        return False
    # Přes 48 hodin už je chyba parsování, ne spoj.
    if journey.duration > dt.timedelta(hours=48):
        return False
    return True


def build_providers(
    names: Sequence[str],
    *,
    options: dict[str, dict[str, Any]] | None = None,
    cache: Cache | None = None,
    converter: CurrencyConverter | None = None,
) -> list[Provider]:
    """Vyrobí instance dopravců podle jmen z registru."""
    from ..registry import load_plugins

    load_plugins()
    options = options or {}
    built: list[Provider] = []
    for name in names:
        cls = PROVIDERS.try_get(name)
        if cls is None:
            log.error("neznámý dopravce %r (dostupní: %s)", name, ", ".join(PROVIDERS.names()))
            continue
        try:
            built.append(cls(cache=cache, converter=converter, options=options.get(name, {})))
        except Exception as exc:
            log.error("dopravce %s nelze vytvořit: %s", name, exc)
    return built
