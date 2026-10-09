"""One visitor's session: credentials (kept in memory only, never stored), checks, analysis.

Runs unchanged on CPython (tests, local server) and in the browser under Pyodide; the only
difference is the transport it is given.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from typing import Any

from ..checks import CheckResult, Step, check_fred_key, check_identity, check_ticker
from ..config import cache_dir
from ..data.cache import FileCache
from ..data.http import Transport
from ..data.sec import SecClient, SecError
from ..data.usage import MeteredTransport, UsageMeter
from ..data.yahoo_http import YahooError, YahooHttpSource


class SessionError(RuntimeError):
    pass


class WebSession:
    def __init__(self, transport: Transport, cfg: dict[str, Any], meter: UsageMeter | None = None) -> None:
        self.meter = meter or UsageMeter()
        self.transport = MeteredTransport(transport, self.meter)
        self.cfg = cfg
        self.cache = FileCache(cache_dir(cfg), bool(cfg.get("cache", {}).get("enabled", True)))
        self.yahoo = YahooHttpSource(self.transport)
        self.identity: str | None = None
        self.fred_key: str | None = None
        self.fred_checked = False
        self.checked: dict[str, float] = {}  # ticker -> time of successful check

    # -- credentials ---------------------------------------------------------------------
    def set_identity(self, raw: str) -> dict[str, Any]:
        res = check_identity(raw, self.transport, self.cache)
        self.identity = res.data["identity"] if res.ok else None
        return self._out(res)

    def set_fred(self, raw: str | None) -> dict[str, Any]:
        key = (raw or "").strip()
        if not key:
            self.fred_key, self.fred_checked = None, True
            res = CheckResult(True, "No key: interest rates come from FRED's public download.",
                              [Step("FRED", True, "public CSV download, no key needed")])  # fmt: skip
            return self._out(res)
        res = check_fred_key(key, self.transport)
        self.fred_key = res.data.get("key") if res.ok else None
        self.fred_checked = res.ok
        return self._out(res)

    def forget(self) -> None:
        self.identity, self.fred_key, self.fred_checked = None, None, False
        self.checked.clear()

    # -- ticker ----------------------------------------------------------------------------
    def _sec(self) -> SecClient:
        if not self.identity:
            raise SessionError("Enter and verify your SEC identity first.")
        return SecClient(self.identity, self.cache, self.cfg.get("cache", {}), self.transport)

    def check_ticker(self, raw: str) -> dict[str, Any]:
        sec = self._sec()

        def quote(t: str):
            try:
                return self.yahoo.quote(t)
            except YahooError as exc:
                if "No data" in str(exc) or "delisted" in str(exc) or "not found" in str(exc).lower():
                    return None
                raise

        res = check_ticker(raw, quote, sec.lookup_cik, sec_required=True)
        if res.ok:
            self.checked[res.data["ticker"]] = time.time()
        return self._out(res)

    # -- analysis --------------------------------------------------------------------------
    def analyze(self, ticker: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        from ..data.bundle import DataError, Sources
        from ..engine import analyze
        from ..report.glossary import summarize
        from .render import report_html

        ticker = (ticker or "").strip().upper()
        if not self.identity or not self.fred_checked:
            raise SessionError("Verify your credentials first.")
        if ticker not in self.checked:
            raise SessionError("Check the ticker first.")
        cfg = copy.deepcopy(self.cfg)
        cfg["sec"]["user_agent"] = self.identity
        cfg.setdefault("fred", {})["api_key"] = self.fred_key or ""
        try:
            report = analyze(ticker, cfg, progress, Sources(market=self.yahoo, transport=self.transport))
        except (SecError, DataError) as exc:
            raise SessionError(str(exc)) from exc
        s = summarize(report)
        return {
            "ticker": report.ticker,
            "name": report.name,
            "signal": report.decision.signal.value,
            "headline": s.headline,
            "simple": report_html(report, "simple"),
            "detailed": report_html(report, "detailed"),
            "usage": self.usage(),
        }

    def usage(self) -> dict[str, Any]:
        snap = self.meter.snapshot()
        snap["mode"] = {"fred": "key" if self.fred_key else "public"}
        return snap

    def _out(self, res: CheckResult) -> dict[str, Any]:
        out = res.to_dict()
        out["data"] = {k: v for k, v in res.data.items() if k != "key"}  # never echo the key back
        out["usage"] = self.usage()
        return out
