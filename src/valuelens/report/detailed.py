"""Detailed view: every number, grouped in panels, each with a 'what it means' note."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis.metrics import finite, fmt_money, pct
from ..models import Report, Scorecard, Status
from .glossary import HEALTH_MEANING, MEANING, METHOD_MEANING, PILLARS, pillar_word, summarize
from .style import (
    ACCENT,
    BAD,
    GOOD,
    MUTED,
    WARN,
    Symbols,
    badge,
    bar,
    bullet_list,
    gauge,
    money,
    pretty_label,
    status_cell,
    symbols,
)

DISCLAIMER = "Educational only. Not investment advice. Do your own research and consult a professional."


def _panel(body: RenderableType, title: str, sym: Symbols, subtitle: Text | None = None) -> Panel:
    return Panel(
        body,
        title=Text(f" {title} ", style=f"bold {ACCENT}"),
        title_align="left",
        subtitle=subtitle,
        subtitle_align="right",
        box=sym.box,
        border_style="grey42",
        padding=(1, 2),
    )


def _table(sym: Symbols, *columns: tuple[str, dict]) -> Table:
    t = Table(box=sym.table_box, header_style=f"bold {MUTED}", pad_edge=False, expand=True, show_edge=False)
    for name, opts in columns:
        t.add_column(name, **opts)
    return t


def _score_text(card: Scorecard, kinds: tuple[str, ...] | None = None) -> Text:
    n, p = card.evaluated(kinds), card.passed(kinds)
    if not n:
        return Text("n/a", style=MUTED)
    ratio = p / n
    style = GOOD if ratio >= 0.7 else WARN if ratio >= 0.45 else BAD
    return Text.assemble((f"{p}/{n}", f"bold {style}"), (f" passed ({ratio:.0%})", MUTED))


def _mos_text(mos: float | None) -> Text:
    if mos is None:
        return Text("n/a", style=MUTED)
    if mos >= 0:
        return Text(f"{mos:.0%} below value (margin of safety)", style=f"bold {GOOD}")
    return Text(f"{-mos:.0%} above value (no margin of safety)", style=f"bold {BAD}")


def _criteria_table(card: Scorecard, sym: Symbols, ascii_only: bool) -> Table:
    t = _table(
        sym,
        ("", {"width": 1, "no_wrap": True}),
        ("Check", {"ratio": 4, "no_wrap": False}),
        ("Result", {"ratio": 3}),
        ("What it means", {"ratio": 5, "style": MUTED}),
    )
    for c in card.criteria:
        result_style = "" if c.status is not Status.NA else MUTED
        t.add_row(
            status_cell(c.status, sym),
            Text(pretty_label(c.label, ascii_only)),
            Text(pretty_label(c.detail, ascii_only), style=result_style),
            Text(MEANING.get(c.key, "")),
        )
    return t


# --------------------------------------------------------------------------------------
def _verdict_panel(r: Report, sym: Symbols, ascii_only: bool) -> Panel:
    d, s = r.decision, summarize(r)
    head = Text.assemble((r.name, "bold"), "  ", (r.ticker, f"bold {ACCENT}"))
    meta = [m for m in (r.sector, r.industry) if m]
    if meta:
        head.append(f"   {f' {sym.dot} '.join(meta)}", style=MUTED)
    data = Text(
        f"Price {money(r.price, r.currency)}  {sym.dot}  Market value {fmt_money(r.market_cap, r.currency)}  {sym.dot}  "
        f"{r.data_source}, FY{r.fiscal_years[0][:4]}-FY{r.fiscal_years[-1][:4]} ({len(r.fiscal_years)}y), "
        + (f"TTM to {r.ttm_end}" if r.ttm_end > r.fiscal_years[-1] else "latest = annual report"),
        style=MUTED,
    )

    grid = Table.grid(padding=(0, 2))
    for _ in range(4):
        grid.add_column()
    composite = d.composite
    grid.add_row(
        Text("Composite", style="bold"),
        bar(composite, sym, 30),
        Text(f"{composite:.0f}/100" if composite is not None else "n/a", style="bold"),
        Text(""),
    )
    for key, (name, hint) in PILLARS.items():
        score = d.components.get(key)
        grid.add_row(
            Text(f"  {name}"),
            bar(score, sym, 30),
            Text(f"{score:.0f}" if score is not None else "n/a"),
            Text(f"{pillar_word(key, score)} {sym.dot} {hint}", style=MUTED),
        )

    lines: list[RenderableType] = [
        head,
        data,
        Text(),
        Text.assemble(("Verdict  ", "bold"), badge(d.signal), (f"   {s.headline}", "")),
        Text(f"Confidence {d.confidence.title()} {sym.dot} {d.coverage:.0%} of checks had data", style=MUTED),
        Text(),
        grid,
        Text(),
    ]
    points = Table.grid(padding=(0, 4), expand=True)
    points.add_column(ratio=1)
    points.add_column(ratio=1)
    left = Group(
        Text("Strengths", style=f"bold {GOOD}"),
        bullet_list(s.strengths, sym.passed, f"bold {GOOD}") if s.strengths else Text("none", style=MUTED),
    )
    right = Group(
        Text("Concerns", style=f"bold {BAD}"),
        bullet_list(s.concerns, sym.failed, f"bold {BAD}") if s.concerns else Text("none", style=MUTED),
    )
    points.add_row(left, right)
    lines += [points, Text()]
    # Reasons that the bars and tables already show are skipped; guardrail explanations stay.
    for reason in d.reasons:
        if reason.startswith(("Business quality", "Blended fair value")):
            continue
        lines.append(Text.assemble((f"{sym.bullet} ", ACCENT), pretty_label(reason, ascii_only)))
    for warning in d.warnings:
        lines.append(
            Text.assemble((f"{sym.flag} ", f"bold {WARN}"), (pretty_label(warning, ascii_only), WARN))
        )
    lines.append(Text.assemble(("Timing  ", "bold"), d.timing))
    return _panel(Group(*lines), "VERDICT", sym)


def _valuation_panel(r: Report, sym: Symbols, ascii_only: bool) -> Panel:
    v = r.valuation
    t = _table(
        sym,
        ("Method", {"ratio": 3}),
        ("Per share", {"justify": "right", "ratio": 2, "no_wrap": True}),
        ("Weight", {"justify": "right", "ratio": 1, "no_wrap": True}),
        ("Assumptions", {"ratio": 3, "style": MUTED}),
        ("What it means", {"ratio": 4, "style": MUTED}),
    )
    for m in v.methods:
        t.add_row(
            Text(m.name, style="" if m.value else MUTED),
            Text(money(m.value, r.currency), style="bold" if m.value else MUTED),
            Text(f"{m.weight:.0%}"),
            Text(pretty_label(m.note, ascii_only)),
            Text(METHOD_MEANING.get(m.name, "")),
        )
    parts: list[RenderableType] = [t, Text()]
    if v.fair_value:
        parts.append(
            Text.assemble(
                ("Fair value ", "bold"),
                (money(v.fair_value, r.currency), f"bold {ACCENT}"),
                (f"   DCF bear {money(v.bear, r.currency)} / bull {money(v.bull, r.currency)}   ", MUTED),
                _mos_text(v.margin_of_safety),
            )
        )
        g = gauge(v.bear, v.bull, v.fair_value, r.price, r.currency, sym)
        if g:
            parts += [Text(), *g]
    parts.append(Text())

    kv = Table.grid(padding=(0, 3))
    kv.add_column(style=MUTED, no_wrap=True)
    kv.add_column(no_wrap=True)
    kv.add_column(style=MUTED)
    if finite(v.implied_growth):
        hist = f"vs {pct(v.historical_growth)}/yr delivered (5y)" if finite(v.historical_growth) else ""
        note = f" [{v.implied_growth_note}]" if v.implied_growth_note else ""
        kv.add_row("Market-implied growth", Text(f"{pct(v.implied_growth)}/yr for 5y{note}", style="bold"),
                   f"{hist} {sym.dot} growth the price already assumes")  # fmt: skip
    if finite(v.discount_rate):
        ke = f"cost of equity {pct(v.cost_of_equity)}" if finite(v.cost_of_equity) else ""
        kv.add_row("Discount rate (WACC)", pct(v.discount_rate), f"{ke} {sym.dot} return investors require")
    if finite(v.fcf_yield):
        kv.add_row("Owner FCF yield", pct(v.fcf_yield), "cash for owners ÷ market value")
    if finite(v.maintenance_capex_share) and v.maintenance_capex_share < 0.999:
        kv.add_row("Maintenance capex", f"{v.maintenance_capex_share:.0%} of capex",
                   "rest treated as growth investment (Greenwald)")  # fmt: skip
    if finite(v.earnings_yield):
        kv.add_row("Earnings yield (EBIT/EV)", pct(v.earnings_yield), "operating profit ÷ company value")
    if finite(v.pe_now):
        med = f"10y median {v.pe_median_10y:.1f}" if finite(v.pe_median_10y) else ""
        kv.add_row("P/E (core EPS)", f"{v.pe_now:.1f}", med)
    parts.append(kv)
    for n in v.notes:
        parts.append(Text(f"{sym.flag} {n}", style=WARN))
    return _panel(Group(*parts), "INTRINSIC VALUE", sym)


def _framework_panel(card: Scorecard, title: str, r: Report, sym: Symbols, ascii_only: bool,
                     kinds: tuple[str, ...] | None = None) -> Panel:  # fmt: skip
    iv, mos = card.extras.get("intrinsic_value"), card.extras.get("margin_of_safety")
    method = card.extras.get("intrinsic_method", "Graham formula")
    footer = Table.grid(padding=(0, 3))
    for _ in range(3):
        footer.add_column(no_wrap=True)
    footer.add_row(
        Text.assemble(("Score ", MUTED), _score_text(card, kinds)),
        Text.assemble((f"Value via {method} ", MUTED), (money(iv, r.currency), "bold")),
        _mos_text(mos),
    )
    body = Group(_criteria_table(card, sym, ascii_only), Text(), footer)
    return _panel(body, title, sym, subtitle=Text.assemble(("signal ", MUTED), badge(card.signal)))


def _health_panel(r: Report, sym: Symbols) -> Panel:
    t = _table(
        sym,
        ("", {"width": 1, "no_wrap": True}),
        ("Score", {"ratio": 3}),
        ("Value", {"justify": "right", "ratio": 1, "no_wrap": True}),
        ("Zone", {"ratio": 3}),
        ("What it means", {"ratio": 5, "style": MUTED}),
    )
    for h in r.health:
        status = Status.NA if h.value is None else Status.FAIL if h.red_flag else Status.PASS
        zone_style = BAD if h.red_flag else (MUTED if h.value is None else GOOD)
        t.add_row(
            status_cell(status, sym),
            Text(h.name),
            Text(f"{h.value:.2f}" if finite(h.value) else "n/a"),
            Text(h.zone, style=zone_style),
            Text(HEALTH_MEANING.get(h.name, "")),
        )
    details = [
        Text(f"{h.name}: {h.detail}", style=MUTED) for h in r.health if h.detail and h.value is not None
    ]
    return _panel(Group(t, Text(), *details), "FINANCIAL HEALTH & FORENSICS", sym)


def _factors_panel(r: Report, sym: Symbols, ascii_only: bool) -> Panel:
    score = Text.assemble(("Score ", MUTED), _score_text(r.factors))
    body = Group(_criteria_table(r.factors, sym, ascii_only), Text(), score)
    return _panel(body, "RESEARCH-BACKED FACTORS", sym)


def _trend_panel(r: Report, sym: Symbols) -> Panel:
    t = r.technicals
    trend_style = {"UPTREND": GOOD, "DOWNTREND": BAD}.get(t.trend, WARN)
    kv = Table.grid(padding=(0, 3))
    kv.add_column(style=MUTED, no_wrap=True)
    kv.add_column(no_wrap=True)
    kv.add_column(style=MUTED)
    kv.add_row("Trend", Text(t.trend, style=f"bold {trend_style}"), "price vs averages and 12-month momentum")
    if t.trend != "N/A":
        kv.add_row("50 / 200-day average", f"{money(t.sma50, r.currency)} / {money(t.sma200, r.currency)}",
                   "short vs long-term average price")  # fmt: skip
        kv.add_row(
            "12-1 month momentum", pct(t.momentum_12_1), "return over the past year, excluding last month"
        )
        kv.add_row(
            "RSI (14 days)", f"{t.rsi14:.0f}" if finite(t.rsi14) else "n/a", "70+ overbought, 30- oversold"
        )
        kv.add_row("From 52-week high", pct(t.from_52w_high), "how far below the year's peak")
        kv.add_row("Volatility (1y)", pct(t.volatility_1y), "typical yearly price swing")
    notes = Text(f"Signals: {f' {sym.dot} '.join(t.notes)}", style=MUTED) if t.notes else Text()
    return _panel(Group(kv, notes), "PRICE TREND (timing only, not scored)", sym)


def _sentiment_panel(r: Report, sym: Symbols) -> Panel | None:
    s = r.sentiment
    if not s:
        return None
    kv = Table.grid(padding=(0, 3))
    kv.add_column(style=MUTED, no_wrap=True)
    kv.add_column(no_wrap=True)
    kv.add_column(style=MUTED)
    if "analyst_target" in s:
        kv.add_row(
            "Analyst mean target",
            f"{money(s['analyst_target'], r.currency)} ({pct(s['analyst_upside'])})",
            f"{s.get('analyst_count') or '?'} analysts {sym.dot} rating: {s.get('analyst_rating') or 'n/a'}",
        )
    if "short_pct_float" in s:
        kv.add_row("Short interest", pct(s["short_pct_float"]), "share of tradable stock bet against")
    if s.get("insider_buy_trans") is not None or s.get("insider_sell_trans") is not None:
        kv.add_row(
            "Insider trades (6m)",
            f"{int(s.get('insider_buy_trans') or 0)} buys / {int(s.get('insider_sell_trans') or 0)} sells",
            "includes stock awards; context only",
        )
    if "beta" in s:
        kv.add_row("Beta", f"{s['beta']:.2f}", "1.0 = moves with the market")
    return _panel(kv, "MARKET SENTIMENT (information only)", sym)


def build_detailed(r: Report, ascii_only: bool = False) -> list[RenderableType]:
    sym = symbols(ascii_only)
    blocks: list[RenderableType] = [
        _verdict_panel(r, sym, ascii_only),
        _valuation_panel(r, sym, ascii_only),
        _framework_panel(r.graham, "GRAHAM: DEFENSIVE INVESTOR", r, sym, ascii_only),
        _framework_panel(r.buffett, "BUFFETT: BUSINESS QUALITY", r, sym, ascii_only, ("quality",)),
        _health_panel(r, sym),
        _factors_panel(r, sym, ascii_only),
        _trend_panel(r, sym),
    ]
    sentiment = _sentiment_panel(r, sym)
    if sentiment:
        blocks.append(sentiment)
    foot = [Text(f"{sym.bullet} {pretty_label(n, ascii_only)}", style=MUTED) for n in r.notes]
    foot += [Text(), Text(f"{DISCLAIMER}  {sym.dot}  {r.as_of}  {sym.dot}  {r.elapsed_s:.1f}s", style=MUTED)]
    blocks.append(_panel(Group(*foot), "NOTES", sym))
    return blocks


def render_detailed(r: Report, console: Console, ascii_only: bool = False) -> None:
    width = min(console.width, 140)
    for block in build_detailed(r, ascii_only):
        console.print(block, width=width)
