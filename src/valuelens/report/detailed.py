"""Detailed view: every number, grouped in panels, each with a 'what it means' note."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..analysis.metrics import finite
from ..models import Report, Scorecard, Status
from .content import (
    DISCLAIMER,
    Row,
    data_line,
    health_status,
    margin_of_safety,
    meta,
    scale,
    score_summary,
    sentiment_rows,
    trend_rows,
    valuation_rows,
    verdict_reasons,
)
from .glossary import HEALTH_MEANING, MEANING, METHOD_MEANING, PILLARS, pillar_word, summarize
from .style import (
    ACCENT,
    BAD,
    GOOD,
    MUTED,
    TONE,
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
    s = score_summary(card, kinds)
    if s is None:
        return Text("n/a", style=MUTED)
    passed, rest, tone = s
    return Text.assemble((passed, f"bold {TONE[tone]}"), (rest, MUTED))


def _mos_text(mos: float | None) -> Text:
    text, tone = margin_of_safety(mos)
    return Text(text, style=MUTED if tone == "muted" else f"bold {TONE[tone]}")


def _kv(rows: list[Row]) -> Table:
    kv = Table.grid(padding=(0, 3))
    kv.add_column(style=MUTED, no_wrap=True)
    kv.add_column(no_wrap=True)
    kv.add_column(style=MUTED)
    for row in rows:
        style = " ".join(s for s in ("bold" if row.strong else "", TONE[row.tone]) if s)
        kv.add_row(row.label, Text(row.value, style=style), row.note)
    return kv


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
    sector = meta(r)
    if sector:
        head.append(f"   {f' {sym.dot} '.join(sector)}", style=MUTED)
    data = Text(data_line(r, sym.dot), style=MUTED)

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
    for reason in verdict_reasons(d):
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
        g = gauge(scale(v, r.price), r.currency, sym)
        if g:
            parts += [Text(), *g]
    parts.append(Text())
    parts.append(_kv(valuation_rows(r, sym.dot)))
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
        status, tone = health_status(h)
        t.add_row(
            status_cell(status, sym),
            Text(h.name),
            Text(f"{h.value:.2f}" if finite(h.value) else "n/a"),
            Text(h.zone, style=TONE[tone]),
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
    notes = Text(f"Signals: {f' {sym.dot} '.join(t.notes)}", style=MUTED) if t.notes else Text()
    return _panel(Group(_kv(trend_rows(r)), notes), "PRICE TREND (timing only, not scored)", sym)


def _sentiment_panel(r: Report, sym: Symbols) -> Panel | None:
    if not r.sentiment:
        return None
    return _panel(_kv(sentiment_rows(r, sym.dot)), "MARKET SENTIMENT (information only)", sym)


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
