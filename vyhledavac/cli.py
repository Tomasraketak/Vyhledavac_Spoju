"""Příkazová řádka.

    python -m vyhledavac Praha Vídeň --date 01.10.2026 --time 08:00 --isic
    python -m vyhledavac --list-providers
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from typing import Any, Sequence

from .engine import SearchEngine
from .models import DEFAULT_TZ, DiscountCard, Passenger, SearchQuery, SearchResult
from .registry import FILTERS, HACKS, PROVIDERS, load_plugins

DEFAULT_PROVIDERS = ["RegioJet", "FlixBus", "ÖBB", "Bileto", "Arriva", "PKP", "MÁV", "DB", "NS", "Trenitalia",
                     "OUIGO", "Renfe", "Eurostar", "EuropeanSleeper", "CFR", "SŽ", "HŽPP", "LeoExpress", "IDS"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vyhledavac",
        description="Metavyhledávač vlaků a autobusů v Evropě.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Příklady:\n"
            "  python -m vyhledavac Praha Brno\n"
            "  python -m vyhledavac Praha Vídeň --date 01.10.2026 --isic --sort price\n"
            "  python -m vyhledavac Praha Vídeň --hacks split_ticket --max-price 800\n"
        ),
    )
    parser.add_argument("origin", nargs="?", help="odkud")
    parser.add_argument("destination", nargs="?", help="kam")
    parser.add_argument("--date", help="datum odjezdu DD.MM.RRRR (výchozí dnes)")
    parser.add_argument("--time", help="čas odjezdu HH:MM (výchozí teď)")
    parser.add_argument("--age", type=int, default=26, help="věk cestujícího")
    parser.add_argument("--isic", action="store_true", help="mám ISIC")
    parser.add_argument("--card", action="append", default=[],
                        help="další slevová karta (lze opakovat)")
    parser.add_argument("--providers", nargs="+", default=DEFAULT_PROVIDERS,
                        help=f"dopravci (výchozí: {' '.join(DEFAULT_PROVIDERS)})")
    parser.add_argument("--hacks", nargs="*", default=["split_ticket", "ids_ticket", "idos_complete", "obb_partial", "hub_transfer",
                                                        "cd_europe_border", "cd_europe_beyond",
                                                        "regional_pass"],
                        help="travel hacky; prázdné = vypnuto")
    parser.add_argument("--sort", choices=["departure", "price", "duration"],
                        default="departure", help="řazení výsledků")
    parser.add_argument("--window", type=float, default=12.0,
                        help="kolik hodin za zadaný čas ještě hledat")
    parser.add_argument("--max-price", type=float, help="cenový strop v Kč")
    parser.add_argument("--max-transfers", type=int, help="nejvýše N přestupů")
    parser.add_argument("--max-duration", type=float, help="nejdelší doba jízdy v hodinách")
    parser.add_argument("--limit", type=int, default=25, help="počet výsledků")
    parser.add_argument("--pareto", action="store_true",
                        help="skrýt spoje, které jsou zároveň dražší i pomalejší")
    parser.add_argument("--only-priced", action="store_true",
                        help="zobrazit jen spoje se známou cenou")
    parser.add_argument("--no-backfill", action="store_true",
                        help="nedoplňovat ceny od dopravců, kteří tentýž vlak prodávají")
    parser.add_argument("--json", action="store_true", help="výstup ve formátu JSON")
    parser.add_argument("--list-providers", action="store_true",
                        help="vypsat dostupné dopravce, filtry a hacky")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="podrobnější log (-vv pro ladění)")
    return parser


def parse_when(date_text: str | None, time_text: str | None) -> dt.datetime:
    now = dt.datetime.now(DEFAULT_TZ)
    if date_text:
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m."):
            try:
                parsed = dt.datetime.strptime(date_text, fmt)
                if fmt == "%d.%m.":
                    parsed = parsed.replace(year=now.year)
                day = parsed.date()
                break
            except ValueError:
                continue
        else:
            raise SystemExit(f"nerozumím datu {date_text!r} (čekám DD.MM.RRRR)")
    else:
        day = now.date()

    if time_text:
        try:
            clock = dt.datetime.strptime(time_text, "%H:%M").time()
        except ValueError:
            raise SystemExit(f"nerozumím času {time_text!r} (čekám HH:MM)") from None
    else:
        clock = now.time().replace(second=0, microsecond=0)

    return dt.datetime.combine(day, clock).replace(tzinfo=DEFAULT_TZ)


def build_filters(args: argparse.Namespace) -> list[Any]:
    spec: list[Any] = [("departure_window", {"hours": args.window})]
    if args.max_price is not None:
        spec.append(("max_price", {"limit": args.max_price}))
    if args.max_transfers is not None:
        spec.append(("max_transfers", {"limit": args.max_transfers}))
    if args.max_duration is not None:
        spec.append(("max_duration", {"hours": args.max_duration}))
    spec.append("drop_sold_out")
    spec.append("deduplicate")
    if not args.no_backfill:
        # Musí být až za deduplikací – ta spáruje stejné spoje od více
        # dopravců a backfill pak doplní, co po ní zbylo bez ceny.
        spec.append("price_backfill")
    if args.only_priced:
        spec.append("require_price")
    if args.pareto:
        spec.append("pareto")
    spec.append(("limit", {"count": args.limit}))
    return spec


def build_passenger(args: argparse.Namespace) -> Passenger:
    cards: list[DiscountCard] = []
    if args.isic:
        cards.append(DiscountCard.ISIC)
    for raw in args.card:
        try:
            cards.append(DiscountCard(raw.lower()))
        except ValueError:
            valid = ", ".join(c.value for c in DiscountCard)
            raise SystemExit(f"neznámá karta {raw!r}. Platné: {valid}") from None
    return Passenger(age=args.age, cards=tuple(cards))


def print_listing() -> None:
    load_plugins()
    print("Dopravci:")
    for name, cls in PROVIDERS.items():
        flags = []
        if getattr(cls, "needs_browser", False):
            flags.append("vyžaduje prohlížeč")
        if not getattr(cls, "provides_prices", True):
            flags.append("bez cen")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {name:<12} {getattr(cls, 'label', '')}{suffix}")
    print("\nFiltry:")
    for name, cls in FILTERS.items():
        print(f"  {name:<20} {getattr(cls, 'label', '')}")
    print("\nTravel hacky:")
    for name, cls in HACKS.items():
        print(f"  {name:<20} {getattr(cls, 'label', '')}")


def render(result: SearchResult, as_json: bool) -> None:
    if as_json:
        import json

        payload = [
            {
                "provider": j.provider,
                "departure": j.departure.isoformat(),
                "arrival": j.arrival.isoformat(),
                "duration_minutes": int(j.duration.total_seconds() // 60),
                "transfers": j.transfers,
                "price_czk": float(j.price) if j.price is not None else None,
                "price_label": j.price_label,
                "legs": [
                    {"from": leg.origin, "to": leg.destination, "line": leg.line,
                     "mode": leg.mode.value,
                     "departure": leg.departure.isoformat(),
                     "arrival": leg.arrival.isoformat()}
                    for leg in j.legs
                ],
                "notes": j.notes,
                "deep_link": j.deep_link,
            }
            for j in result.journeys
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not result.journeys:
        print("Nenašel jsem žádné spojení.")
    else:
        header = f"{'ODJEZD':<7} {'PŘÍJEZD':<8} {'DOBA':<7} {'PŘ.':<4} {'CENA':>10}  DOPRAVCE / SPOJ"
        print(header)
        print("─" * len(header))
        for journey in result.journeys:
            hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
            lines = " ➔ ".join(leg.line for leg in journey.legs if leg.line)
            mark = "💡 " if journey.is_hack else ""
            print(
                f"{journey.departure:%H:%M}   {journey.arrival:%H:%M}    "
                f"{hours}h{minutes:02d}m  {journey.transfers:<4} "
                f"{journey.price_label:>10}  {mark}{journey.provider}"
            )
            if lines:
                print(f"{'':>34}  {lines}")
            for note in journey.notes:
                print(f"{'':>34}  {note}")

    _print_price_summary(result)

    noted = [r for r in result.provider_results if r.notes and not r.error]
    if noted:
        print("\nUpozornění:")
        for entry in noted:
            for note in entry.notes:
                print(f"  • {entry.provider}: {note}")

    failed = result.failed_providers
    if failed:
        print("\nNepodařilo se získat data od:")
        for entry in failed:
            tag = " (blokováno ochranou)" if entry.blocked else ""
            print(f"  • {entry.provider}: {entry.error}{tag}")


def _print_price_summary(result: SearchResult) -> None:
    """Cenový přehled – kvůli tomu se celé hledání dělá."""
    if not result.journeys:
        return

    def line(tag: str, journey) -> str:
        hours, minutes = divmod(int(journey.duration.total_seconds() // 60), 60)
        return (f"  {tag:<12} {journey.departure:%H:%M}–{journey.arrival:%H:%M}  "
                f"{hours}h{minutes:02d}m  {journey.price_label:>10}  {journey.provider}")

    print("\nCENOVÝ PŘEHLED")
    cheapest, fastest, value = result.cheapest, result.fastest, result.best_value
    if cheapest is not None:
        print(line("nejlevnější", cheapest))
    if fastest is not None and fastest is not cheapest:
        print(line("nejrychlejší", fastest))
    if value is not None and value not in (cheapest, fastest):
        print(line("nejlepší poměr", value))

    saving = result.savings_vs_fastest()
    if saving is not None:
        print(f"  → nejlevnějším spojem ušetříte {saving:.0f} Kč oproti nejrychlejšímu")

    by_provider = result.provider_prices()
    if len(by_provider) > 1:
        parts = ", ".join(f"{name} od {price:.0f} Kč" for name, price in by_provider.items())
        print(f"  Nejnižší cena podle dopravce: {parts}")

    coverage = result.price_coverage
    if coverage < 1.0:
        missing = len(result.unpriced)
        print(f"  ⚠️ U {missing} z {len(result.journeys)} spojů cenu neznáme "
              f"(pokrytí {coverage:.0%}) – podle ceny je neporovnáváte.")


async def run(args: argparse.Namespace) -> int:
    query = SearchQuery(
        origin=args.origin,
        destination=args.destination,
        departure=parse_when(args.date, args.time),
        passengers=(build_passenger(args),),
        window=dt.timedelta(hours=args.window),
        max_results=args.limit,
    )

    engine = SearchEngine.build(
        args.providers,
        filter_spec=build_filters(args),
        hack_names=[h for h in (args.hacks or []) if h],
    )
    if not engine.providers:
        print("Nebyl vybrán žádný platný dopravce.", file=sys.stderr)
        return 2

    try:
        result = await engine.search(query, sort_by=args.sort,
                                     enable_hacks=bool(args.hacks))
    finally:
        await engine.aclose()

    render(result, args.json)
    return 0 if result.journeys else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.list_providers:
        print_listing()
        return 0

    if not args.origin or not args.destination:
        parser.print_help()
        return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nPřerušeno.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
