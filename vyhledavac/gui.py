"""Grafické rozhraní.

Návrh stojí na jednom pravidle: **výpadek jednoho dopravce nesmí být vidět
jako výpadek aplikace**. Proto:

* výsledky se do tabulky přidávají průběžně, jak dorazí jednotlivé zdroje –
  nečeká se na nejpomalejšího;
* každý dopravce má vlastní stavovou kontrolku (čeká / hledá / hotovo /
  chyba / blokováno), takže je na první pohled vidět, kdo dodal data;
* chyby se sbírají do panelu vedle výsledků, ne do modálního okna, které
  by uživateli zabránilo pracovat s tím, co už našel;
* dopravce, který opakovaně selhává, jde jedním klikem vypnout;
* i kdyby selhali všichni, okno zůstane ovladatelné a řekne proč.

Asyncio smyčka běží ve vlastním vlákně; do Tkinteru se sahá výhradně přes
frontu událostí, kterou vyzvedává ``after`` – jiný způsob není bezpečný.
"""

from __future__ import annotations

import datetime as dt
import logging
import queue
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any

from .engine import SearchEngine
from .models import (
    DiscountCard,
    Journey,
    Passenger,
    ProviderResult,
    SearchQuery,
    SearchResult,
)
from .registry import HACKS, LOAD_ERRORS, PROVIDERS, load_plugins
from .runtime import AsyncRunner, explain as _explain, filter_spec, short_label as _short_label

log = logging.getLogger(__name__)

# ---- vzhled -----------------------------------------------------------------
BG = "#eef2f7"
CARD = "#ffffff"
INK = "#1f2d3d"
MUTED = "#6b7a90"
LINE = "#dbe2ea"
ACCENT = "#2563eb"
GOOD = "#15803d"
WARN = "#b45309"
BAD = "#b91c1c"
HACK_BG = "#fff8e1"

STATUS_STYLE = {
    "waiting": ("○", MUTED, "čeká"),
    "running": ("◐", ACCENT, "hledám…"),
    "ok": ("●", GOOD, "hotovo"),
    "empty": ("○", MUTED, "nic nenašel"),
    "error": ("▲", BAD, "chyba"),
    "blocked": ("■", WARN, "blokováno"),
    "off": ("·", LINE, "vypnuto"),
}


def card(parent: tk.Widget, **kw: Any) -> tk.Frame:
    return tk.Frame(parent, bg=CARD, highlightthickness=1,
                    highlightbackground=LINE, bd=0, **kw)


def heading(parent: tk.Widget, text: str) -> tk.Label:
    return tk.Label(parent, text=text, font=("Segoe UI", 10, "bold"),
                    bg=CARD, fg=INK, anchor=tk.W)


class ProviderRow:
    """Řádek s kontrolkou jednoho dopravce."""

    def __init__(self, parent: tk.Widget, name: str, enabled: bool, needs_browser: bool,
                 no_prices: bool) -> None:
        self.name = name
        self.var = tk.BooleanVar(value=enabled)
        self.frame = tk.Frame(parent, bg=CARD)
        self.frame.pack(fill=tk.X, pady=1)

        self.check = tk.Checkbutton(self.frame, text=name, variable=self.var,
                                    bg=CARD, fg=INK, font=("Segoe UI", 10),
                                    activebackground=CARD, anchor=tk.W, width=11)
        self.check.pack(side=tk.LEFT)

        self.dot = tk.Label(self.frame, text="○", bg=CARD, fg=MUTED,
                            font=("Segoe UI", 11))
        self.dot.pack(side=tk.LEFT, padx=(2, 4))

        self.state = tk.Label(self.frame, text="čeká", bg=CARD, fg=MUTED,
                              font=("Segoe UI", 9), anchor=tk.W, width=22)
        self.state.pack(side=tk.LEFT)

        notes = []
        if needs_browser:
            notes.append("prohlížeč")
        if no_prices:
            notes.append("bez cen")
        if notes:
            tk.Label(self.frame, text=" · ".join(notes), bg=CARD, fg=MUTED,
                     font=("Segoe UI", 8)).pack(side=tk.LEFT)

    def set(self, status: str, detail: str = "") -> None:
        symbol, colour, label = STATUS_STYLE.get(status, STATUS_STYLE["waiting"])
        self.dot.config(text=symbol, fg=colour)
        self.state.config(text=detail or label, fg=colour if status != "waiting" else MUTED)

    @property
    def enabled(self) -> bool:
        return bool(self.var.get())

    def disable(self) -> None:
        self.var.set(False)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        load_plugins()
        self.title("Vyhledávač spojů")
        self.geometry("1320x880")
        self.minsize(1080, 700)
        self.configure(bg=BG)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=30,
                        background=CARD, fieldbackground=CARD, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"),
                        background="#f6f8fb", foreground=INK, borderwidth=0)
        style.map("Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", INK)])

        self.runner = AsyncRunner()
        self.events: queue.Queue = queue.Queue()
        self.rows: dict[str, Journey] = {}
        self.provider_rows: dict[str, ProviderRow] = {}
        self.problems: list[ProviderResult] = []
        self.live: list[Journey] = []
        self.query: SearchQuery | None = None
        self.searching = False
        self.sort_by = "departure"
        self._generation = 0

        self._build()
        self.after(120, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- stavba okna ---------------------------------------------------------
    def _build(self) -> None:
        root = tk.Frame(self, bg=BG)
        root.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)

        self._build_search_bar(root)

        body = tk.Frame(root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=290)
        body.rowconfigure(0, weight=1)

        self._build_results(body)
        self._build_sidebar(body)
        self._show_load_errors()

    def _build_search_bar(self, parent: tk.Widget) -> None:
        bar = card(parent)
        bar.pack(fill=tk.X)
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill=tk.X, padx=14, pady=12)

        now = dt.datetime.now()
        self.entries: dict[str, tk.Entry] = {}

        def field(label: str, key: str, default: str, width: int) -> None:
            box = tk.Frame(inner, bg=CARD)
            box.pack(side=tk.LEFT, padx=(0, 12))
            tk.Label(box, text=label, bg=CARD, fg=MUTED,
                     font=("Segoe UI", 8)).pack(anchor=tk.W)
            entry = tk.Entry(box, font=("Segoe UI", 11), width=width,
                             relief="solid", bd=1, highlightthickness=0)
            entry.insert(0, default)
            entry.pack()
            entry.bind("<Return>", lambda _e: self.start_search())
            self.entries[key] = entry

        field("Odkud", "origin", "Praha", 18)
        field("Kam", "destination", "Vídeň", 18)
        field("Datum", "date", now.strftime("%d.%m.%Y"), 11)
        field("Odjezd", "time", now.strftime("%H:%M"), 7)
        field("Věk", "age", "26", 4)

        opts = tk.Frame(inner, bg=CARD)
        opts.pack(side=tk.LEFT, padx=(4, 12))
        tk.Label(opts, text="Sleva", bg=CARD, fg=MUTED,
                 font=("Segoe UI", 8)).pack(anchor=tk.W)
        self.var_isic = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="ISIC", variable=self.var_isic, bg=CARD,
                       activebackground=CARD, font=("Segoe UI", 9)).pack(anchor=tk.W)

        self.btn_search = tk.Button(
            inner, text="Hledat", command=self.start_search, bg=ACCENT, fg="white",
            font=("Segoe UI", 11, "bold"), relief="flat", padx=26, pady=7,
            activebackground="#1d4ed8", activeforeground="white", cursor="hand2")
        self.btn_search.pack(side=tk.LEFT, padx=(4, 0))

        self.btn_cancel = tk.Button(
            inner, text="Zrušit", command=self.cancel_search, bg="#e5e7eb", fg=INK,
            font=("Segoe UI", 10), relief="flat", padx=14, pady=7,
            state=tk.DISABLED, cursor="hand2")
        self.btn_cancel.pack(side=tk.LEFT, padx=(8, 0))

    def _build_results(self, parent: tk.Widget) -> None:
        wrap = card(parent)
        wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        top = tk.Frame(wrap, bg=CARD)
        top.pack(fill=tk.X, padx=14, pady=(12, 6))
        heading(top, "Nalezené spoje").pack(side=tk.LEFT)

        self.sort_label = tk.Label(top, text="řadit:", bg=CARD, fg=MUTED,
                                   font=("Segoe UI", 9))
        self.sort_label.pack(side=tk.LEFT, padx=(16, 4))
        self.sort_choice = ttk.Combobox(
            top, values=["podle odjezdu", "podle ceny", "podle délky"],
            state="readonly", width=15, font=("Segoe UI", 9))
        self.sort_choice.current(0)
        self.sort_choice.pack(side=tk.LEFT)
        self.sort_choice.bind("<<ComboboxSelected>>", self._on_sort_change)

        self.count_label = tk.Label(top, text="", bg=CARD, fg=MUTED,
                                    font=("Segoe UI", 9))
        self.count_label.pack(side=tk.RIGHT)

        # Cenový přehled – hlavní důvod, proč se hledá.
        self.summary = tk.Label(wrap, text="", bg="#f8fafc", fg=INK, justify=tk.LEFT,
                                font=("Segoe UI", 9), anchor=tk.W, padx=12, pady=8)
        self.summary.pack(fill=tk.X, padx=14)

        table = tk.Frame(wrap, bg=CARD)
        table.pack(fill=tk.BOTH, expand=True, padx=14, pady=(8, 14))

        columns = ("dep", "arr", "duration", "transfers", "price", "provider", "line")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for key, title, width, anchor in [
            ("dep", "Odjezd", 70, tk.CENTER), ("arr", "Příjezd", 70, tk.CENTER),
            ("duration", "Doba", 75, tk.CENTER), ("transfers", "Přest.", 55, tk.CENTER),
            ("price", "Cena", 135, tk.E), ("provider", "Dopravce", 200, tk.W),
            ("line", "Spoj", 210, tk.W),
        ]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor)

        self.tree.tag_configure("hack", background=HACK_BG)
        self.tree.tag_configure("odd", background="#fafbfc")
        self.tree.tag_configure("noprice", foreground=MUTED)

        scroll = ttk.Scrollbar(table, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", self._show_detail)

        self.empty_hint = tk.Label(
            wrap, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify=tk.CENTER)

    def _build_sidebar(self, parent: tk.Widget) -> None:
        side = tk.Frame(parent, bg=BG)
        side.grid(row=0, column=1, sticky="nsew")

        providers = card(side)
        providers.pack(fill=tk.X)
        head = tk.Frame(providers, bg=CARD)
        head.pack(fill=tk.X, padx=12, pady=(10, 4))
        heading(head, "Zdroje dat").pack(side=tk.LEFT)
        tk.Button(head, text="vše", command=lambda: self._toggle_all(True), bg=CARD,
                  fg=ACCENT, relief="flat", font=("Segoe UI", 8),
                  cursor="hand2").pack(side=tk.RIGHT)
        tk.Button(head, text="rychlé", command=self._select_fast, bg=CARD, fg=ACCENT,
                  relief="flat", font=("Segoe UI", 8), cursor="hand2").pack(side=tk.RIGHT)

        listing = tk.Frame(providers, bg=CARD)
        listing.pack(fill=tk.X, padx=12, pady=(0, 10))
        for name, cls in PROVIDERS.items():
            needs_browser = bool(getattr(cls, "needs_browser", False))
            self.provider_rows[name] = ProviderRow(
                listing, name,
                enabled=not needs_browser,
                needs_browser=needs_browser,
                no_prices=not getattr(cls, "provides_prices", True),
            )

        extras = card(side)
        extras.pack(fill=tk.X, pady=(12, 0))
        heading(tk.Frame(extras, bg=CARD)).pack() if False else None
        inner = tk.Frame(extras, bg=CARD)
        inner.pack(fill=tk.X, padx=12, pady=10)
        heading(inner, "Možnosti").pack(anchor=tk.W, pady=(0, 6))

        self.var_hacks: dict[str, tk.BooleanVar] = {}
        for name, cls in HACKS.items():
            var = tk.BooleanVar(value=(name == "split_ticket"))
            self.var_hacks[name] = var
            tk.Checkbutton(inner, text=_short_label(getattr(cls, "label", name)),
                           variable=var, bg=CARD, activebackground=CARD,
                           anchor=tk.W, font=("Segoe UI", 9),
                           justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X)

        self.var_only_priced = tk.BooleanVar(value=False)
        tk.Checkbutton(inner, text="Jen spoje se známou cenou",
                       variable=self.var_only_priced, bg=CARD, activebackground=CARD,
                       anchor=tk.W, font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.var_pareto = tk.BooleanVar(value=True)
        tk.Checkbutton(inner, text="Skrýt dražší i pomalejší",
                       variable=self.var_pareto, bg=CARD, activebackground=CARD,
                       anchor=tk.W, font=("Segoe UI", 9)).pack(anchor=tk.W)
        self.var_direct = tk.BooleanVar(value=False)
        tk.Checkbutton(inner, text="Jen přímé spoje", variable=self.var_direct,
                       bg=CARD, activebackground=CARD, anchor=tk.W,
                       font=("Segoe UI", 9)).pack(anchor=tk.W)

        # Panel problémů – nikdy nevyskakuje, jen se plní.
        self.problem_card = card(side)
        self.problem_inner = tk.Frame(self.problem_card, bg=CARD)
        self.problem_inner.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)
        self.problem_title = heading(self.problem_inner, "Co se nepovedlo")
        self.problem_title.pack(anchor=tk.W)
        self.problem_body = tk.Frame(self.problem_inner, bg=CARD)
        self.problem_body.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    # ---- ovládání ------------------------------------------------------------
    def _toggle_all(self, value: bool) -> None:
        for row in self.provider_rows.values():
            row.var.set(value)

    def _select_fast(self) -> None:
        """Jen zdroje bez prohlížeče – rychlé a spolehlivé."""
        for name, row in self.provider_rows.items():
            cls = PROVIDERS.try_get(name)
            row.var.set(not getattr(cls, "needs_browser", False))

    def _on_sort_change(self, _event=None) -> None:
        self.sort_by = {
            "podle odjezdu": "departure",
            "podle ceny": "price",
            "podle délky": "duration",
        }[self.sort_choice.get()]
        if self.live:
            self._repaint()

    def _collect_query(self) -> SearchQuery | None:
        from .cli import parse_when

        origin = self.entries["origin"].get().strip()
        destination = self.entries["destination"].get().strip()
        if not origin or not destination:
            messagebox.showwarning("Chybí trasa", "Vyplňte odkud a kam.")
            return None
        try:
            age = int(self.entries["age"].get().strip())
        except ValueError:
            messagebox.showwarning("Neplatný věk", "Věk musí být číslo.")
            return None
        try:
            when = parse_when(self.entries["date"].get().strip(),
                              self.entries["time"].get().strip())
        except SystemExit as exc:
            messagebox.showwarning("Neplatné datum", str(exc))
            return None

        cards = (DiscountCard.ISIC,) if self.var_isic.get() else ()
        return SearchQuery(origin=origin, destination=destination, departure=when,
                           passengers=(Passenger(age=age, cards=cards),))

    def start_search(self) -> None:
        if self.searching:
            return
        query = self._collect_query()
        if query is None:
            return
        providers = [n for n, r in self.provider_rows.items() if r.enabled]
        if not providers:
            messagebox.showwarning("Bez zdrojů", "Vyberte alespoň jeden zdroj dat.")
            return

        self.query = query
        self.live = []
        self.problems = []
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self._clear_problems()
        lead = query.lead
        if lead.has(DiscountCard.ISIC) and lead.age >= 26:
            self._add_problem("ISIC", "studentská sleva platí jen do 26 let – hledám "
                              "ceny pro dospělého. Jste-li mladší, opravte věk.",
                              blocked=False, soft=True)
        self.summary.config(text="")
        self.count_label.config(text="")
        self._generation += 1

        for name, row in self.provider_rows.items():
            row.set("waiting" if name in providers else "off")

        self.searching = True
        self.btn_search.config(state=tk.DISABLED, text="Hledám…")
        self.btn_cancel.config(state=tk.NORMAL)

        hacks = [n for n, v in self.var_hacks.items() if v.get()]
        self.runner.submit(self._run(query, providers, hacks, self._generation))

    def cancel_search(self) -> None:
        """Ukončí čekání na dopravce. Co už dorazilo, zůstane na obrazovce."""
        self._generation += 1
        self._finish(cancelled=True)

    # ---- běh hledání ---------------------------------------------------------
    def _filter_spec(self) -> list[Any]:
        return filter_spec(direct=self.var_direct.get(),
                           only_priced=self.var_only_priced.get(),
                           pareto=self.var_pareto.get())

    async def _run(self, query, providers, hacks, generation) -> None:
        engine = SearchEngine.build(providers, filter_spec=self._filter_spec(),
                                    hack_names=hacks)
        for name in providers:
            self.events.put((generation, "provider", (name, "running", "")))
        try:
            def partial(result: ProviderResult) -> None:
                self.events.put((generation, "partial", result))

            def progress(result: ProviderResult) -> None:
                self.events.put((generation, "progress", result))

            result = await engine.search(query, on_partial=partial, on_progress=progress,
                                         sort_by=self.sort_by,
                                         enable_hacks=bool(hacks))
            self.events.put((generation, "done", result))
        except Exception as exc:
            log.exception("hledání selhalo")
            self.events.put((generation, "fatal", str(exc)))
        finally:
            await engine.aclose()

    def _drain(self) -> None:
        """Přenáší události z asyncio vlákna do Tkinteru."""
        try:
            while True:
                generation, kind, payload = self.events.get_nowait()
                if generation != self._generation:
                    continue          # výsledek zrušeného hledání
                if kind == "provider":
                    name, status, detail = payload
                    row = self.provider_rows.get(name)
                    if row:
                        row.set(status, detail)
                elif kind == "partial":
                    self._on_partial(payload)
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "fatal":
                    self._add_problem("Vyhledávání", str(payload), False)
                    self._finish()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _on_partial(self, result: ProviderResult) -> None:
        """Dorazil jeden dopravce – hned ho promítneme."""
        row = self.provider_rows.get(result.provider)
        if result.not_served:
            # Dopravce tam prostě nejezdí – žádná porucha, jen informace.
            if row:
                row.set("empty", "nejezdí tam")
            self._add_problem(result.provider, result.error or "do této destinace nejezdí",
                              blocked=False, soft=True)
            return
        if row:
            if result.error:
                row.set("blocked" if result.blocked else "error",
                        "blokováno" if result.blocked else "chyba")
            elif result.journeys:
                extra = " · s výhradou" if result.notes else ""
                row.set("ok", f"{len(result.journeys)} spojů · {result.elapsed:.0f}s{extra}")
            else:
                row.set("empty")

        if result.error:
            self.problems.append(result)
            self._add_problem(result.provider, result.error, result.blocked)
        elif result.notes:
            for note in result.notes:
                self._add_problem(result.provider, note, blocked=False, soft=True)

        # Dopravce mohl už dřív poslat průběžný stav – nahradit, ne přidat.
        self.live = [j for j in self.live if j.provider != result.provider]
        if result.journeys:
            self.live.extend(result.journeys)
        self._repaint()

    def _on_progress(self, result: ProviderResult) -> None:
        """Průběžný stav pomalého dopravce: spoje hned, ceny postupně."""
        row = self.provider_rows.get(result.provider)
        if row:
            priced = sum(1 for j in result.journeys if j.price is not None)
            row.set("running", f"{len(result.journeys)} spojů · ceny {priced}/"
                               f"{len(result.journeys)}…")
        self.live = [j for j in self.live if j.provider != result.provider]
        self.live.extend(result.journeys)
        self._repaint()

    def _on_done(self, result: SearchResult) -> None:
        self.live = list(result.journeys)
        self._repaint(final=result)
        self._finish()

    # ---- vykreslování --------------------------------------------------------
    def _repaint(self, final: SearchResult | None = None) -> None:
        """Překreslí tabulku z toho, co je zatím k dispozici."""
        if self.query is None:
            return

        if final is not None:
            journeys = final.journeys
            summary_source = final
        else:
            # Průběžný stav: použijeme stejné filtry, ale nad tím, co už máme.
            from .filters.base import FilterPipeline
            from .models import SearchResult as SR, sort_journeys

            pipeline = FilterPipeline.from_spec(self._filter_spec())
            journeys = sort_journeys(pipeline.apply(list(self.live), self.query),
                                     by=self.sort_by)
            summary_source = SR(query=self.query, journeys=journeys)

        self.tree.delete(*self.tree.get_children())
        self.rows.clear()

        for index, journey in enumerate(journeys):
            hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
            tags: tuple[str, ...] = ("hack",) if journey.is_hack else (
                ("odd",) if index % 2 else ())
            if journey.price is None:
                tags += ("noprice",)
            lines = " ➔ ".join(leg.line for leg in journey.legs if leg.line)
            item = self.tree.insert("", tk.END, tags=tags, values=(
                f"{journey.departure:%H:%M}",
                f"{journey.arrival:%H:%M}",
                f"{hours}h{minutes:02d}",
                journey.transfers or "—",
                journey.price_label,
                ("💡 " if journey.is_hack else "") + journey.provider,
                lines,
            ))
            self.rows[item] = journey

        self.count_label.config(text=f"{len(journeys)} spojů")
        self._render_summary(summary_source)
        self._render_empty_hint(journeys)

    def _render_empty_hint(self, journeys) -> None:
        """Když nic není, vysvětlíme proč – prázdná tabulka nestačí."""
        if journeys:
            self.empty_hint.pack_forget()
            return
        if self.searching:
            text = "Hledám…"
        elif self.problems:
            names = ", ".join(sorted({p.provider for p in self.problems}))
            text = (f"Zatím nic. Nepodařilo se načíst: {names}.\n"
                    "Zkuste jiné zdroje vlevo, nebo hledání opakujte.")
        else:
            text = ("Žádné spoje pro zadané parametry.\n"
                    "Zkuste jiný čas, delší okno nebo víc zdrojů.")
        self.empty_hint.config(text=text)
        self.empty_hint.pack(pady=18)

    def _render_summary(self, result: SearchResult) -> None:
        if not result.journeys:
            self.summary.config(text="")
            return

        def describe(tag: str, journey: Journey) -> str:
            hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
            return (f"{tag} {journey.departure:%H:%M}–{journey.arrival:%H:%M} · "
                    f"{hours}h{minutes:02d} · {journey.price_label} · {journey.provider}")

        parts = []
        cheapest, fastest, value = result.cheapest, result.fastest, result.best_value
        if cheapest is not None:
            parts.append(describe("💰 nejlevnější:", cheapest))
        if fastest is not None and fastest is not cheapest:
            parts.append(describe("⚡ nejrychlejší:", fastest))
        if value is not None and value not in (cheapest, fastest):
            parts.append(describe("⭐ nejlepší poměr:", value))

        saving = result.savings_vs_fastest()
        extra = []
        if saving is not None:
            extra.append(f"úspora oproti nejrychlejšímu: {saving:.0f} Kč")
        coverage = result.price_coverage
        if coverage < 1.0:
            extra.append(f"⚠️ u {len(result.unpriced)} spojů cenu neznáme "
                         f"({coverage:.0%} pokrytí)")

        text = "\n".join(parts)
        if extra:
            text += ("\n" if text else "") + "   ·   ".join(extra)
        self.summary.config(text=text)

    # ---- panel problémů ------------------------------------------------------
    def _clear_problems(self) -> None:
        for child in self.problem_body.winfo_children():
            child.destroy()
        self.problem_card.pack_forget()
        self._show_load_errors()

    def _show_load_errors(self) -> None:
        """Dopravce, jehož modul se nenačetl, v nabídce chybí – řekneme proč."""
        for module, error in LOAD_ERRORS.items():
            name = module.rsplit(".", 1)[-1]
            self._add_problem(name, f"modul se nenačetl, v nabídce proto chybí ({error})",
                              blocked=False, soft=True)

    def _add_problem(self, provider: str, message: str, blocked: bool,
                     soft: bool = False) -> None:
        """Chyba se jen zapíše do panelu – nic nevyskakuje a nic neblokuje.

        ``soft`` je dílčí potíž (dopravce spoje dodal, jen něco chybí) –
        bez tlačítka "vypnout", protože vypnutím by se přišlo i o spoje.
        """
        self.problem_card.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        box = tk.Frame(self.problem_body, bg=CARD)
        box.pack(fill=tk.X, pady=(0, 8))

        title = tk.Frame(box, bg=CARD)
        title.pack(fill=tk.X)
        tk.Label(title, text=("● " if soft else "■ " if blocked else "▲ ") + provider,
                 bg=CARD, fg=WARN if (blocked or soft) else BAD,
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        row = self.provider_rows.get(provider)
        if row is not None and not soft:
            tk.Button(title, text="vypnout", cursor="hand2", bg=CARD, fg=ACCENT,
                      relief="flat", font=("Segoe UI", 8),
                      command=row.disable).pack(side=tk.RIGHT)

        hint = message if soft else _explain(message, blocked)
        tk.Label(box, text=hint, bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                 wraplength=250, justify=tk.LEFT).pack(anchor=tk.W)

    def _finish(self, cancelled: bool = False) -> None:
        self.searching = False
        self.btn_search.config(state=tk.NORMAL, text="Hledat")
        self.btn_cancel.config(state=tk.DISABLED)
        if cancelled:
            for row in self.provider_rows.values():
                if row.state.cget("text") in ("hledám…", "čeká"):
                    row.set("off", "zrušeno")
        self._render_empty_hint(list(self.rows.values()))

    # ---- detail --------------------------------------------------------------
    def _show_detail(self, _event=None) -> None:
        item = self.tree.focus()
        journey = self.rows.get(item)
        if journey is None:
            return

        popup = tk.Toplevel(self)
        popup.title(f"{journey.origin} → {journey.destination}")
        popup.geometry("700x660")
        popup.configure(bg=CARD)

        header = tk.Frame(popup, bg=CARD)
        header.pack(fill=tk.X, padx=22, pady=(20, 8))
        hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
        tk.Label(header, text=f"{journey.departure:%H:%M} → {journey.arrival:%H:%M}",
                 font=("Segoe UI", 16, "bold"), bg=CARD, fg=INK).pack(side=tk.LEFT)
        tk.Label(header, text=f"  {hours}h{minutes:02d} · {journey.price_label}",
                 font=("Segoe UI", 12), bg=CARD, fg=MUTED).pack(side=tk.LEFT)

        text = tk.Text(popup, wrap=tk.WORD, font=("Segoe UI", 10), bg="#f8fafc",
                       relief="flat", padx=16, pady=14, bd=0)
        text.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 12))
        text.tag_configure("note", foreground=WARN, font=("Segoe UI", 10, "bold"))
        text.tag_configure("train", foreground=INK, font=("Segoe UI", 11, "bold"))
        text.tag_configure("stop", foreground=GOOD)
        text.tag_configure("muted", foreground=MUTED)

        for note in journey.notes:
            text.insert(tk.END, f"{note}\n\n", "note")

        if journey.segments:
            text.insert(tk.END, "Jízdenky ke koupi:\n", "train")
            for index, segment in enumerate(journey.segments, 1):
                text.insert(tk.END, f"  {index}. {segment.origin} → {segment.destination}"
                                    f"  ({segment.provider}): {segment.price_label}\n")
            text.insert(tk.END, "\n")

        for leg in journey.legs:
            text.insert(tk.END, f"{leg.line or leg.carrier}  "
                                f"{leg.departure:%H:%M} → {leg.arrival:%H:%M}\n", "train")
            for stop in leg.stops:
                times = []
                if stop.arrival:
                    times.append(f"příj. {stop.arrival:%H:%M}")
                if stop.departure:
                    times.append(f"odj. {stop.departure:%H:%M}")
                suffix = f"   ({', '.join(times)})" if times else ""
                text.insert(tk.END, f"   • {stop.name}{suffix}\n", "stop")
            text.insert(tk.END, "\n")
        text.config(state=tk.DISABLED)

        if journey.deep_link:
            link = journey.deep_link
            tk.Button(popup, text="Otevřít u dopravce", bg=ACCENT, fg="white",
                      relief="flat", font=("Segoe UI", 10, "bold"), pady=7,
                      cursor="hand2",
                      command=lambda: webbrowser.open(link)).pack(
                fill=tk.X, padx=22, pady=(0, 18))

    def _close(self) -> None:
        self.runner.stop()
        self.destroy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    App().mainloop()


if __name__ == "__main__":
    main()
