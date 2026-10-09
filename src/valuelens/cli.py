"""Command-line interface.

valuelens MSFT                 simple, plain-English view (default)
valuelens MSFT GOOG -d         detailed view with every number explained
valuelens MSFT --json          machine-readable output
"""

from __future__ import annotations

import argparse
import logging
import sys

from rich.text import Text

from . import __version__
from .config import ConfigError, load_config
from .data.bundle import DataError
from .data.sec import SecError
from .engine import analyze
from .report.json_out import to_json
from .report.progress import LoadingDisplay
from .report.style import BAD
from .report.text import make_console, print_comparison, print_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="valuelens",
        description="Value-investing analysis of a stock: Graham + Buffett + research-backed checks, "
        "combined into a buy/hold/sell verdict.",
    )
    p.add_argument("tickers", nargs="+", help="Ticker symbols in Yahoo format (e.g. MSFT, BRK-B, ASML.AS)")
    view = p.add_mutually_exclusive_group()
    view.add_argument("-s", "--simple", action="store_const", const="simple", dest="view",
                      help="Plain-English summary (default)")  # fmt: skip
    view.add_argument("-d", "--detailed", action="store_const", const="detailed", dest="view",
                      help="Every number, with what it means")  # fmt: skip
    view.add_argument("--json", action="store_const", const="json", dest="view", help="Machine-readable JSON")
    p.add_argument("--config", help="Path to a TOML file overriding the defaults")
    p.add_argument("--no-cache", action="store_true", help="Ignore and do not write the local cache")
    p.add_argument("--no-animation", action="store_true", help="Do not show the loading animation")
    p.add_argument("--ascii", action="store_true", help="ASCII-only output (for old consoles)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if not args.verbose:
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # its warnings are noisy
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            args.ascii = True

    err = make_console(ascii_only=args.ascii, stderr=True)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        err.print(Text(f"error: {exc}", style=BAD))
        return 2
    if args.no_cache:
        cfg["cache"]["enabled"] = False

    report_cfg = cfg.get("report", {})
    view = args.view or report_cfg.get("view", "simple")
    animate = report_cfg.get("animations", True) and not args.no_animation and not args.verbose
    out = make_console(ascii_only=args.ascii)

    reports, failures = [], 0
    total = len(args.tickers)
    for i, ticker in enumerate(args.tickers, 1):
        position = f"({i}/{total})" if total > 1 else ""
        loader = LoadingDisplay(err, ticker.upper(), position, args.ascii)
        loader.enabled = loader.enabled and animate
        try:
            with loader:
                report = analyze(ticker, cfg, progress=loader.step)
        except SecError as exc:
            err.print(Text(f"error: {exc}", style=BAD))
            return 2
        except DataError as exc:
            err.print(Text(f"{ticker}: {exc}", style=BAD))
            failures += 1
            continue
        reports.append(report)
        if view != "json":
            print_report(report, out, view, args.ascii)
            out.print()

    if view == "json" and reports:
        print(to_json(reports))
    elif len(reports) > 1:
        print_comparison(reports, out, args.ascii)
    return 1 if failures and not reports else 0


if __name__ == "__main__":
    raise SystemExit(main())
