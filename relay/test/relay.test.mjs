// Offline tests for the relay Worker: `node --test test/` (Node 22+).
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { handle, matchRoute, newSession, readSession, resetCrumb } from "../src/index.js";

const ORIGIN = "https://someone.github.io";
const IDENT = "Jane Doe jane@example.com";
const ENV = { ALLOWED_ORIGINS: `${ORIGIN}, http://127.0.0.1:8765/` };

function req(path, { method = "GET", origin = ORIGIN, headers = {}, body } = {}) {
  const h = new Headers(headers);
  if (origin) h.set("Origin", origin);
  return new Request(`https://relay.example.workers.dev/${path}`, { method, headers: h, body });
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
  assert.equal(ok.headers.get("Access-Control-Allow-Headers"), "X-SEC-Identity, X-Relay-Session, Accept");
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

test("the SEC ticker list (the identity check) always reaches the SEC: no edge cache", async () => {
  const cache = new MemCache();
  const { fetch, calls } = upstream(() => new Response('{"0":{}}', { headers: { "Content-Type": "application/json" } }));
  for (let i = 0; i < 2; i++) {
    const res = await handle(req("sec/www/files/company_tickers.json", { headers: { "X-SEC-Identity": IDENT } }), ENV, {}, { fetch, cache });
    assert.equal(res.status, 200);
    assert.equal(res.headers.get("X-Relay-Cache"), null);
  }
  assert.equal(calls.length, 2);
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

// ---- Turnstile sessions --------------------------------------------------------------------
const SECRETS = { ...ENV, TURNSTILE_SECRET: "ts-secret", SESSION_KEY: "k".repeat(43) };

function siteverify(success) {
  return upstream(async (url, init) => {
    assert.equal(url, "https://challenges.cloudflare.com/turnstile/v0/siteverify");
    const form = init.body instanceof URLSearchParams ? init.body : new URLSearchParams(String(init.body));
    return new Response(JSON.stringify({ success: success && form.get("secret") === "ts-secret" && form.get("response") === "tok" }));
  });
}

test("without the Turnstile secrets the relay works as before and has no sessions", async () => {
  const { fetch } = upstream(() => new Response("x"));
  const res = await handle(req("fred/web/graph/fredgraph.csv?id=AAA"), ENV, {}, { fetch, cache: null });
  assert.equal(res.status, 200);
  const s = await handle(req("session", { method: "POST", body: "tok" }), ENV, {}, { fetch, cache: null });
  assert.equal(s.status, 404);
});

test("a passed Turnstile check is swapped for a session; a failed one is refused", async () => {
  const good = siteverify(true);
  const headers = { "CF-Connecting-IP": "203.0.113.9" };
  const res = await handle(req("session", { method: "POST", body: "tok", headers }), SECRETS, {}, { fetch: good.fetch, cache: null });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  const out = await res.json();
  assert.equal(out.expires_in, 3600);
  assert.ok(await readSession(SECRETS.SESSION_KEY, out.session));
  assert.equal(new URLSearchParams(String(good.calls[0].init.body)).get("remoteip"), "203.0.113.9");

  const bad = siteverify(false);
  const no = await handle(req("session", { method: "POST", body: "tok" }), SECRETS, {}, { fetch: bad.fetch, cache: null });
  assert.equal(no.status, 403);
  const empty = await handle(req("session", { method: "POST", body: "" }), SECRETS, {}, { fetch: bad.fetch, cache: null });
  assert.equal(empty.status, 400);
  assert.equal(bad.calls.length, 1); // an empty token never reaches siteverify
  const get = await handle(req("session"), SECRETS, {}, { fetch: bad.fetch, cache: null });
  assert.equal(get.status, 405);
});

test("sessions only for the allowed origins, and limited per address", async () => {
  const { fetch, calls } = siteverify(true);
  const other = await handle(req("session", { method: "POST", body: "tok", origin: "https://evil.example" }), SECRETS, {}, { fetch, cache: null });
  assert.equal(other.status, 403);
  const env = { ...SECRETS, SESSION_LIMITER: { limit: async () => ({ success: false }) } };
  const limited = await handle(req("session", { method: "POST", body: "tok" }), env, {}, { fetch, cache: null });
  assert.equal(limited.status, 429);
  assert.equal(calls.length, 0);
});

test("with sessions on, data requests need a valid, unexpired session", async () => {
  const { fetch, calls } = upstream(() => new Response("x"));
  const path = "fred/web/graph/fredgraph.csv?id=AAA";
  const missing = await handle(req(path), SECRETS, {}, { fetch, cache: null });
  assert.equal(missing.status, 401);
  assert.equal(missing.headers.get("X-Relay-Session-Expired"), "1");
  assert.equal(missing.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.match(missing.headers.get("Access-Control-Expose-Headers"), /X-Relay-Session-Expired/);

  const session = await newSession(SECRETS.SESSION_KEY);
  const ok = await handle(req(path, { headers: { "X-Relay-Session": session } }), SECRETS, {}, { fetch, cache: null });
  assert.equal(ok.status, 200);
  assert.equal(calls.length, 1);

  const forged = await newSession("another key");
  const expired = await newSession(SECRETS.SESSION_KEY, Date.now() - 3601 * 1000);
  const [exp, id, sig] = session.split(".");
  const stretched = `${Number(exp) + 86400}.${id}.${sig}`;
  for (const s of [forged, expired, stretched, "garbage", `${session}x`]) {
    const res = await handle(req(path, { headers: { "X-Relay-Session": s } }), SECRETS, {}, { fetch, cache: null });
    assert.equal(res.status, 401, s);
  }
  assert.equal(calls.length, 1);
});

test("the rate limit counts per session, not per address", async () => {
  const keys = [];
  const env = { ...SECRETS, RATE_LIMITER: { limit: async ({ key }) => { keys.push(key); return { success: true }; } } };
  const { fetch } = upstream(() => new Response("x"));
  const a = await newSession(SECRETS.SESSION_KEY);
  const b = await newSession(SECRETS.SESSION_KEY);
  for (const s of [a, b]) {
    await handle(req("fred/web/graph/fredgraph.csv?id=AAA", { headers: { "X-Relay-Session": s, "CF-Connecting-IP": "198.51.100.1" } }), env, {}, { fetch, cache: null });
  }
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1]);
  assert.ok(keys.every((k) => k.startsWith("session:")));
});

test("health reports whether sessions are on", async () => {
  const off = await (await handle(req("health", { origin: null }), ENV)).json();
  const on = await (await handle(req("health", { origin: null }), SECRETS)).json();
  assert.equal(off.sessions, "none");
  assert.equal(on.sessions, "Cloudflare Turnstile");
});
