"""Diagnostický soubor hledání – vše, co je potřeba k rozboru chyby, jako prostý text.

Po každém hledání si ho jde stáhnout (tlačítko *Diagnostika (.txt)*) a poslat
dál místo snímků obrazovky: dotaz, verze programu, stav a chyby dopravců,
co který dopravce vrátil, výsledné spoje se všemi úseky, jízdenkami a
poznámkami a úvahy hacků (kde se zkoušely lomy jízdenek, u koho a co vyšlo).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..models import Journey, Leg, SearchQuery
from ..normalize import strip_accents

#: Kolik posledních řádků záznamu serveru přiložit.
LOG_LINES = 400


def filename(query: SearchQuery) -> str:
    """„diagnostika_Trebon-Brasov_2026-10-15_0500.txt“ – bez diakritiky a mezer."""
    def slug(text: str) -> str:
        clean = "".join(c if c.isalnum() else "-" for c in strip_accents(text))
        return "-".join(part for part in clean.split("-") if part)[:40] or "misto"

    return (f"diagnostika_{slug(query.origin)}-{slug(query.destination)}_"
            f"{query.departure:%Y-%m-%d_%H%M}.txt")


def _t(value: dt.datetime | None) -> str:
    if value is None:
        return "?"
    offset = value.utcoffset()
    zone = "" if offset is None else f" (UTC{offset.total_seconds() / 3600:+.0f})"
    return f"{value:%d.%m. %H:%M}{zone}"


def _leg(leg: Leg) -> str:
    text = (f"{_t(leg.departure)} {leg.origin} → {_t(leg.arrival)} {leg.destination} | "
            f"{leg.mode.value} {leg.line or '–'} | {leg.carrier or 'dopravce ?'}")
    if leg.stops:
        text += f" | {len(leg.stops)} zastávek"
    if leg.reservation_required:
        text += " | povinná místenka"
    return text


def _journey(journey: Journey, n: int | None = None) -> list[str]:
    head = f"{n}. " if n is not None else ""
    lines = [f"{head}{_t(journey.departure)} → {_t(journey.arrival)} · {int(journey.duration.total_seconds() // 60)} min · "
             f"{journey.transfers} přest. · {journey.provider} · {journey.price_label} "
             f"[{journey.fare_status.value}]{' · HACK' if journey.is_hack else ''}"]
    for leg in journey.legs:
        lines.append(f"     úsek: {_leg(leg)}")
    for fare in journey.fares:
        czk = f" = {fare.amount_czk:.0f} Kč" if fare.amount_czk is not None and fare.currency != "CZK" else ""
        lines.append(f"     cena: {fare.amount} {fare.currency}{czk} | {fare.label} | {fare.tariff}"
                     f"{'' if fare.bookable else ' | NEPRODEJNÉ'}"
                     f"{f' | míst {fare.seats_left}' if fare.seats_left is not None else ''}")
    for seg in journey.segments:
        lines.append(f"     jízdenka: {seg.origin} → {seg.destination} | {seg.provider} | {seg.price_label}"
                     f"{f' | {seg.deep_link}' if seg.deep_link else ''}")
    for note in journey.notes:
        lines.append(f"     pozn.: {note}")
    if journey.deep_link:
        lines.append(f"     odkaz: {journey.deep_link}")
    return lines


def report(session: Any, version: dict[str, Any], ui: str = "", log_file: str = "") -> str:
    """Celý diagnostický text jednoho hledání (``session`` = ``SearchSession``)."""
    q: SearchQuery = session.query
    out: list[str] = [
        "VYHLEDÁVAČ SPOJŮ – diagnostika hledání",
        f"Vytvořeno: {dt.datetime.now():%d.%m.%Y %H:%M:%S}",
        f"Verze: větev {version.get('branch', '?')}, commit {version.get('commit', '?')}, "
        f"otisk {version.get('fingerprint', '?')}",
        "",
        "== Dotaz ==",
        f"Odkud: {q.origin}",
        f"Kam: {q.destination}",
        f"Odjezd od: {_t(q.departure)}, okno do {_t(q.window_end)}",
        "Cestující: " + "; ".join(
            f"{p.age} let" + (f", karty {', '.join(c.value for c in p.cards)}" if p.cards else "")
            + (f", předplatné {dict(p.passes)}" if p.passes else "") for p in q.passengers),
        f"Jen vlaky: {'ano' if q.trains_only else 'ne'}",
        f"Zpáteční: {q.round_trip or 'ne'}",
        f"Dopravci: {', '.join(session.providers)}",
        f"Hacky: {', '.join(session.hacks) or '–'}",
        f"Stav: {'hotovo' if session.done else 'ještě běží'}",
    ]
    if ui:
        out += ["", "== Webové rozhraní ==", ui]

    out += ["", "== Dopravci =="]
    for name in session.providers:
        st = session.status.get(name, {})
        elapsed = f" ({st['elapsed']} s)" if st.get("elapsed") is not None else ""
        out.append(f"{name}: {st.get('status', '?')} – {st.get('detail', '')}{elapsed}")

    if session.problems:
        out += ["", "== Chyby a upozornění =="]
        for p in session.problems:
            out.append(f"[{p['kind']}] {p['provider']}: {p['message']}" + (f"\n    {p['raw']}" if p.get("raw") else ""))

    journeys = session.journeys()
    out += ["", f"== Výsledné spoje ({len(journeys)}) =="]
    for n, journey in enumerate(journeys, 1):
        out += _journey(journey, n)

    out += ["", "== Co vrátili dopravci (před sloučením a filtry) =="]
    for name in session.providers:
        found = session.live.get(name) or []
        if not found:
            continue
        out.append(f"-- {name}: {len(found)} spojů")
        for journey in found:
            rides = [leg for leg in journey.legs if leg.mode.value != "walk"] or journey.legs
            first, last = rides[0], rides[-1]
            out.append(f"   {_t(journey.departure)} {first.origin} → {_t(journey.arrival)} {last.destination} | "
                       f"{' + '.join(leg.line or leg.mode.value for leg in rides)} | {journey.price_label}")

    if session.trace:
        out += ["", f"== Jak program přemýšlel ({len(session.trace)} řádků) =="]
        out += session.trace

    if log_file:
        try:
            with open(log_file, encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-LOG_LINES:]
            out += ["", f"== Záznam serveru (posledních {len(tail)} řádků, {log_file}) =="]
            out += [line.rstrip("\n") for line in tail]
        except OSError:
            pass
    return "\n".join(out) + "\n"
