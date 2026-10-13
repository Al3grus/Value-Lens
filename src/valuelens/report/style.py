"""Shared terminal styling: palette, symbols, badges, score bars and the value gauge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich import box
from rich.table import Table
from rich.text import Text

from ..analysis.metrics import finite
from ..models import Signal, Status

if TYPE_CHECKING:
    from .content import Scale

ACCENT = "cyan"
MUTED = "grey58"
GOOD = "green"
BAD = "red"
WARN = "yellow"
TONE = {"good": GOOD, "bad": BAD, "warn": WARN, "muted": MUTED, "": ""}  # report.content tones

SIGNAL_STYLE = {
    Signal.STRONG_BUY: "bold white on dark_green",
    Signal.BUY: "bold black on green",
    Signal.HOLD: "bold black on yellow",
    Signal.SELL: "bold white on red3",
    Signal.STRONG_SELL: "bold white on dark_red",
}
SIGNAL_FG = {
    Signal.STRONG_BUY: "bold green",
    Signal.BUY: "green",
    Signal.HOLD: "yellow",
    Signal.SELL: "red",
    Signal.STRONG_SELL: "bold red",
}


@dataclass(frozen=True)
class Symbols:
    passed: str
    failed: str
    na: str
    flag: str
    bullet: str
    bar_full: str
    bar_empty: str
    line: str
    range: str
    fair: str
    price: str
    arrow: str
    dot: str
    box: box.Box
    table_box: box.Box


# Every glyph below is in the WGL4 set, which all common monospace fonts (Consolas, Cascadia,
# Courier New, DejaVu, JetBrains Mono) cover. Glyphs outside it (e.g. ✓ ◆ ━ ╭) are borrowed
# from fallback fonts with different widths, which pushes box borders out of line.
UNICODE = Symbols("√", "×", "–", "!", "•", "█", "░", "─", "═", "♦", "●", "→", "·",
                  box.SQUARE, box.SIMPLE_HEAD)  # fmt: skip
WGL4_SAFE = frozenset("√×–!•█░─═♦●→·≤≥÷…")  # extra non-ASCII glyphs the reports may use
ASCII = Symbols("+", "x", "-", "!", "-", "#", ".", "-", "=", "F", "P", "->", "|",
                box.ASCII, box.ASCII)  # fmt: skip


def symbols(ascii_only: bool) -> Symbols:
    return ASCII if ascii_only else UNICODE


def pretty_label(label: str, ascii_only: bool) -> str:
    if ascii_only:
        return label.replace("×", "x").replace("√", "sqrt").replace("–", "-")
    return label.replace("<=", "≤").replace(">=", "≥").replace(" -> ", " → ")


def status_cell(status: Status, sym: Symbols) -> Text:
    if status is Status.PASS:
        return Text(sym.passed, style=f"bold {GOOD}")
    if status is Status.FAIL:
        return Text(sym.failed, style=f"bold {BAD}")
    return Text(sym.na, style=MUTED)


def badge(signal: Signal | None) -> Text:
    if signal is None:
        return Text(" N/A ", style="reverse")
    return Text(f" {signal.value} ", style=SIGNAL_STYLE[signal])


def score_tone(score: float | None) -> str:
    """Level of a 0-100 score: "good", "warn", "bad", or "muted" when there is none."""
    if score is None:
        return "muted"
    if score >= 70:
        return "good"
    if score >= 45:
        return "warn"
    return "bad"


def score_color(score: float | None) -> str:
    return TONE[score_tone(score)]


def bar(score: float | None, sym: Symbols, width: int = 24) -> Text:
    """Horizontal 0-100 bar coloured by level."""
    if score is None:
        return Text(sym.bar_empty * width, style=MUTED)
    filled = round(max(0.0, min(100.0, score)) / 100 * width)
    out = Text(sym.bar_full * filled, style=score_color(score))
    out.append(sym.bar_empty * (width - filled), style="grey35")
    return out


def bullet_list(items: list[str], symbol: str, style: str, indent: int = 0) -> Table:
    """Bulleted lines whose wrapped continuation stays aligned under the text."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=indent + len(symbol), no_wrap=True)
    grid.add_column()
    for item in items:
        grid.add_row(Text(" " * indent + symbol, style=style), Text(item))
    return grid


def money(x: float | None, currency: str, digits: int = 2) -> str:
    if not finite(x):
        return "n/a"
    sym = "$" if currency == "USD" else f"{currency} "
    return f"{sym}{x:,.{digits}f}"


def gauge(sc: Scale | None, currency: str, sym: Symbols, width: int = 48) -> list[Text] | None:
    """Two lines: a scale with the fair-value range, fair value and price, then a legend."""
    if sc is None:
        return None

    def pos(v: float) -> int:
        return round(sc.at(v) * (width - 1))

    cells = [Text(sym.line, style="grey35") for _ in range(width)]
    for i in range(pos(sc.low), pos(sc.high) + 1):
        cells[i] = Text(sym.range, style=ACCENT)
    cells[pos(sc.fair)] = Text(sym.fair, style=f"bold {ACCENT}")
    price_style = f"bold {GOOD}" if sc.price <= sc.fair else f"bold {BAD}"
    cells[pos(sc.price)] = Text(sym.price, style=price_style)
    line = Text()
    for c in cells:
        line.append_text(c)
    line = Text.assemble(Text(f"{money(sc.left, currency, 0):>9} ", style=MUTED), line,
                         Text(f" {money(sc.right, currency, 0)}", style=MUTED))  # fmt: skip
    legend = Text.assemble(
        " " * 10,
        (sym.range * 2, ACCENT), (f" value range {money(sc.low, currency, 0)} to {money(sc.high, currency, 0)}   ", MUTED),
        (sym.fair, f"bold {ACCENT}"), (f" fair value {money(sc.fair, currency, 0)}   ", MUTED),
        (sym.price, price_style), (f" price {money(sc.price, currency, 0)}", MUTED),
    )  # fmt: skip
    return [line, legend]
