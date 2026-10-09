"""Simple view: one panel per stock, plain English, no jargon."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis.metrics import finite, fmt_money
from ..models import Report
from .glossary import PILLARS, pillar_word, summarize
from .style import (
    ACCENT,
    BAD,
    GOOD,
    MUTED,
    SIGNAL_FG,
    Symbols,
    badge,
    bar,
    bullet_list,
    gauge,
    money,
    symbols,
)


def _section(title: str) -> Text:
    return Text(title, style=f"bold {ACCENT}")


def _header(r: Report, sym: Symbols) -> Text:
    meta = [m for m in (r.sector, r.industry) if m]
    t = Text.assemble((r.name, "bold"), "  ", (r.ticker, f"bold {ACCENT}"))
    if meta:
        t.append(f"\n{f' {sym.dot} '.join(meta)}", style=MUTED)
    t.append(f"\nPrice {money(r.price, r.currency)}", style="bold")
    if finite(r.market_cap):
        t.append(f"  {sym.dot}  Market value {fmt_money(r.market_cap, r.currency)}", style=MUTED)
    return t


def build_simple(r: Report, ascii_only: bool = False, width: int = 110) -> Panel:
    sym = symbols(ascii_only)
    s = summarize(r)
    d = r.decision
    v = r.valuation
    parts: list[RenderableType] = [_header(r, sym), Text()]

    verdict = Text.assemble(("Verdict  ", "bold"), badge(d.signal))
    verdict.append(f"   Confidence: {d.confidence.title()}", style=MUTED)
    parts += [verdict, Text(s.headline, style=SIGNAL_FG.get(d.signal, "")), Text()]

    # ---- value ------------------------------------------------------------------------
    parts.append(_section("What is it worth?"))
    if v.fair_value:
        lo = min(x for x in (v.bear, v.fair_value) if finite(x))
        hi = max(x for x in (v.bull, v.fair_value) if finite(x))
        line = Text.assemble(
            "  Fair value  ", (money(v.fair_value, r.currency, 0), f"bold {ACCENT}"), " per share",
            (f"   (range {money(lo, r.currency, 0)} to {money(hi, r.currency, 0)})", MUTED),
        )  # fmt: skip
        price_style = GOOD if r.price <= v.fair_value else BAD
        parts += [line, Text.assemble("  Price       ", (money(r.price, r.currency, 0), f"bold {price_style}"),
                                      f"   {sym.arrow} {s.value}")]  # fmt: skip
        g = gauge(v.bear, v.bull, v.fair_value, r.price, r.currency, sym, width=40)
        if g:
            parts += [Text(), *g]
    else:
        parts.append(Text(f"  {s.value}", style=MUTED))
    if s.expectation:
        parts += [Text(), Padding(Text(s.expectation, style=MUTED), (0, 2))]
    parts.append(Text())

    # ---- scorecard --------------------------------------------------------------------
    parts.append(_section("Scorecard"))
    show_hints = width >= 96
    bar_width = 20 if width >= 80 else 12
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True, overflow="crop")
    grid.add_column(no_wrap=True, justify="right")
    grid.add_column(no_wrap=True)
    if show_hints:
        grid.add_column()
    for key, (name, hint) in PILLARS.items():
        score = d.components.get(key)
        row = [
            Text(f"  {name}"),
            bar(score, sym, bar_width),
            Text(f"{score:.0f}" if score is not None else "n/a", style="bold"),
            Text(pillar_word(key, score), style="bold"),
        ]
        if show_hints:
            row.append(Text(hint, style=MUTED))
        grid.add_row(*row)
    parts += [grid, Text()]

    # ---- strengths / concerns ---------------------------------------------------------
    if s.strengths:
        parts.append(_section("Strengths"))
        parts.append(bullet_list(s.strengths, sym.passed, f"bold {GOOD}", indent=2))
        parts.append(Text())
    if s.concerns:
        parts.append(_section("Concerns"))
        parts.append(bullet_list(s.concerns, sym.failed, f"bold {BAD}", indent=2))
        parts.append(Text())

    parts.append(Text.assemble(("Timing  ", "bold"), (d.timing, "")))
    parts.append(Text(f"Confidence  {s.confidence}", style=MUTED))

    footer = Text(f" detailed view has every number {sym.dot} not investment advice ", style=MUTED)
    return Panel(Group(*parts), box=sym.box, border_style=ACCENT, padding=(1, 2), subtitle=footer,
                 subtitle_align="right")  # fmt: skip


def render_simple(r: Report, console: Console, ascii_only: bool = False) -> None:
    width = min(console.width, 110)
    console.print(build_simple(r, ascii_only, width), width=width)


def render_comparison(reports: list[Report], console: Console, ascii_only: bool = False) -> None:
    """One-line-per-stock summary when several tickers are analysed together."""
    sym = symbols(ascii_only)
    console.print()
    t = Table(box=sym.table_box, header_style=f"bold {ACCENT}", title="Side by side", title_style="bold",
              pad_edge=False)  # fmt: skip
    for col, justify in (("Ticker", "left"), ("Verdict", "left"), ("Price", "right"), ("Fair value", "right"),
                         ("Price vs value", "right"), ("Quality", "right"), ("Strength", "right"),
                         ("Confidence", "left")):  # fmt: skip
        t.add_column(col, justify=justify, no_wrap=True)
    for r in reports:
        v, d = r.valuation, r.decision
        gap = Text("n/a", style=MUTED)
        if v.fair_value:
            ratio = r.price / v.fair_value - 1
            gap = Text(f"{ratio:+.0%}", style=GOOD if ratio <= 0 else BAD)
        q, sa = d.components.get("quality"), d.components.get("safety")
        t.add_row(
            Text(r.ticker, style="bold"),
            badge(d.signal),
            money(r.price, r.currency),
            money(v.fair_value, r.currency),
            gap,
            Text(f"{q:.0f}" if q is not None else "n/a"),
            Text(f"{sa:.0f}" if sa is not None else "n/a"),
            Text(d.confidence.title(), style=MUTED),
        )
    console.print(t)
    console.print(Text("Price vs value: negative = trading below our estimate (cheaper).", style=MUTED))
