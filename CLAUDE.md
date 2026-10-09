# ValueLens — notes for Claude Code

Single-stock value analysis: Graham + Buffett criteria, intrinsic value, forensic scores,
factors, technical timing overlay, combined verdict. CLI (`valuelens`) and a website (GitHub
Pages + Pyodide in the browser + a Cloudflare Worker relay; `valuelens-dashboard` serves the same
site locally with a Python relay).

## Commands
- Install: `uv sync --extra dev` (uv manages Python and .venv; commit `uv.lock`)
- Website locally: `uv run valuelens-dashboard` (site + local relay on 127.0.0.1:8765)
- Build the static site: `uv run python -m valuelens.web.site --relay https://<worker> --out _site`
- Relay tests: `cd relay && node --test`; deploy: `npx wrangler deploy`
- Run: `uv run valuelens MSFT` / `--json` (needs `[sec] user_agent` in valuelens.toml or
  `VALUELENS_SEC_USER_AGENT="Name email"`)
- Test: `uv run pytest` (fully offline). Lint/format: `uv run ruff check . && uv run ruff format --check .`

## Publishing (the README is for visitors only: what it is + link to the site)
- Site: https://al3grus.github.io/Value-Lens/. Pages source is GitHub Actions
  (`.github/workflows/pages.yml`, runs on push to main); it needs the repository variable
  `RELAY_URL` = the relay Worker's https address (`gh variable set RELAY_URL --body <url>`).
- Relay: `cd relay; npx wrangler deploy`. `ALLOWED_ORIGINS` in `relay/wrangler.jsonc` is the
  site origin (`https://al3grus.github.io`, no path). Free plan: 100,000 requests/day; each
  visitor is limited to 120/min by the `RATE_LIMITER` binding.
- Deploy order when routes change: push the site first, then deploy the relay.

## Architecture
- `data/` — network + normalisation only. `sec.py` parses XBRL companyfacts (annual = 340–390-day
  periods from 10-K/20-F, latest filing wins; TTM = FY + YTD − prior YTD with per-part filing dates
  for split adjustment). `concepts.py` maps canonical fields to ordered US-GAAP concept lists.
  `normalize.py` derives canonical columns. `bundle.py` assembles a `DataBundle` (split, FX,
  ADR/share-class reconciliation).
- `analysis/` — pure functions of `DataBundle` + config → scorecards/valuation/decision. No I/O.
- `report/` — `glossary.py` (all plain-English wording: per-check meanings, strengths/concerns
  sentences, headline), `content.py` (what the views say as plain data: rows, ranges, tones;
  shared by the terminal views and `web/render.py`), `simple.py` and `detailed.py` (rich renderers), `style.py` (palette,
  badges, bars, gauge), `progress.py` (live loading checklist), `json_out.py` (JSON incl. summary
  and glossary). `engine.py` wires it together; `cli.py` is the entry point.
- `data/http.py` — every request goes through a `Transport` (`HttpxTransport` on CPython,
  `web.browser.XhrTransport` in the browser). Never import an HTTP library elsewhere.
  `data/usage.py` counts requests per service against published limits (SEC 10/s, FRED 120/min).
  `data/bundle.Sources` picks the market source: `YFinanceSource` (CLI) or `YahooHttpSource`
  (browser; chart + quoteSummary endpoints, statements from SEC only).
- `checks.py` — identity (format, typo domains, DNS-over-HTTPS MX, SEC HEAD), ticker (Yahoo
  quote, SEC ticker list). The website asks only for name + e-mail; its rates always come from
  FRED's public CSV (same figures as the keyed API), so the relay has no FRED-API route. Must stay importable without numpy/pandas: the
  browser answers stages 1–2 before those finish downloading (`tests/test_web.py` enforces it).
- `web/` — `static/` (index.html, app.js, engine.mjs worker, style.css; no build step, CSP meta),
  `render.py` (report as HTML; borders, bars and gauge are CSS `.rp` rules in style.css, never
  box-drawing characters; every text node html-escaped), `session.py` (one visitor, identity in memory only), `browser.py` (Pyodide entry points),
  `relay.py` + `relay_routes.json` (the one allow-list, also imported by `relay/src/index.js`;
  keep `browser.ROUTES` in sync), `site.py` (static build + Python zip, Pyodide version pin; the
  page loads its files as `name?v=<site version>` and checks `version.json` uncached, so a tab
  restored from cache updates itself), `server.py` (local, `no-store`, no version).
- `relay/` — Cloudflare Worker (wrangler.jsonc), node:test tests. CORS only for ALLOWED_ORIGINS.
- Browser target is Pyodide (Python 3.14, pandas 3.0.x, numpy 2.4.x); CI tests 3.14.
- HTTP client is `httpx2` (maintained successor of httpx, also what Starlette's TestClient uses).
- Report glyphs are restricted to WGL4 (`style.WGL4_SAFE` + light box drawing) so borders align
  in every monospace font; `tests/test_views.py` enforces it.
- Rendering uses `rich` with markup disabled (`text.make_console`): build `Text` objects, never
  markup strings. `--ascii` output is guaranteed ASCII by `AsciiFile`.
- All thresholds and weights live in `src/valuelens/defaults.toml`; never hard-code a threshold.

## Conventions
- Missing data → `Status.NA` with a reason; never let missing data improve a score
  (decision.py caps BUY at HOLD when coverage < 60% or only one valuation method is available).
- A short record that fails a test (e.g. 5 years of dividends vs 20 required) is FAIL, not N/A.
- Money columns are converted to the price currency; `eps`, `shares`, `shares_outstanding` are not.
- New metrics need a unit test in `tests/` using the synthetic fixture (`tests/fixtures.py`).
- Every new check needs entries in `report/glossary.py` (`MEANING`, `PLAIN`, `THEME`);
  `tests/test_views.py` enforces this.
