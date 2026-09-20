"""Grafické rozhraní (Tkinter).

Oproti původnímu ``VyhledavacMain.py`` má GUI jedinou odpovědnost:
zobrazovat. Veškerá logika hledání, filtrování i hacků žije v jádře, takže
se dá používat i z CLI nebo z jiné aplikace.

Asyncio smyčka běží v samostatném vlákně; do Tkinteru se sahá výhradně
přes ``self.after``, což je jediný bezpečný způsob, jak ho z jiného vlákna
aktualizovat.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .engine import SearchEngine
from .models import DEFAULT_TZ, DiscountCard, Passenger, SearchQuery, SearchResult
from .registry import HACKS, PROVIDERS, load_plugins

log = logging.getLogger(__name__)

BG = "#f0f4f8"
CARD = "#ffffff"
ACCENT = "#2c3e50"


class AsyncRunner:
    """Asyncio smyčka žijící ve vlastním vlákně."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> "asyncio.Future":
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def card(parent: tk.Widget, title: str = "") -> tk.Frame:
    frame = tk.Frame(parent, bg=CARD, highlightthickness=1,
                     highlightbackground="#e0e0e0", bd=0)
    if title:
        tk.Label(frame, text=title, font=("Segoe UI", 11, "bold"),
                 bg=CARD, fg=ACCENT).pack(anchor=tk.W, pady=(10, 5), padx=15)
    return frame


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_plugins()
        self.title("Vyhledávač spojů")
        self.geometry("1180x860")
        self.configure(bg=BG)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=32)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        self.runner = AsyncRunner()
        self.events: queue.Queue = queue.Queue()
        self.row_data: dict[str, Any] = {}
        self._searching = False

        self._build_form()
        self._build_table()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- rozložení -----------------------------------------------------------
    def _build_form(self) -> None:
        pad = tk.Frame(self, bg=BG)
        pad.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        self.pad = pad

        form = card(pad, "Parametry vyhledávání")
        form.pack(fill=tk.X, pady=(0, 12))
        grid = tk.Frame(form, bg=CARD)
        grid.pack(fill=tk.X, padx=15, pady=(0, 15))

        now = dt.datetime.now()
        self.entries: dict[str, tk.Entry] = {}
        fields = [
            ("Odkud:", "origin", "Praha", 0, 0),
            ("Kam:", "destination", "Vídeň", 0, 2),
            ("Datum:", "date", now.strftime("%d.%m.%Y"), 0, 4),
            ("Odjezd v:", "time", now.strftime("%H:%M"), 1, 0),
            ("Věk:", "age", "26", 1, 2),
            ("Okno (h):", "window", "12", 1, 4),
        ]
        for label, key, default, row, col in fields:
            tk.Label(grid, text=label, bg=CARD, font=("Segoe UI", 10)).grid(
                row=row, column=col, sticky=tk.W, pady=6)
            entry = tk.Entry(grid, font=("Segoe UI", 10), width=18)
            entry.insert(0, default)
            entry.grid(row=row, column=col + 1, padx=(8, 20))
            self.entries[key] = entry

        checks = tk.Frame(grid, bg=CARD)
        checks.grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(8, 0))
        self.var_isic = tk.BooleanVar(value=True)
        tk.Checkbutton(checks, text="Mám ISIC", variable=self.var_isic, bg=CARD).pack(side=tk.LEFT, padx=(0, 18))
        self.var_pareto = tk.BooleanVar(value=True)
        tk.Checkbutton(checks, text="Skrýt nevýhodné spoje", variable=self.var_pareto,
                       bg=CARD).pack(side=tk.LEFT, padx=(0, 18))
        self.var_direct = tk.BooleanVar(value=False)
        tk.Checkbutton(checks, text="Jen přímé", variable=self.var_direct,
                       bg=CARD).pack(side=tk.LEFT, padx=(0, 18))
        self.var_only_priced = tk.BooleanVar(value=False)
        tk.Checkbutton(checks, text="Jen se známou cenou", variable=self.var_only_priced,
                       bg=CARD).pack(side=tk.LEFT)

        # Dopravci a hacky se berou z registru – nový modul se objeví sám.
        options = card(pad, "Dopravci a travel hacky")
        options.pack(fill=tk.X, pady=(0, 12))
        inner = tk.Frame(options, bg=CARD)
        inner.pack(fill=tk.X, padx=15, pady=(0, 15))

        provider_row = tk.Frame(inner, bg=CARD)
        provider_row.pack(fill=tk.X)
        self.provider_vars: dict[str, tk.BooleanVar] = {}
        for name, cls in PROVIDERS.items():
            # Prohlížečové dopravce necháváme vypnuté – jsou pomalí.
            default = not getattr(cls, "needs_browser", False)
            var = tk.BooleanVar(value=default)
            self.provider_vars[name] = var
            text = name + (" (prohlížeč)" if getattr(cls, "needs_browser", False) else "")
            tk.Checkbutton(provider_row, text=text, variable=var, bg=CARD).pack(side=tk.LEFT, padx=(0, 14))

        hack_row = tk.Frame(inner, bg=CARD)
        hack_row.pack(fill=tk.X, pady=(8, 0))
        self.hack_vars: dict[str, tk.BooleanVar] = {}
        for name, cls in HACKS.items():
            var = tk.BooleanVar(value=(name == "split_ticket"))
            self.hack_vars[name] = var
            tk.Checkbutton(hack_row, text=f"💡 {getattr(cls, 'label', name)}",
                           variable=var, bg=CARD, fg="#d35400").pack(side=tk.LEFT, padx=(0, 14))

        buttons = tk.Frame(pad, bg=BG)
        buttons.pack(fill=tk.X, pady=(0, 12))
        self.btn_time = tk.Button(buttons, text="⏱ HLEDAT DLE ČASU", bg="#3498db", fg="white",
                                  font=("Segoe UI", 11, "bold"), pady=9, bd=0,
                                  command=lambda: self.start_search("departure"))
        self.btn_time.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 8))
        self.btn_price = tk.Button(buttons, text="💰 HLEDAT DLE CENY", bg="#2ecc71", fg="white",
                                   font=("Segoe UI", 11, "bold"), pady=9, bd=0,
                                   command=lambda: self.start_search("price"))
        self.btn_price.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(8, 0))

        self.status = tk.Label(pad, text="Připraven.", bg=BG, fg="#7f8c8d",
                               font=("Segoe UI", 10, "italic"), anchor=tk.W)
        self.status.pack(fill=tk.X, pady=(0, 4))

        # Cenový přehled – kvůli němu se hledá.
        self.summary = tk.Label(pad, text="", bg=BG, fg="#2c3e50", justify=tk.LEFT,
                                font=("Segoe UI", 10), anchor=tk.W)
        self.summary.pack(fill=tk.X, pady=(0, 8))

    def _build_table(self) -> None:
        table = card(self.pad)
        table.pack(fill=tk.BOTH, expand=True)
        columns = ("provider", "line", "dep", "arr", "duration", "transfers", "price")
        headings = [
            ("provider", "Dopravce", 260, tk.W), ("line", "Spoj", 200, tk.W),
            ("dep", "Odjezd", 80, tk.CENTER), ("arr", "Příjezd", 80, tk.CENTER),
            ("duration", "Doba", 90, tk.CENTER), ("transfers", "Přestupy", 80, tk.CENTER),
            ("price", "Cena", 110, tk.E),
        ]
        self.tree = ttk.Treeview(table, columns=columns, show="headings")
        for key, title, width, anchor in headings:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor)
        self.tree.tag_configure("hack", background="#fef9e7")
        self.tree.tag_configure("odd", background="#fbfcfc")
        scroll = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scroll.set)
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.tree.bind("<Double-1>", self._show_detail)

    # ---- hledání -------------------------------------------------------------
    def _collect_query(self) -> SearchQuery | None:
        try:
            age = int(self.entries["age"].get().strip())
            window = float(self.entries["window"].get().strip())
        except ValueError:
            messagebox.showwarning("Neplatný vstup", "Věk a okno musí být čísla.")
            return None

        from .cli import parse_when

        try:
            when = parse_when(self.entries["date"].get().strip(),
                              self.entries["time"].get().strip())
        except SystemExit as exc:
            messagebox.showwarning("Neplatné datum", str(exc))
            return None

        origin = self.entries["origin"].get().strip()
        destination = self.entries["destination"].get().strip()
        if not origin or not destination:
            messagebox.showwarning("Chybí trasa", "Vyplňte odkud a kam.")
            return None

        cards = (DiscountCard.ISIC,) if self.var_isic.get() else ()
        return SearchQuery(
            origin=origin, destination=destination, departure=when,
            passengers=(Passenger(age=age, cards=cards),),
            window=dt.timedelta(hours=window),
        )

    def start_search(self, sort_by: str) -> None:
        if self._searching:
            return
        query = self._collect_query()
        if query is None:
            return

        providers = [n for n, v in self.provider_vars.items() if v.get()]
        if not providers:
            messagebox.showwarning("Bez dopravce", "Vyberte alespoň jednoho dopravce.")
            return
        hacks = [n for n, v in self.hack_vars.items() if v.get()]

        spec: list[Any] = [("departure_window", {"hours": query.window.total_seconds() / 3600})]
        if self.var_direct.get():
            spec.append(("max_transfers", {"limit": 0}))
        spec += ["drop_sold_out", "deduplicate", "price_backfill"]
        if self.var_only_priced.get():
            spec.append("require_price")
        if self.var_pareto.get():
            spec.append("pareto")
        spec.append(("limit", {"count": 60}))

        self._searching = True
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()
        self.btn_time.config(state=tk.DISABLED)
        self.btn_price.config(state=tk.DISABLED)
        self.status.config(text="⏳ Hledám…", fg="#d35400")

        self.runner.submit(self._search(query, providers, hacks, spec, sort_by))

    async def _search(self, query, providers, hacks, spec, sort_by) -> None:
        engine = SearchEngine.build(providers, filter_spec=spec, hack_names=hacks)
        try:
            def partial(result) -> None:
                self.events.put(("partial", result))

            result = await engine.search(query, on_partial=partial,
                                         sort_by=sort_by, enable_hacks=bool(hacks))
            self.events.put(("done", result))
        except Exception as exc:
            log.exception("hledání selhalo")
            self.events.put(("error", str(exc)))
        finally:
            await engine.aclose()

    def _drain_events(self) -> None:
        """Přenáší výsledky z asyncio vlákna do Tkinteru."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "partial":
                    self.status.config(
                        text=f"⏳ {payload.provider}: {len(payload.journeys)} spojů"
                             + (f" – {payload.error}" if payload.error else ""),
                        fg="#d35400")
                elif kind == "done":
                    self._render(payload)
                elif kind == "error":
                    self.status.config(text=f"❌ {payload}", fg="#c0392b")
                    self._finish()
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _render(self, result: SearchResult) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_data.clear()

        for index, journey in enumerate(result.journeys):
            hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
            tags = ("hack",) if journey.is_hack else (("odd",) if index % 2 else ())
            lines = " ➔ ".join(leg.line for leg in journey.legs if leg.line)
            item = self.tree.insert("", tk.END, tags=tags, values=(
                ("💡 " if journey.is_hack else "") + journey.provider,
                lines,
                f"{journey.departure:%H:%M}",
                f"{journey.arrival:%H:%M}",
                f"{hours}h{minutes:02d}m",
                journey.transfers,
                journey.price_label,
            ))
            self.row_data[item] = journey

        self._render_summary(result)

        failed = result.failed_providers
        if failed:
            names = ", ".join(f"{r.provider} ({r.error})" for r in failed)
            self.status.config(text=f"✅ {len(result.journeys)} spojů | ⚠️ selhalo: {names}",
                               fg="#d35400")
        else:
            self.status.config(text=f"✅ Hotovo – {len(result.journeys)} spojů.", fg="#27ae60")
        self._finish()

    def _render_summary(self, result: SearchResult) -> None:
        """Vypíše nejlevnější / nejrychlejší / nejlepší poměr a pokrytí cenami."""
        if not result.journeys:
            self.summary.config(text="")
            return

        def describe(tag: str, journey) -> str:
            hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
            return (f"{tag}: {journey.departure:%H:%M}–{journey.arrival:%H:%M} "
                    f"({hours}h{minutes:02d}m, {journey.price_label}, {journey.provider})")

        parts = []
        cheapest, fastest, value = result.cheapest, result.fastest, result.best_value
        if cheapest is not None:
            parts.append(describe("💰 Nejlevnější", cheapest))
        if fastest is not None and fastest is not cheapest:
            parts.append(describe("⚡ Nejrychlejší", fastest))
        if value is not None and value not in (cheapest, fastest):
            parts.append(describe("⭐ Nejlepší poměr", value))

        saving = result.savings_vs_fastest()
        if saving is not None:
            parts.append(f"Úspora oproti nejrychlejšímu: {saving:.0f} Kč")

        coverage = result.price_coverage
        if coverage < 1.0:
            parts.append(f"⚠️ U {len(result.unpriced)} spojů cenu neznáme "
                         f"(pokrytí {coverage:.0%})")

        self.summary.config(text="   |   ".join(parts))

    def _finish(self) -> None:
        self._searching = False
        self.btn_time.config(state=tk.NORMAL)
        self.btn_price.config(state=tk.NORMAL)

    # ---- detail --------------------------------------------------------------
    def _show_detail(self, _event) -> None:
        item = self.tree.focus()
        journey = self.row_data.get(item)
        if journey is None:
            return

        popup = tk.Toplevel(self)
        popup.title(f"Detail: {journey.origin} ➔ {journey.destination}")
        popup.geometry("680x640")
        popup.configure(bg=CARD)
        text = tk.Text(popup, wrap=tk.WORD, font=("Segoe UI", 10), bg="#f8f9f9",
                       padx=15, pady=15, relief="solid", bd=1)
        text.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        text.tag_configure("title", font=("Segoe UI", 11, "bold"), foreground=ACCENT)
        text.tag_configure("note", font=("Segoe UI", 10, "bold"), foreground="#d35400")
        text.tag_configure("stop", foreground="#27ae60")

        for note in journey.notes:
            text.insert(tk.END, f"{note}\n\n", "note")

        if journey.segments:
            text.insert(tk.END, "Dílčí jízdenky ke koupi:\n", "title")
            for index, segment in enumerate(journey.segments, 1):
                text.insert(
                    tk.END,
                    f"  {index}. {segment.origin} ➔ {segment.destination} "
                    f"({segment.provider}): {segment.price_label}\n",
                )
            text.insert(tk.END, "\n")

        for leg in journey.legs:
            text.insert(tk.END, f"🚆 {leg.line or leg.carrier} "
                                f"({leg.departure:%H:%M} → {leg.arrival:%H:%M})\n", "title")
            for stop in leg.stops:
                times = []
                if stop.arrival:
                    times.append(f"příj. {stop.arrival:%H:%M}")
                if stop.departure:
                    times.append(f"odj. {stop.departure:%H:%M}")
                suffix = f"  ({', '.join(times)})" if times else ""
                text.insert(tk.END, f"   • {stop.name}{suffix}\n", "stop")
            text.insert(tk.END, "\n")

        if journey.deep_link:
            text.insert(tk.END, f"\nOdkaz: {journey.deep_link}\n")
        text.config(state=tk.DISABLED)

    def _on_close(self) -> None:
        self.runner.stop()
        self.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    App().mainloop()


if __name__ == "__main__":
    main()
