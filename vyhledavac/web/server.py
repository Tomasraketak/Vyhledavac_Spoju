"""Webové rozhraní: malý HTTP server nad vyhledávacím enginem.

Bez dalších závislostí – stačí standardní knihovna:

* ``ThreadingHTTPServer`` obsluhuje stránku a JSON API,
* hledání běží v asyncio smyčce ve vlastním vlákně (``AsyncRunner``),
* průběh se do prohlížeče posílá přes Server-Sent Events: po každé změně
  (dopravce začal, poslal mezistav, skončil, selhal) celý aktuální stav.

Stejně jako tkinter GUI platí: **výpadek jednoho dopravce nesmí vypadat
jako výpadek aplikace**. Výsledky přibývají průběžně, každý dopravce má
vlastní stav a chyby se sbírají do panelu, nic nevyskakuje.

Server poslouchá jen na ``127.0.0.1``. Když se prohlížeč déle než
``IDLE_SHUTDOWN`` sekund neozve, server se sám ukončí – po spuštění přes
``pythonw`` by jinak zůstal viset bez okna.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import mimetypes
import threading
import time
import urllib.parse
import uuid
import webbrowser
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import stations
from ..browser import playwright_available
from ..engine import SearchEngine
from ..filters.base import FilterPipeline
from ..models import (
    DEFAULT_TZ,
    DiscountCard,
    Journey,
    Passenger,
    ProviderResult,
    SearchQuery,
    SearchResult,
    sort_journeys,
)
from ..money import get_converter
from ..registry import HACKS, LOAD_ERRORS, PROVIDERS, load_plugins
from ..runtime import AsyncRunner, explain, short_label
from .serialize import journey_key, journey_to_dict

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
DEFAULT_PORT = 8765
#: Po kolika sekundách bez jediného dotazu z prohlížeče se server ukončí.
IDLE_SHUTDOWN = 120.0

#: Filtry na straně serveru. Řazení, druhy dopravy, přestupy, dopravce,
#: "jen se známou cenou" a "skrýt dražší i pomalejší" řeší prohlížeč sám –
#: přepnutí je pak okamžité a nevyžaduje nové hledání.
SERVER_FILTERS: list[Any] = [
    ("departure_window", {"hours": 12}),
    "deduplicate",
    "price_backfill",
    ("limit", {"count": 120}),
]


# ---------------------------------------------------------------------------
#  Jedno hledání
# ---------------------------------------------------------------------------
class SearchSession:
    """Stav jednoho hledání. Mění ho asyncio vlákno, čte HTTP vlákno."""

    def __init__(self, query: SearchQuery, providers: list[str], hacks: list[str]) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.query = query
        self.providers = providers
        self.hacks = hacks
        self.cond = threading.Condition()
        self.version = 0
        self.snapshot = "{}"
        self.done = False
        self.future = None
        self.live: dict[str, list[Journey]] = {}
        self.final: SearchResult | None = None
        self.status: dict[str, dict[str, Any]] = {
            name: {"name": name, "status": "running", "detail": "hledám…"} for name in providers
        }
        self.problems: list[dict[str, Any]] = []
        lead = query.lead
        if lead.has(DiscountCard.ISIC) and lead.age >= 26:
            self._problem("ISIC", "studentská sleva platí jen do 26 let – hledám ceny pro "
                                  "dospělého. Jste-li mladší, opravte věk.", "soft")
        for module, error in LOAD_ERRORS.items():
            self._problem(module.rsplit(".", 1)[-1],
                          f"modul se nenačetl, v nabídce proto chybí ({error})", "soft")
        self.publish()

    # ---- události z enginu (asyncio vlákno) ----------------------------------
    def on_progress(self, result: ProviderResult) -> None:
        self.live[result.provider] = list(result.journeys)
        priced = sum(1 for j in result.journeys if j.price is not None)
        self.status[result.provider] = {
            "name": result.provider, "status": "running", "count": len(result.journeys),
            "detail": f"{len(result.journeys)} spojů · ceny {priced}/{len(result.journeys)}…",
        }
        self.publish()

    def on_partial(self, result: ProviderResult) -> None:
        name = result.provider
        entry: dict[str, Any] = {"name": name, "elapsed": round(result.elapsed, 1)}
        if result.not_served:
            entry.update(status="empty", detail="nejezdí tam")
            self._problem(name, result.error or "do této destinace nejezdí", "not_served")
            self.live.pop(name, None)
        elif result.error:
            entry.update(status="blocked" if result.blocked else "error",
                         detail="blokováno" if result.blocked else "chyba")
            self._problem(name, explain(result.error, result.blocked),
                          "blocked" if result.blocked else "error", raw=result.error)
            self.live.pop(name, None)
        else:
            self.live[name] = list(result.journeys)
            if result.journeys:
                extra = " · s výhradou" if result.notes else ""
                entry.update(status="ok", count=len(result.journeys),
                             detail=f"{len(result.journeys)} spojů · {result.elapsed:.0f} s{extra}")
            else:
                entry.update(status="empty", detail="nic nenašel")
            for note in result.notes:
                self._problem(name, note, "soft")
        self.status[name] = entry
        self.publish()

    def on_done(self, result: SearchResult) -> None:
        self.final = result
        self.finish()

    def on_fatal(self, message: str) -> None:
        self._problem("Vyhledávání", message, "error")
        self.finish()

    def finish(self, cancelled: bool = False) -> None:
        for entry in self.status.values():
            if entry["status"] == "running":
                if cancelled:
                    entry.update(status="off", detail="zrušeno")
                elif entry.get("count"):
                    entry.update(status="ok", detail=f"{entry['count']} spojů")
        self.done = True
        self.publish(cancelled=cancelled)

    def _problem(self, provider: str, message: str, kind: str, raw: str = "") -> None:
        if any(p["provider"] == provider and p["message"] == message for p in self.problems):
            return
        self.problems.append({"provider": provider, "message": message, "kind": kind,
                              "raw": raw, "canDisable": provider in PROVIDERS.names()
                              and kind in ("error", "blocked")})

    # ---- snímek pro prohlížeč ------------------------------------------------
    def journeys(self) -> list[Journey]:
        if self.final is not None:
            return list(self.final.journeys)
        pool = [j for name in self.providers for j in self.live.get(name, [])]
        pool = sort_journeys(pool, by="departure")
        pool = FilterPipeline.from_spec(SERVER_FILTERS).apply(pool, self.query)
        return sort_journeys(pool, by="departure")

    def publish(self, cancelled: bool = False) -> None:
        journeys = self.journeys()
        keep = {id(j) for j in FilterPipeline.from_spec(["pareto"]).apply(list(journeys), self.query)}
        seen: dict[str, int] = {}
        items = []
        for journey in journeys:
            key = journey_key(journey)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key}#{seen[key]}"
            items.append(journey_to_dict(journey, key, dominated=id(journey) not in keep))
        query = self.query
        payload = {
            "id": self.id,
            "done": self.done,
            "cancelled": cancelled,
            "query": {"from": query.origin, "to": query.destination,
                      "departure": query.departure.isoformat(timespec="minutes")},
            "providers": [self.status[name] for name in self.providers],
            "problems": self.problems,
            "journeys": items,
        }
        text = json.dumps(payload, ensure_ascii=False)
        with self.cond:
            self.snapshot = text
            self.version += 1
            self.cond.notify_all()


# ---------------------------------------------------------------------------
#  Aplikace
# ---------------------------------------------------------------------------
class WebApp:
    def __init__(self) -> None:
        load_plugins()
        self.runner = AsyncRunner()
        self.sessions: dict[str, SearchSession] = {}
        self.lock = threading.Lock()
        self.last_seen = time.monotonic()
        self.converter = get_converter()
        try:
            self.runner.submit(self.converter.load()).result(timeout=15)
        except Exception:
            log.info("kurzy měn se nenačetly, použije se záložní kurz")

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def meta(self) -> dict[str, Any]:
        rate = self.converter.to_czk(Decimal(1), "EUR")
        return {
            "providers": [{
                "name": name,
                "label": getattr(cls, "label", name),
                "browser": bool(getattr(cls, "needs_browser", False)),
                "prices": bool(getattr(cls, "provides_prices", True)),
                "default": not getattr(cls, "needs_browser", False),
                "countries": list(getattr(cls, "countries", ())),
            } for name, cls in PROVIDERS.items()],
            "hacks": [{"name": name, "label": short_label(getattr(cls, "label", name)),
                       "default": name == "split_ticket"} for name, cls in HACKS.items()],
            "playwright": playwright_available(),
            "eurRate": float(rate) if rate else 25.0,
            "loadErrors": {k: str(v) for k, v in LOAD_ERRORS.items()},
        }

    def start_search(self, body: dict[str, Any]) -> SearchSession:
        query = parse_query(body)
        providers = [p for p in body.get("providers") or [] if p in PROVIDERS.names()]
        if not providers:
            raise ValueError("Vyberte alespoň jeden zdroj dat.")
        hacks = [h for h in body.get("hacks") or [] if h in HACKS.names()]

        # Aplikace má jednoho uživatele: nové hledání ruší to předchozí,
        # ať zbytečně neběží prohlížeče na pozadí.
        with self.lock:
            for old in list(self.sessions.values()):
                if not old.done:
                    self.cancel(old)
            session = SearchSession(query, providers, hacks)
            self.sessions[session.id] = session
            for stale in list(self.sessions)[:-6]:
                self.sessions.pop(stale, None)
        session.future = self.runner.submit(self._run(session))
        return session

    async def _run(self, session: SearchSession) -> None:
        engine = SearchEngine.build(session.providers, filter_spec=SERVER_FILTERS,
                                    hack_names=session.hacks)
        try:
            result = await engine.search(
                session.query, on_partial=session.on_partial, on_progress=session.on_progress,
                sort_by="departure", enable_hacks=bool(session.hacks))
            session.on_done(result)
        except Exception as exc:  # noqa: BLE001 - i tohle musí skončit v UI
            log.exception("hledání selhalo")
            session.on_fatal(str(exc))
        finally:
            await engine.aclose()

    def cancel(self, session: SearchSession) -> None:
        if session.done:
            return
        if session.future is not None:
            session.future.cancel()
        session.finish(cancelled=True)


def parse_query(body: dict[str, Any]) -> SearchQuery:
    origin = str(body.get("from") or "").strip()
    destination = str(body.get("to") or "").strip()
    if not origin or not destination:
        raise ValueError("Vyplňte odkud a kam.")
    try:
        day = dt.date.fromisoformat(str(body.get("date") or dt.date.today().isoformat()))
        hour, minute = (int(x) for x in str(body.get("time") or "00:00").split(":")[:2])
        when = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=DEFAULT_TZ)
    except ValueError as exc:
        raise ValueError("Neplatné datum nebo čas.") from exc
    try:
        age = int(body.get("age", 26))
    except (TypeError, ValueError) as exc:
        raise ValueError("Věk musí být číslo.") from exc
    if not 0 <= age <= 120:
        raise ValueError("Věk musí být mezi 0 a 120.")
    cards = (DiscountCard.ISIC,) if body.get("isic") else ()
    return SearchQuery(origin=origin, destination=destination, departure=when,
                       passengers=(Passenger(age=age, cards=cards),))


# ---------------------------------------------------------------------------
#  HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    app: WebApp  # nastaví ``make_server``
    server_version = "Vyhledavac/2"

    def log_message(self, fmt: str, *args: Any) -> None:  # tichý log
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- pomůcky -------------------------------------------------------------
    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _session(self, sid: str) -> SearchSession | None:
        return self.app.sessions.get(sid)

    # ---- GET -----------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - jméno dané http.server
        self.app.touch()
        path = self.path.split("?", 1)[0]
        if path == "/api/meta":
            return self._json(self.app.meta())
        if path == "/api/ping":
            return self._json({"ok": True})
        if path == "/api/stations":
            params = urllib.parse.parse_qs(self.path.partition("?")[2])
            q = (params.get("q") or [""])[0][:80]
            try:
                limit = max(1, min(30, int((params.get("limit") or ["10"])[0])))
            except ValueError:
                limit = 10
            return self._json({"q": q, "places": stations.suggest(q, limit)})
        if path.startswith("/api/search/") and path.endswith("/events"):
            session = self._session(path.split("/")[3])
            if session is None:
                return self._json({"error": "neznámé hledání"}, 404)
            return self._stream(session)
        return self._static(path)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self._json({"error": "nenalezeno"}, 404)
        data = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript",):
            kind += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, session: SearchSession) -> None:
        """Server-Sent Events: po každé změně celý stav hledání."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # Proud nemá délku – jeho konec ohlásí až zavření spojení.
        self.close_connection = True
        sent = 0
        try:
            while True:
                with session.cond:
                    session.cond.wait_for(lambda: session.version > sent, timeout=15)
                    version, snapshot, done = session.version, session.snapshot, session.done
                self.app.touch()
                if version > sent:
                    self.wfile.write(f"data: {snapshot}\n\n".encode("utf-8"))
                    sent = version
                    if done:
                        break
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    # ---- POST ----------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        self.app.touch()
        path = self.path.split("?", 1)[0]
        if path == "/api/search":
            try:
                session = self.app.start_search(self._body())
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"id": session.id})
        if path.startswith("/api/search/") and path.endswith("/cancel"):
            session = self._session(path.split("/")[3])
            if session is not None:
                self.app.cancel(session)
            return self._json({"ok": True})
        return self._json({"error": "nenalezeno"}, HTTPStatus.NOT_FOUND)


def make_server(app: WebApp, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)   # port je obsazený
    server.daemon_threads = True
    return server


def _already_running(port: int) -> bool:
    """Běží už na portu náš server (druhé dvojkliknutí na spouštěč)?"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def _watchdog(app: WebApp, server: ThreadingHTTPServer, idle: float) -> None:
    while True:
        time.sleep(5)
        busy = any(not s.done for s in app.sessions.values())
        if not busy and time.monotonic() - app.last_seen > idle:
            log.info("prohlížeč se %.0f s neozval, končím", idle)
            server.shutdown()
            return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m vyhledavac.web",
                                     description="Webové rozhraní vyhledávače spojů.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="neotvírat prohlížeč")
    parser.add_argument("--idle", type=float, default=IDLE_SHUTDOWN,
                        help="ukončit po N s bez prohlížeče (0 = nikdy)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if _already_running(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        log.info("server už běží – otevírám %s", url)
        if not args.no_browser:
            webbrowser.open(url)
        return

    # Databázi zastávek načíst hned – první písmeno v našeptávači pak nečeká.
    threading.Thread(target=stations.database, daemon=True).start()
    app = WebApp()
    server = make_server(app, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    log.info("Vyhledávač spojů běží na %s", url)
    if args.idle > 0:
        threading.Thread(target=_watchdog, args=(app, server, args.idle), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.runner.stop()


if __name__ == "__main__":
    main()
