"""Live loading checklist shown while data is fetched (stderr, interactive terminals only)."""

from __future__ import annotations

import time
from types import TracebackType

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .style import ACCENT, GOOD, MUTED

STEPS: dict[str, str] = {
    "market": "Price & company profile",
    "filings": "Financial statements (SEC filings)",
    "rates": "Interest rates (FRED)",
    "analysis": "Graham, Buffett, valuation & risk checks",
}


class LoadingDisplay:
    """Context manager exposing ``step(key)`` as the progress callback.

    Each step shows a spinner while running and a tick with its duration once the next step
    starts. The display is transient: it disappears when loading finishes, leaving only the report.
    """

    def __init__(self, console: Console, ticker: str, position: str = "", ascii_only: bool = False) -> None:
        self.console = console
        self.ticker = ticker
        self.position = position
        self.ascii_only = ascii_only
        self.enabled = console.is_terminal and not console.is_dumb_terminal
        self._done: list[tuple[str, float]] = []
        self._current: str | None = None
        self._started = 0.0
        self._live: Live | None = None

    # -- rendering -------------------------------------------------------------------------
    def _render(self) -> Group:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=2)
        grid.add_column()
        grid.add_column(style=MUTED)
        for key, secs in self._done:
            grid.add_row(Text("+" if self.ascii_only else "√", style=f"bold {GOOD}"), STEPS.get(key, key),
                         f"{secs:.1f}s")  # fmt: skip
        if self._current:
            spinner = Spinner("line" if self.ascii_only else "dots", style=ACCENT)
            grid.add_row(spinner, Text(STEPS.get(self._current, self._current), style="bold"), "")
        title = Text.assemble(
            ("Analysing ", MUTED), (self.ticker, f"bold {ACCENT}"), (f" {self.position}", MUTED)
        )
        return Group(title, grid)

    # -- callback --------------------------------------------------------------------------
    def step(self, key: str) -> None:
        now = time.perf_counter()
        if self._current is not None:
            self._done.append((self._current, now - self._started))
        self._current, self._started = key, now
        if self._live:
            self._live.update(self._render())

    # -- context manager -------------------------------------------------------------------
    def __enter__(self) -> LoadingDisplay:
        if self.enabled:
            self._live = Live(self._render(), console=self.console, refresh_per_second=12, transient=True)
            self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._live:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None
