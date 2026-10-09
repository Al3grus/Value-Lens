"""Render the terminal reports as HTML (rich's own export, so the page shows exactly what the
terminal shows). Rich escapes all text, so company names cannot inject markup."""

from __future__ import annotations

import io

from rich.terminal_theme import TerminalTheme

from ..models import Report
from ..report.text import make_console, print_report

# Graphite background, white text, gold accents. Slot order: black, red, green, yellow, blue,
# magenta, cyan, white (normal, then bright). The reports use cyan as their accent colour; here it
# renders as pale gold so the page keeps to gold and white, with green/red only for pass/fail.
READOUT_THEME = TerminalTheme(
    (28, 29, 32),
    (232, 232, 230),
    [
        (20, 21, 23), (224, 108, 100), (125, 191, 142), (226, 186, 92),
        (190, 190, 186), (204, 178, 120), (232, 210, 150), (232, 232, 230),
    ],
    [
        (110, 111, 116), (240, 132, 124), (150, 214, 166), (240, 206, 120),
        (214, 214, 210), (226, 200, 140), (244, 226, 176), (250, 250, 248),
    ],
)  # fmt: skip

WIDTHS = {"simple": 104, "detailed": 124}


def report_html(report: Report, view: str) -> str:
    console = make_console(
        file=io.StringIO(), width=WIDTHS[view], record=True, force_terminal=True, color_system="truecolor"
    )
    print_report(report, console, view)
    return console.export_html(theme=READOUT_THEME, inline_styles=True, code_format="{code}")
