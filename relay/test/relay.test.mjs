// Offline tests for the relay Worker: `node --test test/` (Node 22+).
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { handle, matchRoute, resetCrumb } from "../src/index.js";

const ORIGIN = "https://someone.github.io";
const IDENT = "Jane Doe jane@example.com";
const ENV = { ALLOWED_ORIGINS: `${ORIGIN}, http://127.0.0.1:8765/` };

function req(path, { method = "GET", origin = ORIGIN, headers = {} } = {}) {
  const h = new Headers(headers);
  if (origin) h.set("Origin", origin);
  return new Request(`https://relay.example.workers.dev/${path}`, { method, headers: h });
}

function upstream(handler) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  };
  return { fetch, calls };
}

class MemCache {
  constructor() { this.store = new Map(); }
  async match(r) { const v = this.store.get(r.url); return v ? v.clone() : undefined; }
  async put(r, res) { this.store.set(r.url, res); }
}

beforeEach(() => resetCrumb());

test("route allowlist", () => {
  assert.ok(matchRoute("sec/data/api/xbrl/companyfacts/CIK0000320193.json"));
  assert.ok(matchRoute("yahoo/q1/v8/finance/chart/%5EGSPC"));
  assert.equal(matchRoute("sec/data/api/xbrl/frames/x.json"), null);
  assert.equal(matchRoute("sec/www/cgi-bin/browse-edgar"), null);
  assert.equal(matchRoute("other/x"), null);
  assert.equal(matchRoute("fred/api/fred/series/observations"), null); // public download only, no key route
});

test("preflight only for allowed origins", async () => {
  const ok = await handle(req("sec/www/files/company_tickers.json", { method: "OPTIONS" }), ENV);
  assert.equal(ok.status, 204);
  assert.equal(ok.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.equal(ok.headers.get("Access-Control-Allow-Headers"), "X-SEC-Identity, Accept");
  const bad = await handle(req("sec/www/files/company_tickers.json", { method: "OPTIONS", origin: "https://evil.example" }), ENV);
  assert.equal(bad.status, 403);
});

test("refuses other origins and missing origin", async () => {
  const { fetch, calls } = upstream(() => new Response("{}"));
  for (const origin of ["https://evil.example", null]) {
    const res = await handle(req("fred/web/graph/fredgraph.csv?id=AAA", { origin }), ENV, {}, { fetch, cache: null });
    assert.equal(res.status, 403);
  }
  assert.equal(calls.length, 0);
});

test("SEC: identity required and sent as User-Agent; HEAD has no body", async () => {
  const { fetch, calls } = upstream(() => new Response('{"0":{}}', { headers: { "Content-Type": "application/json" } }));
  const missing = await handle(req("sec/www/files/company_tickers.json"), ENV, {}, { fetch, cache: null });
  assert.equal(missing.status, 400);
  const res = await handle(req("sec/www/files/company_tickers.json", { method: "HEAD", headers: { "X-SEC-Identity": IDENT } }), ENV, {}, { fetch, cache: null });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "");
  assert.equal(calls[0].url, "https://www.sec.gov/files/company_tickers.json");
  assert.equal(calls[0].init.headers["User-Agent"], IDENT);
  assert.equal(calls[0].init.method, "HEAD");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
});

test("SEC filings are cached at the edge without the identity", async () => {
  const cache = new MemCache();
  const { fetch, calls } = upstream(() => new Response('{"facts":{}}', { headers: { "Content-Type": "application/json" } }));
  const path = "sec/data/api/xbrl/companyfacts/CIK0000320193.json";
  const a = await handle(req(path, { headers: { "X-SEC-Identity": IDENT } }), ENV, {}, { fetch, cache });
  const b = await handle(req(path, { headers: { "X-SEC-Identity": "John Roe john@example.org" } }), ENV, {}, { fetch, cache });
  assert.equal(a.headers.get("X-Relay-Cache"), "MISS");
  assert.equal(b.headers.get("X-Relay-Cache"), "HIT");
  assert.equal(await b.text(), '{"facts":{}}');
  assert.equal(calls.length, 1);
});

test("only allowlisted query parameters are forwarded", async () => {
  const { fetch, calls } = upstream(() => new Response("observation_date,AAA\n2026-09-01,6.03\n"));
  await handle(req(`fred/web/graph/fredgraph.csv?id=AAA&api_key=${"z".repeat(32)}&callback=x`), ENV, {}, { fetch, cache: null });
  const u = new URL(calls[0].url);
  assert.equal(u.origin + u.pathname, "https://fred.stlouisfed.org/graph/fredgraph.csv");
  assert.equal(u.search, "?id=AAA");
});

test("encoded slashes in Yahoo symbols are refused", () => {
  assert.equal(matchRoute("yahoo/q1/v8/finance/chart/..%2F..%2Fv7%2Ffinance%2Fquote"), null);
  assert.ok(matchRoute("yahoo/q1/v8/finance/chart/EURUSD%3DX"));
  assert.ok(matchRoute("yahoo/q2/v10/finance/quoteSummary/BRK-B"));
});

test("CORS responses expose the relay headers", async () => {
  const { fetch } = upstream(() => new Response("x"));
  const res = await handle(req("fred/web/graph/fredgraph.csv?id=AAA"), ENV, {}, { fetch, cache: null });
  assert.match(res.headers.get("Access-Control-Expose-Headers"), /X-Relay-Limit/);
});

test("upstream status codes pass through (Yahoo 404 for an unknown symbol)", async () => {
  const { fetch } = upstream(() => new Response('{"chart":{"error":{"code":"Not Found"}}}', { status: 404 }));
  const res = await handle(req("yahoo/q1/v8/finance/chart/NOSUCH?range=5d"), ENV, {}, { fetch, cache: null });
  assert.equal(res.status, 404);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
});

test("Yahoo quoteSummary gets cookie + crumb, refreshed once on 401", async () => {
  let crumbs = 0;
  const { fetch, calls } = upstream((url, init) => {
    if (url === "https://fc.yahoo.com") return new Response("", { status: 404, headers: { "Set-Cookie": "A3=d=abc; Domain=.yahoo.com; Path=/; Secure" } });
    if (url.endsWith("/v1/test/getcrumb")) { crumbs += 1; return new Response(`CRUMB${crumbs}`); }
    const u = new URL(url);
    const ok = u.searchParams.get("crumb") === "CRUMB2" && init.headers.Cookie === "A3=d=abc";
    return new Response(ok ? '{"quoteSummary":{"result":[{}]}}' : "{}", { status: ok ? 200 : 401 });
  });
  const res = await handle(req("yahoo/q2/v10/finance/quoteSummary/MSFT?modules=price"), ENV, {}, { fetch, cache: null });
  assert.equal(res.status, 200);
  assert.equal(crumbs, 2);
  assert.ok(calls.every((c) => /Mozilla/.test(c.init.headers["User-Agent"])));
});

test("rate limiter returns 429 with CORS", async () => {
  const env = { ...ENV, RATE_LIMITER: { limit: async () => ({ success: false }) } };
  const res = await handle(req("fred/web/graph/fredgraph.csv?id=AAA"), env, {}, { fetch: () => assert.fail("no upstream call"), cache: null });
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("X-Relay-Limit"), "1");
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
});

test("network failure becomes 502", async () => {
  const fetch = async () => { throw new TypeError("network"); };
  const res = await handle(req("yahoo/q1/v8/finance/chart/MSFT?range=5d"), ENV, {}, { fetch, cache: null });
  assert.equal(res.status, 502);
});
