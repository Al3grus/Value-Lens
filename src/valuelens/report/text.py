"""Entry points for rendering a report to the terminal or to a plain string."""

from __future__ import annotations

import io
import sys
from typing import Literal, TextIO

from rich.console import Console

from ..models import Report
from .detailed import render_detailed
from .simple import render_comparison, render_simple

View = Literal["simple", "detailed"]


_ASCII_MAP = str.maketrans({"…": "...", "–": "-", "—": "-", "→": "->", "≤": "<=", "≥": ">=", "×": "x",
                            "÷": "/", "·": "|", "√": "sqrt", "’": "'", "“": '"', "”": '"', "█": "#",
                            "░": ".", "═": "=", "♦": "F", "●": "P", "•": "-"})  # fmt: skip


class AsciiFile:
    """File proxy that guarantees pure-ASCII output (for consoles that cannot show Unicode)."""

    def __init__(self, file: TextIO) -> None:
        self._file = file

    def write(self, text: str) -> int:
        clean = text.translate(_ASCII_MAP).encode("ascii", "replace").decode("ascii")
        return self._file.write(clean)

    def flush(self) -> None:
        self._file.flush()

    def isatty(self) -> bool:
        return self._file.isatty()

    def fileno(self) -> int:
        return self._file.fileno()

    @property
    def encoding(self) -> str:
        return "ascii"


def make_console(*, ascii_only: bool = False, stderr: bool = False, file: TextIO | None = None,
                 **kwargs) -> Console:  # fmt: skip
    """Console with markup/emoji/auto-highlighting off: all styling is explicit, and text from
    filings can never be misread as markup. ``ascii_only`` guarantees pure-ASCII output."""
    if ascii_only:
        file = AsciiFile(file or (sys.stderr if stderr else sys.stdout))
    return Console(
        file=file,
        stderr=stderr,
        markup=False,
        emoji=False,
        highlight=False,
        safe_box=True,
        legacy_windows=None if not ascii_only else False,
        **kwargs,
    )


def print_report(report: Report, console: Console, view: View = "simple", ascii_only: bool = False) -> None:
    if view == "detailed":
        render_detailed(report, console, ascii_only)
    else:
        render_simple(report, console, ascii_only)


def print_comparison(reports: list[Report], console: Console, ascii_only: bool = False) -> None:
    render_comparison(reports, console, ascii_only)


def render(report: Report, view: View = "simple", ascii_only: bool = False, width: int = 110) -> str:
    """Render to a plain string (no colour codes), e.g. for files, tests or e-mail."""
    buf = io.StringIO()
    console = make_console(
        ascii_only=ascii_only, file=buf, width=width, color_system=None, force_terminal=False
    )
    print_report(report, console, view, ascii_only)
    return buf.getvalue()
