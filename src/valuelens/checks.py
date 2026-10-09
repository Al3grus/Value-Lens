"""Checks that run before anything expensive: the SEC identity, the FRED key and the ticker.

Each check makes at most one or two small requests, so a typo is caught before an analysis spends
SEC, FRED and Yahoo requests. Used by the website (through the relay) and testable offline with a
fake transport.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data import fred
from .data.cache import FileCache
from .data.http import Transport, TransportError
from .data.sec import TICKERS_URL, SecClient, SecError, ascii_identity, identity_problem

DOH_URL = "https://cloudflare-dns.com/dns-query"

# Misspellings of the big mail providers. These domains may exist (typo-squatters register
# them), so a DNS check alone would let them through.
DOMAIN_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com",
    "gnail.com": "gmail.com", "gmaill.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gmail.cm": "gmail.com", "gmal.com": "gmail.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "hotmail.con": "hotmail.com", "outlok.com": "outlook.com", "outllook.com": "outlook.com",
    "outlook.co": "outlook.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "yahoo.co": "yahoo.com", "icloud.co": "icloud.com", "iclod.com": "icloud.com",
    "protonmail.co": "protonmail.com",
}  # fmt: skip

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,15}$")
NOT_A_COMPANY = {
    "ETF": "an ETF", "MUTUALFUND": "a mutual fund", "CRYPTOCURRENCY": "a cryptocurrency",
    "INDEX": "an index", "CURRENCY": "a currency pair", "FUTURE": "a futures contract",
    "OPTION": "an option",
}  # fmt: skip


@dataclass
class Step:
    label: str
    ok: bool | None  # None = could not be checked
    detail: str


@dataclass
class CheckResult:
    ok: bool
    message: str
    steps: list[Step] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------------------
# SEC identity
# --------------------------------------------------------------------------------------
def email_domain(identity: str) -> str:
    return identity.strip().rsplit("@", 1)[-1].lower().rstrip(".")


def mail_domain_status(domain: str, transport: Transport) -> tuple[bool | None, str]:
    """(accepts mail?, detail) via DNS-over-HTTPS. MX records, or an address record as the
    implicit mail server (RFC 5321 section 5.1)."""

    def query(rtype: str) -> dict[str, Any] | None:
        try:
            resp = transport.request(
                "GET",
                DOH_URL,
                params={"name": domain, "type": rtype},
                headers={"Accept": "application/dns-json"},
            )
        except TransportError:
            return None
        if not resp.ok:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    unchecked = None, "DNS lookup unavailable, domain not checked"
    mx = query("MX")
    if mx is None:
        return unchecked
    if mx.get("Status") == 3:  # NXDOMAIN
        return False, f"{domain} does not exist"
    if mx.get("Status") != 0:  # SERVFAIL etc.: the resolver failed, not the domain
        return unchecked
    records = [a for a in mx.get("Answer") or [] if a.get("type") == 15]
    if records:
        if all(str(a.get("data", "")).strip() == "0 ." for a in records):  # RFC 7505 null MX
            return False, f"{domain} declares that it receives no e-mail"
        return True, f"{domain} receives e-mail"
    for rtype, code in (("A", 1), ("AAAA", 28)):
        rec = query(rtype)
        if rec is None or rec.get("Status") not in (0, 3):
            return unchecked
        if any(r.get("type") == code for r in rec.get("Answer") or []):
            return True, f"{domain} has no mail record but has a server that may accept e-mail"
    return False, f"{domain} has no mail server"


def check_identity(identity: str, transport: Transport, cache: FileCache | None = None) -> CheckResult:
    typed = " ".join((identity or "").split())
    identity = ascii_identity(typed)
    steps: list[Step] = []
    problem = identity_problem(identity)
    if problem:
        return CheckResult(False, problem, [Step("format", False, problem)])
    sent = "name and e-mail" if identity == typed else f"name and e-mail, sent as {identity}"
    steps.append(Step("format", True, sent))

    domain = email_domain(identity)
    if domain in DOMAIN_TYPOS:
        msg = f"Did you mean {DOMAIN_TYPOS[domain]}? {domain} looks like a typo."
        steps.append(Step("e-mail domain", False, msg))
        return CheckResult(False, msg, steps)
    accepts, detail = mail_domain_status(domain, transport)
    steps.append(Step("e-mail domain", accepts, detail))
    if accepts is False:
        return CheckResult(False, f"Check the e-mail address: {detail}.", steps)

    client = SecClient(identity, cache or FileCache(Path("."), enabled=False), {}, transport)
    try:
        status = client.verify()
    except SecError as exc:
        steps.append(Step("SEC EDGAR", False, str(exc)))
        return CheckResult(False, str(exc), steps)
    if status != 200:
        msg = f"SEC answered HTTP {status} for {TICKERS_URL}. Try again in a minute."
        steps.append(Step("SEC EDGAR", False, msg))
        return CheckResult(False, msg, steps)
    steps.append(Step("SEC EDGAR", True, "SEC accepted this identity"))
    return CheckResult(True, "Identity accepted.", steps, {"identity": identity})


# --------------------------------------------------------------------------------------
# FRED key
# --------------------------------------------------------------------------------------
def check_fred_key(key: str, transport: Transport) -> CheckResult:
    key = (key or "").strip().lower()
    problem = fred.key_problem(key)
    if problem:
        return CheckResult(False, problem, [Step("format", False, problem)])
    steps = [Step("format", True, "32 letters and digits")]
    res = fred.check_key(key, transport)
    detail = res.message
    if res.observation:
        detail = f"live: AAA corporate yield {res.observation[0]:.2f}% ({res.observation[1]})"
    steps.append(Step("FRED API", res.ok, detail))
    return CheckResult(res.ok, res.message, steps, {"key": key} if res.ok else {})


# --------------------------------------------------------------------------------------
# Ticker
# --------------------------------------------------------------------------------------
def normalise_ticker(raw: str) -> tuple[str | None, str | None]:
    """Return (ticker, error)."""
    text = (raw or "").strip().upper()
    if not text:
        return None, "Type a ticker, e.g. MSFT."
    if re.search(r"[\s,;]", text):
        return None, "Enter one ticker only."
    if not TICKER_RE.match(text):
        return None, "Tickers use letters, digits, '.' and '-', e.g. MSFT, BRK-B, ASML.AS."
    return text, None


def check_ticker(raw: str, quote, sec_lookup, *, sec_required: bool = False) -> CheckResult:
    """``quote(ticker)`` -> dict(name, exchange, currency, price, type) or None;
    ``sec_lookup(ticker)`` -> (cik, title) | None, or raises SecError."""
    ticker, error = normalise_ticker(raw)
    if error:
        return CheckResult(False, error, [Step("format", False, error)])
    steps = [Step("format", True, ticker)]
    try:
        q = quote(ticker)
    except Exception as exc:  # network errors, throttling, parsing changes
        msg = f"Could not reach Yahoo Finance ({exc}). Try again in a minute."
        steps.append(Step("market data", None, msg))
        return CheckResult(False, msg, steps, {"ticker": ticker})
    if not q or not q.get("price"):
        msg = (f"No listed company found for {ticker}. Check the spelling; non-US shares need an exchange "
               "suffix, e.g. ASML.AS or SAP.DE.")  # fmt: skip
        steps.append(Step("market data", False, msg))
        return CheckResult(False, msg, steps, {"ticker": ticker})
    kind = q.get("type") or ""
    if kind and kind != "EQUITY":
        msg = f"{ticker} is {NOT_A_COMPANY.get(kind, 'not a company share')}. ValueLens analyses individual companies."
        steps.append(Step("market data", False, msg))
        return CheckResult(False, msg, steps, {"ticker": ticker, **q})
    steps.append(Step("market data", True, f"{q.get('name') or ticker} on {q.get('exchange') or '?'}"))
    try:
        hit = sec_lookup(ticker)
    except SecError as exc:
        steps.append(Step("SEC filings", None, str(exc)))
        return CheckResult(False, str(exc), steps, {"ticker": ticker, **q})
    data = {"ticker": ticker, **q, "sec_filer": hit is not None, "cik": hit[0] if hit else None}
    if hit:
        steps.append(Step("SEC filings", True, f"CIK {hit[0]}: 10+ years of 10-K/10-Q data"))
        return CheckResult(True, "Ready to analyse.", steps, data)
    if sec_required:
        msg = f"{ticker} does not file with the SEC, and this version reads statements from SEC filings only."
        steps.append(Step("SEC filings", False, msg))
        return CheckResult(False, msg, steps, data)
    steps.append(Step("SEC filings", None, "not an SEC filer: about 4 years of Yahoo statements"))
    return CheckResult(True, "Ready (limited history).", steps, data)
