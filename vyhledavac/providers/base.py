"""Základní třídy pro dopravce.

Nový dopravce = jeden soubor s potomkem ``HttpProvider`` (nebo
``BrowserProvider``), dekorovaný ``@PROVIDERS.register()``. Musí umět
jediné: ze ``SearchQuery`` vyrobit seznam ``Journey``. Všechno ostatní –
retry, cache, měny, izolace chyb – řeší jádro.
"""

from __future__ import annotations

import abc
import contextvars
import copy
import datetime as dt
import logging
import time
from decimal import Decimal
from typing import Any, Callable, ClassVar, Sequence

from ..cache import Cache, get_cache
from ..errors import ProviderError, ProviderUnavailableError
from ..models import Fare, Journey, Mode, ProviderResult, SearchQuery, mark_walks
from ..money import CurrencyConverter, get_converter
from ..registry import PROVIDERS

log = logging.getLogger(__name__)

# Poznámky se sbírají per volání ``run``, ne per instance: travel hacky volají
# stejného dopravce souběžně pro víc úseků a poznámky by se jinak míchaly.
_NOTES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "provider_notes", default=None
)
# Kam posílat průběžné výsledky (``Provider.publish``) – opět per volání ``run``.
_PUBLISH: contextvars.ContextVar[Callable[[list[Journey]], None] | None] = contextvars.ContextVar(
    "provider_publish", default=None
)

ProgressCallback = Callable[[ProviderResult], None]


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
    #: Domácí země: jeden konec cesty musí ležet v některé z nich (MÁV prodá
    #: Vídeň → Budapešť, ale ne Záhřeb → Lublaň). Prázdné = stačí ``countries``.
    home: ClassVar[tuple[str, ...]] = ()
    #: Oblast pro rozdělení dopravců ve webovém rozhraní ("cz", "central",
    #: "east", "west", "north"); prázdná = podle první země v ``countries``.
    region: ClassVar[str] = ""
    #: Rozumí české podobě zahraničních měst („Lublaň“)? Ostatním engine
    #: posílá místní jméno („Ljubljana“), viz ``stations.provider_name``.
    czech_names: ClassVar[bool] = False
    #: Na cestách jen po Česku se dopravce neptá – vnitrostátní spoje
    #: (a jejich ceny) mají čeští dopravci, dotaz by jen zdržoval.
    international_only: ClassVar[bool] = False
    #: Umí cenu pro skupinu různých cestujících (jinak se ptá za prvního).
    multi_passenger: ClassVar[bool] = False
    #: Smí se ho ptát travel hacky na dílčí úseky? (IDS z IDOS ne – drahé dotazy.)
    hack_legs: ClassVar[bool] = True
    #: Místa vybírá přesně (číslo zastávky z našeptávače) – kontrola země podle
    #: jména se neprovádí („Anděl“ v Praze není nizozemské Andel).
    exact_places: ClassVar[bool] = False

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

    def publish(self, journeys: list[Journey]) -> None:
        """Pošle průběžný stav, zatímco ``search`` ještě běží.

        Pomalý dopravce (ZSSK zjišťuje ceny spoj po spoji) tak ukáže jízdní
        řád hned a ceny doplňuje, jak přicházejí – nečeká se na poslední.
        Bez posluchače (CLI, travel hacky) je to no-op. Posílá se snímek:
        dopravce si může své objekty dál měnit.
        """
        callback = _PUBLISH.get()
        if callback is None:
            return
        snapshot = []
        for journey in journeys:
            if not _is_sane(journey):
                continue
            clone = copy.copy(journey)
            clone.fares = list(journey.fares)
            snapshot.append(clone)
        try:
            callback(snapshot)
        except Exception:
            log.exception("%s: průběžný výsledek se nepodařilo předat", self.name)

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

    async def run(
        self, query: SearchQuery, on_progress: ProgressCallback | None = None
    ) -> ProviderResult:
        """Spustí hledání a zabalí výsledek i chybu do ``ProviderResult``.

        Engine tohle volá místo ``search`` – díky tomu jeden padající
        dopravce nikdy neshodí ostatní. ``on_progress`` dostává průběžné
        stavy z ``publish`` (s ``partial=True``).
        """
        started = time.monotonic()
        notes: list[str] = []
        token = _NOTES.set(notes)

        def forward(journeys: list[Journey]) -> None:
            if on_progress is not None:
                on_progress(ProviderResult(
                    provider=self.name, journeys=journeys, partial=True,
                    elapsed=time.monotonic() - started, notes=list(notes),
                ))

        publish_token = _PUBLISH.set(forward if on_progress is not None else None)
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
            _PUBLISH.reset(publish_token)

        mark_walks(journeys)
        local_times(journeys)
        journeys = [j for j in journeys if _is_sane(j)]
        kept = [j for j in journeys if self.exact_places or _right_place(j, query)]
        if len(kept) < len(journeys):
            notes.append(f"{len(journeys) - len(kept)} spojů vynecháno – dopravce místo vyložil jinde "
                         "(jiná země, než jste zadali)")
            journeys = kept
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


#: Dopravci, kteří cenu zjišťují spoj po spoji (proklikem / dotazem na každý
#: spoj), oceňují jen první ``PRICE_SCOPE_COUNT`` spoje od zadaného odjezdu
#: (v celém okně hledání, bez časového limitu). Spoje za nimi se neukazují
#: vůbec (bez ceny by jen překážely), takže výsledek je vždy s cenou.
PRICE_SCOPE_COUNT = 3


def price_scope(journeys: Sequence[Journey], query: SearchQuery,
                count: int = PRICE_SCOPE_COUNT) -> list[Journey]:
    """Spoje, u kterých se má zjistit cena: první ``count`` od zadaného odjezdu."""
    until = scope_end(query, [j.departure for j in journeys])
    ordered = sorted((j for j in journeys if j.departure <= until), key=lambda j: (j.departure, j.arrival))
    return ordered[:max(count, 1)]


def local_times(journeys: Sequence[Journey]) -> None:
    """Časy ve výpisu jako na nádraží: místní čas stanice (odjezd ze Sofie
    23:00 EEST, ne 22:00 SELČ – okamžik je tentýž, mění se jen zápis)."""
    from .. import geo

    zones: dict[str, Any] = {}

    def zone(name: str):
        if name not in zones:
            zones[name] = geo.zone_of(name)
        return zones[name]

    from ..models import DEFAULT_TZ

    for journey in journeys:
        if not journey.legs or all(zone(name) is DEFAULT_TZ
                                   for name in (journey.legs[0].origin, journey.legs[-1].destination)):
            continue                 # cesta ve středoevropském čase – nic nepřepisujeme
        for leg in journey.legs:
            leg.departure = leg.departure.astimezone(zone(leg.origin))
            leg.arrival = leg.arrival.astimezone(zone(leg.destination))
            for stop in leg.stops:
                if stop.arrival is not None:
                    stop.arrival = stop.arrival.astimezone(zone(stop.name))
                if stop.departure is not None:
                    stop.departure = stop.departure.astimezone(zone(stop.name))


def scope_end(query: SearchQuery, departures: Sequence[dt.datetime] = ()) -> dt.datetime:
    """Do kdy se oceňuje: celé okno hledání (omezení je jen počet spojů)."""
    return query.window_end


def _right_place(journey: Journey, query: SearchQuery) -> bool:
    """Začíná a končí spoj v zemi, kterou uživatel zadal? ÖBB umí z „Trhových
    Svinů“ udělat zastávku v Bratislavě – takový spoj nepatří do výsledků."""
    from .. import geo, stations

    rides = [leg for leg in journey.legs if leg.mode is not Mode.WALK] or journey.legs
    if not rides:
        return True
    for asked, got in ((query.origin, rides[0].origin), (query.destination, rides[-1].destination)):
        a, b = geo.country_of(asked), geo.country_of(got)
        city = stations.place(asked)
        if city is not None and city.code and city.code != "CZ":
            # „Mnichov“ je München, ne česká vesnice Mnichov – OneTicket ho doplnil na Mnichovice
            if b and b != city.code and city.code not in stations.countries_for(got):
                return False
            there = stations.place(got)
            if (there is not None and there.lat is not None and city.lat is not None
                    and geo.km((city.lat, city.lon), (there.lat, there.lon)) > FAR_KM):
                return False
            continue
        if a and b and a != b and not (stations.countries_for(asked) & stations.countries_for(got)):
            return False
    return True


#: Spoj, který končí dál od hledaného zahraničního města, jede jinam.
FAR_KM = 100.0


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
