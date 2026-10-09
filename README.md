# ValueLens

Type a ticker, get a value-investing verdict built on Benjamin Graham's defensive-investor
rules, Warren Buffett's business-quality tests, academically-tested factors, forensic
accounting scores and an intrinsic-value estimate — combined into **STRONG BUY / BUY / HOLD /
SELL / STRONG SELL** with the reasons spelled out.

```
valuelens MSFT GOOG META ADBE
```

> Educational tool, not investment advice.

## Install

The project is managed with [uv](https://docs.astral.sh/uv/), which also installs a suitable
Python (3.11+) for you if none is present.

1. Install uv once (pick one):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
   winget install --id=astral-sh.uv -e                                                   # Windows
   scoop install main/uv                                                                 # Windows
   curl -LsSf https://astral.sh/uv/install.sh | sh                                       # macOS/Linux
   ```

   Open a new terminal afterwards so `uv` is on your PATH.

2. In the project folder:

   ```bash
   uv sync --extra dev        # creates .venv, installs everything, writes uv.lock (commit it)
   ```

No virtualenv activation is needed: prefix commands with `uv run`.

### One-time setup: SEC identity

Financial statements come straight from the SEC's XBRL API (free, no key), which requires a
User-Agent naming you and an e-mail. Copy the example config and fill it in (the file is
git-ignored):

```powershell
Copy-Item valuelens.example.toml valuelens.toml    # Windows; macOS/Linux: cp valuelens.example.toml valuelens.toml
```

```toml
[sec]
user_agent = "Your Name you@example.com"
```

Alternatively set the `VALUELENS_SEC_USER_AGENT` environment variable.

Optionally add a free [FRED API key](https://fredaccount.stlouisfed.org/apikeys) for interest
rates (without one, FRED's public CSV is used):

```toml
[fred]
api_key = "your 32-character key"
```

or set `VALUELENS_FRED_API_KEY`. (The website asks only for your name and e-mail, in each browser
session, and always uses FRED's public download.)

## Usage

```bash
uv run valuelens MSFT                 # simple view: verdict, fair value, scorecard, strengths/concerns in plain English
uv run valuelens MSFT -d              # detailed view: every number, each with a "what it means" note
uv run valuelens MSFT GOOG META       # several stocks + a side-by-side comparison table
uv run valuelens MSFT --json          # machine-readable output incl. plain-English summary (for the website/API)
uv run valuelens MSFT --ascii         # pure-ASCII output for old consoles
uv run valuelens MSFT --no-animation  # no loading animation
uv run valuelens KO --config my.toml  # override thresholds
uv run valuelens MSFT --no-cache      # force fresh downloads
```

The default view can be changed in `valuelens.toml`:

```toml
[report]
view = "detailed"     # or "simple"
animations = false
```

While data loads, an interactive terminal shows a live checklist (price data, SEC filings,
interest rates, analysis) that disappears once the report is ready. Colours follow the
`NO_COLOR` convention, and output piped to a file is plain text.

## Website

The same analysis as a web page anyone can use with just a name and e-mail: no account, no
key. It is hosted for free on GitHub Pages and runs entirely in the visitor's browser (Python
compiled to WebAssembly with [Pyodide](https://pyodide.org)). Three stages:

1. **Your details.** Name and e-mail, which the SEC asks every automated tool to send. They are
   checked before anything else runs: the e-mail's domain must exist and accept mail (common
   typos such as `gmial.com` are caught) and the SEC must accept the identity. They stay in the
   browser tab and are never stored.
2. **Ticker.** One small price request plus the SEC ticker list confirm the company exists, is
   a company share (not an ETF or fund) and files with the SEC. *RUN ANALYSIS* only appears after
   a successful check, so a typo never spends requests.
3. **Report.** Live step log, then the simple and detailed reports, then *NEW TICKER* or *EXIT*
   (which clears the name and e-mail). The reports are HTML and CSS
   ([`web/render.py`](src/valuelens/web/render.py)) with the same content and wording as the
   terminal views, so they line up in any browser and font.

| Service | Published limit | One analysis uses |
|---|---|---|
| SEC EDGAR | 10 requests per second, no daily cap | about 3 (cached 24 h in the browser) |
| FRED public download | none published | 2 (cached 24 h) |
| Yahoo Finance | none published; bursts are throttled | about 2 |

Interest rates come from FRED's public download, which gives the same figures as its key-based
API, so visitors never need a FRED account. (The command-line version can still use a key.)

### Why there is a relay

SEC, FRED and Yahoo do not allow web pages on other sites to call them (no CORS; the SEC says
so explicitly), and browsers cannot set the User-Agent header the SEC requires. A small
[Cloudflare Worker](relay/src/index.js) forwards the page's requests. It only forwards the paths
and parameters listed in [`relay_routes.json`](src/valuelens/web/relay_routes.json), only for
your site's address, limits each visitor to 120 requests per minute and stores nothing. The
free Workers plan allows 100,000 requests per day. If you give the Worker a custom domain, public SEC filings are also cached at
Cloudflare's edge for 6 hours (Cloudflare's cache does not apply on `workers.dev` addresses).

The Origin check stops other websites, not scripts: anyone could call the relay directly within
the per-visitor limit. If that ever becomes a problem, add Cloudflare Turnstile in front of it.

### Publish it (one time)

1. **Relay.** Install [Node.js](https://nodejs.org) 22+, then in `relay/`:
   ```bash
   npm install
   # edit wrangler.jsonc: ALLOWED_ORIGINS = "https://<your-github-name>.github.io"
   npx wrangler login        # free Cloudflare account
   npx wrangler deploy       # prints https://valuelens-relay.<you>.workers.dev
   ```
2. **GitHub.** In the repository: *Settings → Pages → Source: GitHub Actions*, then
   *Settings → Secrets and variables → Actions → Variables → New variable*:
   `RELAY_URL` = the address printed by `wrangler deploy`.
3. Push to `main`. The *Website* workflow tests, builds and publishes the site to
   `https://<your-github-name>.github.io/<repository>/`.

GitHub Pages is free for public repositories.

### Run it on your computer

```bash
uv run valuelens-dashboard            # same site + a local relay at http://127.0.0.1:8765
```

No Cloudflare account needed; the relay runs inside this local server.

### Website limitations

* Statements come from SEC filings only; companies that do not file with the SEC (most
  non-US listings) need the command-line version, which can fall back to Yahoo statements.
* The first visit downloads Python, numpy and pandas (tens of megabytes); browsers cache them afterwards.
* If Yahoo's company profile is unavailable, beta is computed from five years of monthly prices
  against the S&P 500 and the analyst/short-interest section is left out (it never affects the
  verdict).

## What the report contains

| Section | What it answers |
|---|---|
| **Graham defensive criteria** (8 checks) | Is this a large, financially strong, consistently profitable, dividend-paying company at a modest price? |
| **Buffett quality criteria** (12 quality + 2 price checks) | Does the business have durable economics — high returns on capital, pricing power, low capital needs, honest capital allocation? |
| **Intrinsic value** | Four methods, blended into a fair value with bear/bull range, margin of safety, and the growth the market is already pricing in |
| **Financial health & forensics** | Piotroski F-score, Altman Z-score, Beneish M-score — catches value traps and accounting red flags |
| **Evidence-based factors** | Gross profitability, Greenblatt return on capital & earnings yield, shareholder yield, asset growth |
| **Price trend** | 50/200-day averages, 12-1 momentum, RSI — used only to time entries, never to judge the business |
| **Market sentiment** | Analyst targets, short interest, insider activity — shown for context, not scored |
| **Combined verdict** | Weighted composite + guardrails + timing note + confidence level |

## Methodology

### Graham (The Intelligent Investor, ch. 14)
1. Revenue ≥ $2B (Graham's $100M of 1971, inflation-adjusted)
2. Current ratio ≥ 2 · 3. Long-term debt ≤ net current assets *(utilities: debt ≤ 2× equity; n/a for banks/insurers)*
4. Positive earnings in each of the last 10 years
5. Uninterrupted dividends for 20 years — a short record is a **FAIL**, not N/A
6. EPS up ≥ 33% over ~10 years, comparing 3-year averages at each end (split-adjusted)
7. Price ≤ 15× average EPS of the last 3 years · 8. P/B ≤ 1.5 or P/E × P/B ≤ 22.5

Graham's formula `V = EPS × (8.5 + 2g) × 4.4 / Y` uses the **current** Moody's AAA yield from
FRED for Y (not a fixed 4.4) and caps g at 15%.

### Buffett
Positive FCF in ≥ 8 of 10 years · ROE ≥ 15% and ROIC ≥ 12% (10-year medians) · interest coverage
≥ 5× · net debt/EBITDA ≤ 3 · gross margin ≥ 40% and operating margin ≥ 15% (10-year medians) ·
revenue and owner-earnings CAGR ≥ 5% · no share dilution · the **one-dollar test** (each dollar
retained must create ≥ $1 of market value, 1984 letter) · capex ≤ 50% of net income · EV/FCF ≤ 25 ·
P/E ≤ 25.

### Intrinsic value
| Method | Idea | Default weight |
|---|---|---|
| DCF on owner earnings | Buffett's owner earnings — operating cash flow − **maintenance** capex (Greenwald estimate; growth capex is not deducted) − **stock-based compensation** — plus after-tax interest (unlevered), 5 years of growth fading over 5 more to 2.5%, discounted at WACC (cost of equity = 10y Treasury + β × 5%; min 8%), minus net debt | 60% |
| Graham formula | Graham's growth formula with live AAA yield | 20% |
| Earnings power value | Normalised after-tax EBIT / WACC + net cash — the value with zero growth | 20% |
| Graham number | √(22.5 × EPS × book value per share) — very conservative | 0% (50% for financials) |

**Core earnings.** When non-operating items (investment gains/losses, FX, one-offs) exceed 15%
of operating profit, P/E, the Graham formula, the Graham number and EPS growth use core EPS
(operating profit after interest and tax) instead of reported EPS, and the report says so.

The **reverse DCF** solves for the growth rate that justifies today's price and compares it with
what the company actually delivered. If the market is pricing in 18%/yr and the company has
compounded at 9%, the report says so.

### Financial health
* **Piotroski F-score** (0–9): ≥ 7 strong, ≤ 2 is a red flag.
* **Altman Z** for manufacturers (SIC 2000–3999), **Z''** for everyone else; "distress" is a red flag.
* **Beneish M-score** > −1.78 suggests possible earnings manipulation (red flag), unless sales grew
  > 50% — that alone pushes the score up, so it is reported as "growth-driven" instead.

### Combined verdict
Composite (0–100) = 35% quality (Buffett) + 25% safety (Graham financial strength, Piotroski,
Altman) + 15% factors + 25% valuation (margin of safety mapped from −50%…+50% to 0…100).

Guardrails, in order:
* No reliable fair value → at most **HOLD**.
* BUY or better needs a positive margin of safety; STRONG BUY needs ≥ 20%.
* Missing data never upgrades a verdict: BUY needs ≥ 60% of criteria evaluable and at least two
  valuation methods, otherwise **HOLD**.
* One accounting red flag → at most **HOLD**; two or more → at most **SELL**.
* A downtrend turns STRONG BUY into BUY with advice to scale in.

**Confidence** (HIGH/MEDIUM/LOW) reflects data coverage, years of history and how much the
valuation methods disagree.

All thresholds and weights live in [`src/valuelens/defaults.toml`](src/valuelens/defaults.toml)
and can be overridden from `valuelens.toml`.

## Data sources

| Data | Source | Key needed? |
|---|---|---|
| 10+ years of annual statements, latest 10-Q for TTM | SEC EDGAR XBRL `companyfacts` | No — User-Agent only |
| Price, history, splits, dividends, profile, sentiment | Yahoo Finance via `yfinance` | No |
| AAA corporate yield, 10y Treasury | FRED official API with your key, else FRED public CSV (the website always uses the CSV) | Optional (free), command line only |
| Statements for non-SEC filers (e.g. most European listings) | Yahoo Finance (≈4–5 years) | No |

SEC data is parsed with care for the things that commonly go wrong: concept changes over the
years (e.g. ASC 606 revenue), restatements (latest filing wins), stock splits (values are
adjusted only for splits that happened *after* the filing date), quarterly facts embedded in
10-Ks, 52/53-week fiscal years, fiscal-year changes, multi-class share structures and ADRs
(filings count ordinary shares, the price is per ADS — per-share data is rescaled to the traded
security). Responses are cached for 24h
in `%LOCALAPPDATA%\valuelens\cache` (Windows) or `~/.cache/valuelens`.

## Limitations
* Banks and insurers skip industrial metrics (FCF, margins, debt ratios); REIT earnings understate cash flow (FFO is not computed).
* Non-SEC companies get only ~4–5 years from Yahoo, so 10-year tests are marked accordingly.
* Factor thresholds are absolute rules of thumb; the original research ranks across the whole market.
* Qualitative judgement — management, moat durability, competition — is up to you.

## Development

```bash
uv run pytest                                   # offline tests (synthetic SEC fixture)
uv run ruff check . && uv run ruff format --check .
cd relay && node --test                         # relay Worker tests
```

Project layout:

```
src/valuelens/
  data/        sec.py (EDGAR client + XBRL parser), concepts.py (GAAP concept map), yahoo.py,
               fred.py, normalize.py (canonical columns), bundle.py (assembles everything)
  analysis/    graham.py, buffett.py, valuation.py, health.py, factors.py, technicals.py,
               sentiment.py, decision.py, signals.py, metrics.py
  report/      content.py (what the reports say, shared), simple.py and detailed.py (console
               views), glossary.py (wording), text.py (console entry), json_out.py (API output)
  checks.py    identity and ticker checks run before an analysis
  web/         website: static/ (page, worker), render.py (report as HTML), session.py,
               browser.py (Pyodide glue), relay.py (local relay), site.py (static build),
               server.py (local server)
  engine.py    analyze(ticker) -> Report
  cli.py       command-line entry point
relay/         Cloudflare Worker relay for the hosted website
```

## Roadmap
* Optional paid data providers (FMP, EODHD) for deeper non-US history.
* Peer-relative scoring (rank factors within sector instead of absolute thresholds).

## License
MIT
