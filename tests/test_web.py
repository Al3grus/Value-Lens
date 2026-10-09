"""The website's Python side, offline: checks, Yahoo HTTP source, relay routing, the browser
glue and a full analysis through a fake relay transport (the exact code path Pyodide runs)."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

import fixtures
import numpy as np
import pandas as pd
import pytest
from fakes import FakeTransport

from valuelens.checks import DOH_URL, check_fred_key, check_identity, check_ticker
from valuelens.data import fred
from valuelens.data.http import TransportError
from valuelens.data.sec import FACTS_URL, SUBMISSIONS_URL, TICKERS_URL, SecError, identity_problem
from valuelens.data.usage import MeteredTransport, UsageMeter, service_of
from valuelens.data.yahoo_http import (
    CHART,
    SUMMARY,
    YahooHttpSource,
    flatten_summary,
    insider_from_summary,
    monthly_beta,
    parse_chart,
)

IDENT = "Jane Doe jane@example.com"
CIK = 9999999


# --------------------------------------------------------------------------------------
# A fake internet: SEC, FRED, Yahoo and DNS answering like the real services
# --------------------------------------------------------------------------------------
def chart_json(hist: pd.DataFrame, splits=None, dividends=None, meta=None) -> dict:
    ts = [
        int(pd.Timestamp(d).tz_localize("America/New_York").replace(hour=9, minute=30).timestamp())
        for d in hist.index
    ]
    close = hist["Close"].round(4).tolist()
    events = {}
    if splits is not None and len(splits):
        events["splits"] = {
            str(int(pd.Timestamp(d).timestamp())): {
                "date": int(pd.Timestamp(d).timestamp()),
                "numerator": v,
                "denominator": 1,
            }
            for d, v in splits.items()
        }
    if dividends is not None and len(dividends):
        events["dividends"] = {
            str(int(pd.Timestamp(d).timestamp())): {"date": int(pd.Timestamp(d).timestamp()), "amount": v}
            for d, v in dividends.items()
        }
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": "TEST",
                        "instrumentType": "EQUITY",
                        "longName": "TestCo Inc.",
                        "fullExchangeName": "NasdaqGS",
                        "regularMarketPrice": close[-1],
                        **(meta or {}),
                    },
                    "timestamp": ts,
                    "events": events,
                    "indicators": {
                        "quote": [
                            {
                                "open": close,
                                "high": close,
                                "low": close,
                                "close": close,
                                "volume": [1_000_000] * len(close),
                            }
                        ],
                        "adjclose": [{"adjclose": close}],
                    },
                }
            ],
            "error": None,
        }
    }


SUMMARY_JSON = {
    "quoteSummary": {
        "result": [
            {
                "price": {
                    "longName": "TestCo Inc.",
                    "currency": "USD",
                    "regularMarketPrice": {"raw": 60.0, "fmt": "60.00"},
                },
                "summaryProfile": {"sector": "Technology", "industry": "Software - Application"},
                "defaultKeyStatistics": {
                    "beta": {"raw": 1.1},
                    "sharesOutstanding": {"raw": 1.0e9},
                    "shortPercentOfFloat": {"raw": 0.012},
                },
                "financialData": {
                    "targetMeanPrice": {"raw": 70.0},
                    "numberOfAnalystOpinions": {"raw": 20},
                    "recommendationKey": "buy",
                    "financialCurrency": "USD",
                },
                "netSharePurchaseActivity": {
                    "buyInfoCount": {"raw": 2},
                    "sellInfoCount": {"raw": 9},
                    "netInfoShares": {"raw": -4000},
                    "netPercentInsiderShares": {"raw": -0.01},
                },
            }
        ],
        "error": None,
    }
}


def mx(domain_answers: list[str] | None, status: int = 0):
    answer = [{"name": "x", "type": 15, "data": d} for d in domain_answers or []]
    return (200, {"Status": status, "Answer": answer})


def world(**overrides) -> FakeTransport:
    hist = fixtures.price_history()

    def chart(method, url, headers):
        if "%5EGSPC" in url or "^GSPC" in url:
            return 200, chart_json(hist.assign(Close=hist["Close"] ** 0.8))
        if "range=5d" in url:
            return 200, chart_json(hist.tail(5))
        return 200, chart_json(hist, fixtures.splits(), fixtures.dividends())

    def sec(method, url, headers):
        if headers.get("User-Agent") != IDENT:
            return 403, "Undeclared Automated Tool"
        if url == TICKERS_URL:
            return 200, {
                "0": {"cik_str": CIK, "ticker": "TEST", "title": "TESTCO INC"},
                "1": {"cik_str": 1, "ticker": "SPY", "title": "SPDR"},
            }
        if url == SUBMISSIONS_URL.format(cik=CIK):
            return 200, {"name": "TESTCO INC", "sic": "7372", "sicDescription": "Prepackaged Software"}
        if url == FACTS_URL.format(cik=CIK):
            return 200, fixtures.company_facts()
        return 404, {}

    routes = {
        DOH_URL + "?name=example.com&type=MX": mx(["10 mail.example.com."]),
        "https://www.sec.gov/": sec,
        "https://data.sec.gov/": sec,
        fred.FRED_API: lambda m, u, h: (
            (200, {"observations": [{"date": "2026-09-01", "value": "6.03" if "AAA" in u else "4.20"}]})
            if "api_key=" + "a" * 32 in u
            else (
                400,
                {
                    "error_code": 400,
                    "error_message": "Bad Request.  The value for variable api_key is not registered.",
                },
            )
        ),
        fred.FRED_CSV: lambda m, u, h: (
            200,
            f"observation_date,X\n2026-09-01,{'6.03' if 'AAA' in u else '4.20'}\n",
        ),
        CHART.format(symbol="TEST"): chart,
        CHART.format(symbol="%5EGSPC"): chart,
        CHART.format(symbol="SPY"): lambda m, u, h: (
            200,
            chart_json(hist.tail(5), meta={"instrumentType": "ETF"}),
        ),
        CHART.format(symbol="NOPE"): (
            404,
            {
                "chart": {
                    "result": None,
                    "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"},
                }
            },
        ),
        SUMMARY.format(symbol="TEST"): (200, SUMMARY_JSON),
    }
    routes.update(overrides)
    return FakeTransport(routes)


# --------------------------------------------------------------------------------------
# SEC identity
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,ok",
    [
        (IDENT, True),
        ("jane@example.com", False),
        ("Jane Doe", False),
        ("", False),
        ("Jane jane@example", False),
    ],
)
def test_identity_format(value, ok):
    assert (identity_problem(value) is None) is ok


def test_identity_accepted_after_three_checks():
    t = world()
    res = check_identity("  Jane   Doe  jane@example.com ", t)
    assert res.ok, res.message
    assert [s.label for s in res.steps] == ["format", "e-mail domain", "SEC EDGAR"]
    assert res.data["identity"] == IDENT
    head = [c for c in t.calls if c[1].startswith(TICKERS_URL)]
    assert head and head[0][0] == "HEAD" and head[0][2]["User-Agent"] == IDENT


def test_identity_typo_domain_rejected_without_requests():
    t = world()
    res = check_identity("Jane Doe jane@gmial.com", t)
    assert not res.ok and "gmail.com" in res.message
    assert t.calls == []


def test_identity_nonexistent_domain_rejected():
    t = world(**{DOH_URL + "?name=nowhere.example&type=MX": mx([], status=3)})
    res = check_identity("Jane Doe jane@nowhere.example", t)
    assert not res.ok and "does not exist" in res.message
    assert not t.urls("sec.gov")


def test_identity_null_mx_rejected():
    t = world(**{DOH_URL + "?name=example.com&type=MX": mx(["0 ."])})
    assert not check_identity(IDENT, t).ok


def test_identity_refused_by_sec():
    t = world(**{"https://www.sec.gov/": (403, "Undeclared Automated Tool")})
    res = check_identity(IDENT, t)
    assert not res.ok and "403" in res.message
    assert res.steps[-1].label == "SEC EDGAR" and res.steps[-1].ok is False


def test_identity_dns_unavailable_does_not_block():
    t = world(**{DOH_URL + "?name=example.com&type=MX": TransportError("down")})
    res = check_identity(IDENT, t)
    assert res.ok and res.steps[1].ok is None


# --------------------------------------------------------------------------------------
# FRED key
# --------------------------------------------------------------------------------------
def test_fred_key_checked_live():
    t = world()
    res = check_fred_key("A" * 32, t)  # pasted in capitals: normalised
    assert res.ok and "6.03%" in res.steps[-1].detail
    assert len(t.urls("api.stlouisfed.org")) == 1 and "limit=1" in t.urls("api.stlouisfed.org")[0]


def test_fred_key_unregistered():
    res = check_fred_key("b" * 32, world())
    assert not res.ok and "does not recognise" in res.message


def test_fred_key_rate_limited():
    t = world(**{fred.FRED_API: (429, {"error_code": 429, "error_message": "Too Many Requests"})})
    res = check_fred_key("a" * 32, t)
    assert not res.ok and "120" in res.message


def test_fred_key_bad_format_makes_no_request():
    t = world()
    assert not check_fred_key("short", t).ok
    assert t.calls == []


# --------------------------------------------------------------------------------------
# Ticker
# --------------------------------------------------------------------------------------
def _quote(t):
    return YahooHttpSource(t).quote


def test_ticker_check_ok():
    t = world()
    from valuelens.data.cache import FileCache
    from valuelens.data.sec import SecClient

    sec = SecClient(IDENT, FileCache(".", enabled=False), {}, t)
    res = check_ticker("test", _quote(t), sec.lookup_cik, sec_required=True)
    assert res.ok and res.data["cik"] == CIK and res.data["name"] == "TestCo Inc."


def test_ticker_check_rejects_etf_before_sec():
    t = world()
    res = check_ticker("SPY", _quote(t), lambda _t: pytest.fail("SEC should not be asked"))
    assert not res.ok and "ETF" in res.message


def test_ticker_check_unknown():
    t = world()

    def quote(sym):
        try:
            return _quote(t)(sym)
        except Exception:
            return None

    res = check_ticker("NOPE", quote, lambda _t: None)
    assert not res.ok and "No listed company" in res.message


def test_ticker_check_non_sec_filer_when_required():
    t = world()
    res = check_ticker("TEST", _quote(t), lambda _t: None, sec_required=True)
    assert not res.ok and "SEC" in res.message


def test_ticker_check_sec_error_reported():
    t = world()

    def boom(_t):
        raise SecError("SEC refused the request (403)")

    assert not check_ticker("TEST", _quote(t), boom).ok


# --------------------------------------------------------------------------------------
# Yahoo HTTP source
# --------------------------------------------------------------------------------------
def test_parse_chart_history_splits_dividends():
    hist = fixtures.price_history()
    meta, h, splits, divs = parse_chart(chart_json(hist, fixtures.splits(), fixtures.dividends()))
    assert meta["currency"] == "USD"
    assert len(h) == len(hist) and h.index.tz is None
    assert h.index[-1] == hist.index[-1]
    assert splits.iloc[0] == 2.0
    assert divs.iloc[0] == 0.25 and divs.index.is_monotonic_increasing


def test_parse_chart_error():
    from valuelens.data.yahoo_http import YahooError

    with pytest.raises(YahooError):
        parse_chart({"chart": {"result": None, "error": {"description": "No data found"}}})


def test_flatten_summary_matches_yfinance_keys():
    info = flatten_summary(SUMMARY_JSON)
    assert info["beta"] == 1.1 and info["sector"] == "Technology" and info["targetMeanPrice"] == 70.0
    assert info["recommendationKey"] == "buy"
    ins = insider_from_summary(info)
    assert ins == {"buy_trans": 2.0, "sell_trans": 9.0, "net_shares": -4000.0, "net_pct": -0.01}


def test_monthly_beta():
    idx = pd.date_range("2018-01-31", periods=80, freq="ME")
    market = pd.Series(100 * 1.01 ** np.arange(80), index=idx) * pd.Series([1, 1.02] * 40, index=idx)
    stock = market**1.5
    beta = monthly_beta(stock, market)
    assert beta == pytest.approx(1.5, rel=0.05)
    assert monthly_beta(stock.tail(10), market.tail(10)) is None


def test_fetch_market_without_profile_computes_beta():
    t = world(**{SUMMARY.format(symbol="TEST"): (401, {"finance": {"error": {"code": "Unauthorized"}}})})
    md = YahooHttpSource(t).fetch_market("TEST")
    assert md.price and md.info["longName"] == "TestCo Inc."
    assert "beta" in md.info
    assert any("profile unavailable" in n for n in md.notes)


def test_fx_rate_pence():
    t = FakeTransport(
        {
            CHART.format(symbol="USDGBP%3DX"): (
                200,
                chart_json(fixtures.price_history().tail(5), meta={"regularMarketPrice": 0.8}),
            )
        }
    )
    assert YahooHttpSource(t).fx_rate("USD", "GBp") == pytest.approx(80.0)
    assert YahooHttpSource(t).fx_rate("USD", "USD") == 1.0


# --------------------------------------------------------------------------------------
# Usage meter
# --------------------------------------------------------------------------------------
def test_usage_meter_counts_against_published_limits():
    now = [1000.0]
    meter = UsageMeter(clock=lambda: now[0])
    t = MeteredTransport(world(), meter)
    for _ in range(3):
        t.request("GET", fred.FRED_API, params={"series_id": "AAA", "api_key": "a" * 32})
    snap = meter.snapshot()
    assert snap["fred"]["total"] == 3 and snap["fred"]["remaining"] == 117
    now[0] += 61
    assert meter.snapshot()["fred"]["remaining"] == 120  # window rolled over
    assert service_of("https://data.sec.gov/x") == "sec"


# --------------------------------------------------------------------------------------
# Browser glue + full analysis on the browser code path
# --------------------------------------------------------------------------------------
class FakeXhr:
    def __init__(self, transport, log):
        self.t, self.log, self.headers = transport, log, {}

    def open(self, method, url, sync):
        assert sync is False
        self.method, self.url = method, url

    def setRequestHeader(self, k, v):
        self.headers[k] = v

    def send(self):
        self.log.append((self.method, self.url, dict(self.headers)))
        self.status, self.responseText = 200, "{}"

    def getResponseHeader(self, name):
        return None


def test_relay_url_and_headers():
    from valuelens.web.browser import XhrTransport, relay_url

    assert relay_url("https://relay.example/", FACTS_URL.format(cik=1)) == (
        "https://relay.example/sec/data/api/xbrl/companyfacts/CIK0000000001.json"
    )
    assert (
        relay_url("/relay", CHART.format(symbol="%5EGSPC"), {"range": "5d"})
        == "/relay/yahoo/q1/v8/finance/chart/%5EGSPC?range=5d"
    )
    with pytest.raises(TransportError):
        relay_url("/relay", "https://evil.example/x")
    log = []
    xt = XhrTransport("/relay", lambda: FakeXhr(None, log))
    xt.request("HEAD", TICKERS_URL, headers={"User-Agent": IDENT, "Cookie": "no"})
    assert log == [("HEAD", "/relay/sec/www/files/company_tickers.json", {"X-SEC-Identity": IDENT})]


def test_fred_key_travels_as_header_not_url():
    from valuelens.web.browser import XhrTransport

    log = []
    XhrTransport("/relay", lambda: FakeXhr(None, log)).request(
        "GET", fred.FRED_API, params={"series_id": "AAA", "api_key": "a" * 32}
    )
    _method, url, headers = log[0]
    assert "api_key" not in url and "a" * 32 not in url
    assert headers == {"X-FRED-Key": "a" * 32}


def test_relay_limit_is_reported_as_relay_limit():
    from valuelens.web.browser import XhrTransport

    class Limited(FakeXhr):
        def send(self):
            self.status, self.responseText = 429, "{}"

        def getResponseHeader(self, name):
            return "1" if name == "X-Relay-Limit" else None

    with pytest.raises(TransportError, match="relay limit"):
        XhrTransport("/relay", lambda: Limited(None, [])).request("GET", TICKERS_URL)


def test_non_latin_names_are_folded_for_the_header():
    from valuelens.data.sec import ascii_identity

    assert ascii_identity("Łukasz Ćorić  lukasz@example.com") == "Lukasz Coric lukasz@example.com"
    t = world(**{DOH_URL + "?name=example.com&type=MX": mx(["10 mx."])})
    res = check_identity("Jane Dœ jane@example.com", t)
    assert "sent as Jane Doe jane@example.com" in res.steps[0].detail


def test_dns_servfail_does_not_block():
    from valuelens.checks import mail_domain_status

    t = FakeTransport({DOH_URL: (200, {"Status": 2})})
    assert mail_domain_status("example.com", t)[0] is None
    aaaa_only = FakeTransport(
        {
            DOH_URL + "?name=v6.example&type=MX": (200, {"Status": 0}),
            DOH_URL + "?name=v6.example&type=A": (200, {"Status": 0}),
            DOH_URL + "?name=v6.example&type=AAAA": (
                200,
                {"Status": 0, "Answer": [{"type": 28, "data": "::1"}]},
            ),
        }
    )
    assert mail_domain_status("v6.example", aaaa_only)[0] is True


def test_xhr_network_failure_is_transport_error():
    from valuelens.web.browser import XhrTransport

    class Dead(FakeXhr):
        def send(self):
            raise RuntimeError("NetworkError")

    with pytest.raises(TransportError) as err:
        XhrTransport("/relay", lambda: Dead(None, [])).request(
            "GET", fred.FRED_API, params={"api_key": "a" * 32}
        )
    assert "a" * 32 not in str(err.value) and "NetworkError" not in str(err.value)


@pytest.fixture
def browser_session(tmp_path):
    from valuelens.web import browser

    t = world()
    json.loads(browser.start("/relay", str(tmp_path / "cache"), transport=t))
    yield browser, t
    browser._session = None


def call(browser, cmd, arg="", progress=None):
    return json.loads(browser.call(cmd, arg, progress))


def test_browser_flow_end_to_end(browser_session):
    browser, t = browser_session
    assert not call(browser, "ticker", "TEST")["ok"]  # identity first
    assert not call(browser, "analyze", "TEST")["ok"]

    r = call(browser, "identity", IDENT)
    assert r["ok"] and r["result"]["ok"]
    r = call(browser, "fred", "a" * 32)
    assert r["ok"] and r["result"]["ok"]
    assert "key" not in r["result"]["data"]  # the key is never echoed back
    assert "a" * 32 not in json.dumps(r)

    r = call(browser, "analyze", "TEST")
    assert not r["ok"] and "Check the ticker" in r["error"]

    r = call(browser, "ticker", "test")
    assert r["result"]["ok"], r
    steps = []
    r = call(browser, "analyze", "TEST", steps.append)
    assert r["ok"], r.get("error")
    res = r["result"]
    assert steps == ["market", "filings", "rates", "analysis"]
    assert res["signal"] in {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}
    assert "<span" in res["simple"] and "background-color" not in res["simple"][:0]
    usage = res["usage"]
    assert usage["fred"]["total"] == 3  # key check + AAA + DGS10
    assert usage["mode"]["fred"] == "key"
    assert usage["sec"]["total"] >= 3

    # Second run: filings and rates come from the cache.
    before = len(t.calls)
    assert call(browser, "analyze", "TEST")["ok"]
    new = [u for _, u, _ in t.calls[before:]]
    assert not [u for u in new if "sec.gov" in u or "stlouisfed" in u], new


def test_browser_without_fred_key_uses_public_csv(browser_session):
    browser, t = browser_session
    call(browser, "identity", IDENT)
    r = call(browser, "fred", "")
    assert r["result"]["ok"] and r["result"]["usage"]["mode"]["fred"] == "public"
    call(browser, "ticker", "TEST")
    assert call(browser, "analyze", "TEST")["ok"]
    assert t.urls("fredgraph.csv") and not t.urls("api.stlouisfed.org")


def test_browser_forget_clears_credentials(browser_session):
    browser, _ = browser_session
    call(browser, "identity", IDENT)
    call(browser, "fred", "")
    call(browser, "forget")
    assert not call(browser, "ticker", "TEST")["ok"]


# --------------------------------------------------------------------------------------
# Relay (Python twin of the Cloudflare Worker)
# --------------------------------------------------------------------------------------
class Upstream:
    """httpx2 MockTransport handler recording what the relay sends upstream."""

    def __init__(self):
        self.seen = []

    def __call__(self, request):
        import httpx2

        self.seen.append(request)
        host = request.url.host
        if host == "fc.yahoo.com":
            return httpx2.Response(404, headers={"set-cookie": "A3=d=abc; Domain=.yahoo.com; Path=/"})
        if request.url.path == "/v1/test/getcrumb":
            return httpx2.Response(200, text="CRUMB123")
        if "quoteSummary" in request.url.path:
            ok = request.url.params.get("crumb") == "CRUMB123" and "A3=d=abc" in request.headers.get(
                "cookie", ""
            )
            return httpx2.Response(200 if ok else 401, json=SUMMARY_JSON if ok else {})
        return httpx2.Response(200, json={"host": host, "path": request.url.path})


@pytest.fixture
def relay():
    import httpx2

    from valuelens.web.relay import Relay

    up = Upstream()
    client = httpx2.Client(transport=httpx2.MockTransport(up), follow_redirects=True)
    return Relay(client), up


def test_relay_sec_needs_identity(relay):
    r, up = relay
    path = "sec/data/api/xbrl/companyfacts/CIK0000320193.json"
    assert r.handle("GET", path, [], {}).status == 400
    res = r.handle("GET", path, [], {"X-SEC-Identity": IDENT})
    assert res.status == 200 and up.seen[-1].headers["user-agent"] == IDENT
    assert up.seen[-1].url.host == "data.sec.gov"


def test_relay_only_forwards_allowlisted_paths_and_params(relay):
    r, up = relay
    assert r.handle("GET", "sec/data/../../etc/passwd", [], {"X-SEC-Identity": IDENT}).status == 404
    assert r.handle("GET", "sec/www/cgi-bin/browse-edgar", [], {"X-SEC-Identity": IDENT}).status == 404
    assert r.handle("GET", "anything/else", [], {}).status == 404
    assert r.handle("POST", "fred/api/fred/series/observations", [], {}).status == 405
    assert r.handle("GET", "fred/api/fred/series/observations\n", [], {}).status == 404
    assert r.handle("GET", "yahoo/q1/v8/finance/chart/..%2Fv7%2Ffinance%2Fquote", [], {}).status == 404
    res = r.handle(
        "GET", "fred/api/fred/series/observations", [("api_key", "z" * 32)], {"X-FRED-Key": "a" * 32}
    )
    assert "api_key=" + "a" * 32 in str(up.seen[-1].url) and "z" * 32 not in str(up.seen[-1].url)
    res = r.handle("GET", "fred/api/fred/series/observations", [("series_id", "AAA"), ("evil", "1")], {})
    assert res.status == 200
    assert "evil" not in str(up.seen[-1].url) and "series_id=AAA" in str(up.seen[-1].url)


def test_relay_yahoo_summary_gets_crumb(relay):
    r, up = relay
    res = r.handle("GET", "yahoo/q2/v10/finance/quoteSummary/MSFT", [("modules", "price")], {})
    assert res.status == 200 and json.loads(res.body)["quoteSummary"]
    hosts = [s.url.host for s in up.seen]
    assert hosts[:2] == ["fc.yahoo.com", "query1.finance.yahoo.com"]
    r.handle("GET", "yahoo/q2/v10/finance/quoteSummary/AAPL", [("modules", "price")], {})
    assert [s.url.host for s in up.seen].count("fc.yahoo.com") == 1  # crumb reused


def test_relay_routes_cover_every_browser_route():
    from valuelens.web.browser import ROUTES
    from valuelens.web.relay import load_routes

    spec = load_routes()["routes"]
    for upstream, prefix in ROUTES.items():
        assert spec[prefix]["upstream"] == upstream


# --------------------------------------------------------------------------------------
# Site build and local server
# --------------------------------------------------------------------------------------
def test_python_bundle_contents():
    from valuelens.web.site import python_bundle

    data = python_bundle()
    names = zipfile.ZipFile(BytesIO(data)).namelist()
    assert "valuelens/web/browser.py" in names and "valuelens/defaults.toml" in names
    assert "valuelens/web/relay_routes.json" in names
    assert any(n.startswith("rich/") for n in names)
    assert not [n for n in names if "__pycache__" in n or n.startswith("valuelens/web/static/")]
    assert python_bundle() == data  # deterministic


def test_build_site(tmp_path):
    from valuelens.web.site import build_site

    out = build_site(tmp_path / "site", "https://relay.example.workers.dev/")
    files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert {"index.html", "app.js", "engine.mjs", "style.css", "config.js"} <= files
    cfg = (out / "config.js").read_text()
    assert '"relay": "https://relay.example.workers.dev"' in cfg and "pyodide/v" in cfg
    page = (out / "index.html").read_text()
    assert "__CSP__" not in page
    assert "connect-src 'self' https://cdn.jsdelivr.net https://relay.example.workers.dev" in page
    assert "'wasm-unsafe-eval' https://cdn.jsdelivr.net" in page
    bundle = json.loads(cfg.split("=", 1)[1].rstrip(";\n"))["bundle"]
    assert (out / bundle).is_file()
    with pytest.raises(SystemExit):
        build_site(tmp_path / "x", "http://insecure.example")


def test_local_server_serves_site_and_relay():
    from starlette.testclient import TestClient

    from valuelens.web.relay import RelayResponse
    from valuelens.web.server import create_app

    class StubRelay:
        def handle(self, method, path, query, headers):
            return RelayResponse(
                200, json.dumps({"path": path, "id": headers.get("x-sec-identity")}).encode()
            )

    client = TestClient(create_app(relay=StubRelay()), base_url="http://127.0.0.1")
    page = client.get("/")
    assert page.status_code == 200 and "VALUELENS" in page.text
    assert "Content-Security-Policy" in page.text and "__CSP__" not in page.text
    engine = client.get("/engine.mjs")
    assert engine.headers["content-type"].startswith("text/javascript")
    assert "wasm-unsafe-eval" in engine.headers["content-security-policy"]
    assert '"relay": "/relay"' in client.get("/config.js").text
    assert client.get("/py/valuelens.zip").content[:2] == b"PK"
    r = client.get("/relay/sec/www/files/company_tickers.json", headers={"X-SEC-Identity": IDENT})
    assert r.json() == {"path": "sec/www/files/company_tickers.json", "id": IDENT}
    assert client.get("/secret.txt").status_code == 404
    evil = TestClient(create_app(relay=StubRelay()), base_url="http://evil.example")
    assert evil.get("/").status_code == 400


def test_checks_run_without_numpy_or_pandas(tmp_path):
    """The browser answers stage 1 and 2 before numpy/pandas have downloaded."""
    import subprocess
    import sys
    from pathlib import Path

    code = f"""
import sys, json
sys.path.insert(0, {str(Path(__file__).parent)!r})
import fixtures
from test_web import world, IDENT, chart_json, CHART
t = world(**{{CHART.format(symbol="TEST"): (200, chart_json(fixtures.price_history().tail(5)))}})
for name in [m for m in sys.modules if m.split(".")[0] in ("valuelens", "numpy", "pandas")]:
    del sys.modules[name]
sys.modules["numpy"] = None
sys.modules["pandas"] = None
from valuelens.web import browser
browser.start("/relay", {str(tmp_path)!r}, transport=t)
r = [json.loads(browser.call(c, a)) for c, a in [("identity", IDENT), ("fred", ""), ("ticker", "TEST")]]
assert all(x["ok"] and x["result"]["ok"] for x in r), r
print("ok")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr[-2000:]
