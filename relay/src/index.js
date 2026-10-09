// ValueLens relay: a small allow-listed proxy that lets the website on GitHub Pages reach SEC
// EDGAR, FRED and Yahoo Finance, which do not allow requests from other websites (no CORS).
//
// It forwards only the paths and query parameters listed in relay_routes.json (shared with the
// Python twin in src/valuelens/web/relay.py), only for the origins in ALLOWED_ORIGINS, and stores
// nothing. The SEC identity arrives as X-SEC-Identity (browsers cannot set User-Agent) and leaves
// as the User-Agent the SEC requires. Interest rates come from FRED's public download, so no
// API key ever passes through here.

import SPEC from "../../src/valuelens/web/relay_routes.json" with { type: "json" };

const IDENTITY_RE = new RegExp(SPEC.identity_pattern);
const ROUTES = Object.entries(SPEC.routes).map(([prefix, r]) => ({
  prefix,
  ...r,
  patterns: r.paths.map((p) => new RegExp(p)),
}));
const YAHOO_COOKIE_URL = "https://fc.yahoo.com";
const YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb";
const CRUMB_TTL_MS = 3600 * 1000;

let crumbCache = null; // { cookie, crumb, at } — per isolate, refreshed on 401/403

export function resetCrumb() {
  crumbCache = null;
}

function allowedOrigins(env) {
  return (env.ALLOWED_ORIGINS || "")
    .split(",")
    .map((s) => s.trim().replace(/\/+$/, ""))
    .filter(Boolean);
}

function corsFor(origin, env) {
  if (!origin || !allowedOrigins(env).includes(origin)) return null;
  return { "Access-Control-Allow-Origin": origin, "Access-Control-Expose-Headers": "X-Relay-Limit, X-Relay-Cache", Vary: "Origin" };
}

function json(status, body, cors, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...(cors || {}), ...extra },
  });
}

export function matchRoute(path) {
  for (const route of ROUTES) {
    if (path.startsWith(route.prefix)) {
      const rest = path.slice(route.prefix.length);
      return route.patterns.some((re) => re.test(rest)) ? { route, rest } : null;
    }
  }
  return null;
}

async function yahooSession(fetchFn, refresh) {
  if (crumbCache && !refresh && Date.now() - crumbCache.at < CRUMB_TTL_MS) return crumbCache;
  const ua = { "User-Agent": SPEC.browser_agent };
  const first = await fetchFn(YAHOO_COOKIE_URL, { headers: ua, redirect: "manual" });
  const raw = typeof first.headers.getSetCookie === "function" ? first.headers.getSetCookie() : [first.headers.get("set-cookie")];
  const cookie = raw.filter(Boolean).map((c) => c.split(";")[0].trim()).join("; ");
  if (!cookie) return null;
  const res = await fetchFn(YAHOO_CRUMB_URL, { headers: { ...ua, Cookie: cookie } });
  const crumb = (await res.text()).trim();
  if (res.status !== 200 || !crumb || crumb.includes("<")) return null;
  crumbCache = { cookie, crumb, at: Date.now() };
  return crumbCache;
}

export async function handle(request, env = {}, ctx = {}, deps = {}) {
  const fetchFn = deps.fetch || fetch;
  const cache = deps.cache !== undefined ? deps.cache : globalThis.caches?.default;
  const url = new URL(request.url);
  const origin = request.headers.get("Origin");
  const cors = corsFor(origin, env);

  if (request.method === "OPTIONS") {
    if (!cors) return new Response(null, { status: 403 });
    return new Response(null, {
      status: 204,
      headers: {
        ...cors,
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "X-SEC-Identity, Accept",
        "Access-Control-Max-Age": "86400",
      },
    });
  }
  if (url.pathname === "/health") {
    return json(200, { ok: true, service: "valuelens-relay", rate_limit: env.RATE_LIMITER ? "120 requests per minute per visitor" : "none" }, cors);
  }
  if (!cors) return json(403, { error: "This relay only serves the ValueLens website." });

  if (env.RATE_LIMITER) {
    const key = request.headers.get("CF-Connecting-IP") || "unknown";
    const { success } = await env.RATE_LIMITER.limit({ key });
    if (!success) {
      return json(429, { error: "Relay limit reached: 120 requests per minute. Wait a minute." }, cors, { "X-Relay-Limit": "1" });
    }
  }

  const hit = matchRoute(url.pathname.replace(/^\/+/, ""));
  if (!hit) return json(404, { error: "This relay only forwards ValueLens data requests." }, cors);
  const { route, rest } = hit;
  if (!route.methods.includes(request.method)) return json(405, { error: "Method not allowed." }, cors);

  const params = new URLSearchParams();
  for (const [k, v] of url.searchParams) if (route.params.includes(k)) params.append(k, v);
  const headers = { "User-Agent": SPEC.relay_agent };
  if (route.identity) {
    const ident = (request.headers.get("X-SEC-Identity") || "").trim().replace(/\s+/g, " ");
    if (!IDENTITY_RE.test(ident)) return json(400, { error: "Missing or malformed SEC identity (name and e-mail)." }, cors);
    headers["User-Agent"] = ident;
  }
  if (route.browser_agent) headers["User-Agent"] = SPEC.browser_agent;
  if (route.accept) headers.Accept = route.accept;

  const base = route.upstream + rest;
  const target = () => (params.toString() ? `${base}?${params}` : base);

  // Shared, identity-free edge cache for public filings: fewer requests reach the SEC.
  // (The Cache API only takes effect when the Worker runs on a custom domain, not workers.dev.)
  const cacheable = request.method === "GET" && route.edge_cache_s && cache;
  const cacheKey = cacheable ? new Request(target(), { method: "GET" }) : null;
  if (cacheable) {
    const cached = await cache.match(cacheKey);
    if (cached) return passthrough(cached, request.method, cors, "HIT");
  }

  let upstream;
  try {
    if (route.yahoo_crumb) {
      upstream = await yahooFetch(fetchFn, base, params, headers);
    } else {
      upstream = await fetchFn(target(), { method: request.method, headers, redirect: "follow" });
      for (let i = 0; i < (route.retry_denied || 0) && upstream.status === 403; i++) {
        await upstream.body?.cancel();
        upstream = await fetchFn(target(), { method: request.method, headers, redirect: "follow" });
      }
    }
  } catch (err) {
    return json(502, { error: `Upstream unreachable (${err && err.name ? err.name : "error"}).` }, cors);
  }
  if (!upstream) return json(502, { error: "Yahoo session unavailable." }, cors);

  if (cacheable && upstream.status === 200) {
    const body = await upstream.arrayBuffer();
    const stored = new Response(body, {
      status: 200,
      headers: { "Content-Type": upstream.headers.get("Content-Type") || "application/json", "Cache-Control": `public, max-age=${route.edge_cache_s}` },
    });
    const put = cache.put(cacheKey, stored.clone());
    if (ctx.waitUntil) ctx.waitUntil(put); else await put;
    return passthrough(stored, request.method, cors, "MISS");
  }
  return passthrough(upstream, request.method, cors, null);
}

async function yahooFetch(fetchFn, base, params, headers) {
  let res = null;
  for (const refresh of [false, true]) {
    const session = await yahooSession(fetchFn, refresh);
    if (!session) return null;
    const p = new URLSearchParams(params);
    p.set("crumb", session.crumb);
    res = await fetchFn(`${base}?${p}`, { headers: { ...headers, Cookie: session.cookie } });
    if (res.status !== 401 && res.status !== 403) return res;
  }
  return res;
}

function passthrough(res, method, cors, cacheState) {
  const headers = {
    "Content-Type": res.headers.get("Content-Type") || "application/octet-stream",
    "Cache-Control": "no-store",
    ...cors,
  };
  if (cacheState) headers["X-Relay-Cache"] = cacheState;
  return new Response(method === "HEAD" ? null : res.body, { status: res.status, headers });
}

export default {
  fetch(request, env, ctx) {
    return handle(request, env, ctx);
  },
};
