// ValueLens page logic. Three stages: your details (name + e-mail for the SEC) -> ticker -> report.
// All analysis runs in the engine worker (engine.mjs); this file only drives the screen. The name
// and e-mail live in the worker's memory for this tab and are never written to storage.
(() => {
  "use strict";
  const CONFIG = window.VALUELENS_CONFIG || {};
  const TICKER_RE = /^[A-Z0-9][A-Z0-9.\-=^]{0,15}$/;
  const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;
  const SPIN = ["|", "/", "-", "\\"];
  const STEPS = { market: "Market data (Yahoo Finance)", filings: "SEC filings (10-K, 10-Q)", rates: "Interest rates (FRED)", analysis: "Scoring and valuation" };
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (id) => document.getElementById(id);
  const txt = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const S = { verified: false, checked: null, view: "simple", result: null, busy: false, engine: false };
  try { if (localStorage.getItem("valuelens.view") === "detailed") S.view = "detailed"; } catch (_) {}

  // ---- engine worker ---------------------------------------------------------------------
  const worker = new Worker(CONFIG.engine || "engine.mjs", { type: "module" });
  const pending = new Map();
  let seq = 0;
  worker.onmessage = (e) => {
    const m = e.data || {};
    if (m.boot) return bootStep(m.boot, m.state);
    const p = pending.get(m.id);
    if (!p) return;
    if (m.progress) return p.onProgress && p.onProgress(m.progress);
    pending.delete(m.id);
    if (m.session_expired) { human.until = 0; p.reject(new Error(SESSION_EXPIRED)); return; }
    if (m.ok) p.resolve(m.result);
    else p.reject(new Error(m.error || "Something went wrong. Reload the page."));
  };
  worker.onerror = (e) => engineFailed(e.message || "ValueLens could not start in this browser.");
  function rpc(cmd, arg, onProgress, extra) {
    const id = ++seq;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject, onProgress });
      worker.postMessage({ id, cmd, arg, ...extra });
    });
  }
  const booting = rpc("boot", "", null, { config: CONFIG });
  booting.then(() => { S.engine = true; }).catch((err) => engineFailed(err.message));

  function bootStep() { /* loading runs silently in the background */ }
  booting.then(() => relaySession()).catch(() => { /* reported when the visitor acts */ });
  function engineFailed(message) {
    $("cred-err").textContent = message + " Try reloading, or a current Chrome, Edge, Firefox or Safari.";
  }

  // ---- relay session (Cloudflare Turnstile) ------------------------------------------------------
  // The relay only serves browsers that pass Cloudflare Turnstile. The page swaps the Turnstile
  // token for a one-hour relay session, which the engine sends with every request. Most visitors
  // never see the check; when Cloudflare wants a click, the widget appears at the top. Without a
  // site key (the local server) there is no check.
  const TURNSTILE_JS = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  const SESSION_EXPIRED = "The security check has expired. Press the button again.";
  const human = { script: null, widget: null, wait: null, until: 0, pending: null };

  function relaySession() {
    if (!CONFIG.turnstile || human.until - Date.now() > 10 * 60e3) return Promise.resolve();
    if (!human.pending) human.pending = newSession().finally(() => { human.pending = null; });
    return human.pending;
  }
  async function newSession() {
    const token = await humanToken();
    let res;
    try {
      res = await fetch(`${CONFIG.relay}/session`, { method: "POST", body: token, cache: "no-store" });
    } catch (_) {
      throw new Error("The ValueLens relay could not be reached. Check your connection and try again.");
    }
    const body = await res.json().catch(() => ({}));
    if (res.status === 404) { human.until = Date.now() + 15 * 60e3; return; } // relay without sessions
    if (!res.ok || !body.session) throw new Error(body.error || `The security check failed (${res.status}). Reload the page.`);
    await rpc("session", body.session);
    human.until = Date.now() + body.expires_in * 1000;
  }
  function loadTurnstile() {
    if (!human.script) {
      human.script = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = TURNSTILE_JS; s.async = true;
        s.onload = () => (window.turnstile ? resolve(window.turnstile) : s.onerror());
        s.onerror = () => {
          human.script = null; s.remove();
          reject(new Error("The security check (Cloudflare Turnstile) could not load. If a content blocker is on, allow challenges.cloudflare.com and try again."));
        };
        document.head.append(s);
      });
    }
    return human.script;
  }
  async function humanToken() {
    const ts = await loadTurnstile();
    return new Promise((resolve, reject) => {
      human.wait = { resolve, reject };
      if (human.widget != null) return ts.reset(human.widget);
      const fail = (msg) => () => { settle(null, new Error(msg)); return true; };
      human.widget = ts.render("#human-box", {
        sitekey: CONFIG.turnstile,
        action: "turnstile-spin-v2", // Cloudflare's marker for Turnstile set up with its Spin guide
        appearance: "interaction-only",
        theme: "dark",
        "refresh-expired": "manual",
        callback: (token) => settle(token),
        "error-callback": fail("The security check failed. Reload the page and try again."),
        "timeout-callback": fail("The security check timed out. Press the button again."),
        "unsupported-callback": fail("This browser cannot run the security check. Try a current Chrome, Edge, Firefox or Safari."),
        "before-interactive-callback": () => { $("human").hidden = false; },
        "after-interactive-callback": () => { $("human").hidden = true; },
      }) ?? null;
    });
  }
  function settle(token, err) {
    const w = human.wait;
    human.wait = null;
    if (w) token ? w.resolve(token) : w.reject(err);
  }

  // ---- stages ------------------------------------------------------------------------------
  function stage(n) {
    for (const i of [1, 2, 3]) {
      $("stage" + i).hidden = i !== n;
      $("s" + i).className = i === n ? "now" : i < n ? "done" : "";
    }
    $("closed").hidden = true;
    const focus = { 1: "name", 2: "tk" }[n];
    if (focus) setTimeout(() => $(focus).focus(), 0);
  }

  // ---- checks list -----------------------------------------------------------------------
  function checkRows(list, heading, steps) {
    list.append(txt("li", heading, "head"));
    for (const s of steps) {
      const li = document.createElement("li");
      const cls = s.ok === true ? "ok" : s.ok === false ? "bad" : "na";
      const mark = s.ok === true ? "[√]" : s.ok === false ? "[×]" : "[!]";
      li.append(txt("span", mark, "st " + cls), txt("span", s.label, "what"), txt("span", s.detail, "detail"));
      list.append(li);
    }
  }
  function runningRow(list, heading, label) {
    list.append(txt("li", heading, "head"));
    const li = document.createElement("li");
    li.append(txt("span", reduced ? "[*]" : "[|]", "st run"), txt("span", label, "what"), txt("span", "", "detail"));
    list.append(li);
    if (reduced) return () => {};
    let f = 0;
    const t = setInterval(() => { f = (f + 1) % SPIN.length; li.firstChild.textContent = `[${SPIN[f]}]`; }, 120);
    return () => clearInterval(t);
  }

  // ---- stage 1: your details (name + e-mail for the SEC) ---------------------------------------
  function identity() { return `${$("name").value.trim().replace(/\s+/g, " ")} ${$("email").value.trim()}`; }
  function unverify() {
    if (!S.verified && $("cred-go").textContent === "VERIFY") return;
    S.verified = false; $("cred-go").textContent = "VERIFY"; $("cred-result").hidden = true;
  }
  for (const id of ["name", "email"]) $(id).addEventListener("input", () => { unverify(); $("cred-err").textContent = ""; });

  $("cred-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (S.verified) { showCreds(); stage(2); return; }
    const name = $("name").value.trim(), email = $("email").value.trim();
    if (name.length < 2) { $("cred-err").textContent = "Enter your full name."; $("name").focus(); return; }
    if (!EMAIL_RE.test(email)) { $("cred-err").textContent = "Enter a valid e-mail address, e.g. name@domain.com."; $("email").focus(); return; }
    $("cred-go").disabled = true; $("cred-err").textContent = "";
    const list = $("cred-checks"); list.replaceChildren(); $("cred-result").hidden = false;
    const stop = runningRow(list, "SEC EDGAR IDENTITY", "checking");
    try {
      await booting;
      await relaySession();
      const sec = await rpc("identity", identity());
      stop(); list.replaceChildren(); checkRows(list, "SEC EDGAR IDENTITY", sec.steps);
      if (!sec.ok) { $("cred-err").textContent = sec.message; return; }
      S.verified = true;
      $("cred-go").textContent = "CONTINUE";
      $("cred-go").focus();
    } catch (err) {
      stop(); $("cred-err").textContent = err.message;
    } finally { $("cred-go").disabled = false; }
  });
  function showCreds() {
    $("st-creds").hidden = false;
    $("cred-cancel").hidden = false;
  }
  const change = txt("button", "change", "linkbtn"); change.type = "button";
  $("st-creds").append("  ", change);
  change.addEventListener("click", () => { if (!S.busy) stage(1); });
  $("cred-cancel").addEventListener("click", () => { if (S.verified) stage(2); });

  // ---- stage 2: ticker -------------------------------------------------------------------------
  function resetCheck() { S.checked = null; $("found").hidden = true; $("run").hidden = true; }
  $("tk").addEventListener("input", () => {
    const v = $("tk").value.toUpperCase();
    if ($("tk").value !== v) $("tk").value = v;
    resetCheck();
    $("tk-err").textContent = /[\s,;]/.test(v) ? "Enter one ticker only."
      : v && !TICKER_RE.test(v) ? "Use letters, digits, '.' and '-', e.g. MSFT, BRK-B." : "";
  });
  $("tk-form").addEventListener("submit", (e) => { e.preventDefault(); check(); });

  async function check() {
    const t = $("tk").value.trim().toUpperCase();
    if (!TICKER_RE.test(t)) { $("tk-err").textContent = t ? "Enter one valid ticker, e.g. MSFT." : "Type a ticker, e.g. MSFT."; return; }
    $("tk-check").disabled = true; $("tk-err").textContent = ""; resetCheck();
    const list = $("tk-checks"); list.replaceChildren(); $("found").hidden = false; $("tk-co").hidden = true;
    const stop = runningRow(list, t, "checking");
    try {
      await relaySession();
      const v = await rpc("ticker", t);
      stop(); list.replaceChildren();
      if ($("tk").value.trim().toUpperCase() !== t) return;
      checkRows(list, t, v.steps);
      const d = v.data || {};
      if (d.name) {
        $("f-name").textContent = d.name.toUpperCase();
        const meta = $("f-meta"); meta.replaceChildren();
        const price = d.price != null ? `${d.currency || ""} ${Number(d.price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`.trim() : null;
        for (const f of [d.ticker, d.exchange, price]) if (f) meta.append(txt("span", f));
        $("tk-co").hidden = false;
      }
      if (!v.ok) { $("tk-err").textContent = v.message; return; }
      S.checked = d;
      $("run").hidden = false; $("run").focus();
    } catch (err) { stop(); $("tk-err").textContent = err.message; }
    finally { $("tk-check").disabled = false; }
  }

  function recent(add) {
    let list = [];
    try { list = JSON.parse(localStorage.getItem("valuelens.recent") || "[]"); } catch (_) {}
    if (add) {
      list = [add, ...list.filter((x) => x !== add)].slice(0, 6);
      try { localStorage.setItem("valuelens.recent", JSON.stringify(list)); } catch (_) {}
    }
    const box = $("recent");
    box.replaceChildren(txt("span", "RECENT"));
    for (const t of list) {
      if (!TICKER_RE.test(t)) continue;
      const b = txt("button", t, "btn small"); b.type = "button";
      b.addEventListener("click", () => { $("tk").value = t; resetCheck(); check(); });
      box.append(b);
    }
    box.hidden = list.length === 0;
  }

  // ---- stage 3: analysis -------------------------------------------------------------------------
  $("run").addEventListener("click", async () => {
    if (!S.checked || S.busy) return;
    const t = S.checked.ticker;
    S.busy = true; $("run").disabled = true;
    stage(3);
    $("cmd-ticker").textContent = t;
    $("failure").hidden = true; $("result").hidden = true; $("end").hidden = true;
    const started = performance.now();
    const log = { current: null, at: started, done: [] };
    let frame = 0;
    const paintLog = () => {
      $("log").replaceChildren(...Object.entries(STEPS).map(([key, label]) => {
        const li = document.createElement("li");
        const done = log.done.find((d) => d.key === key);
        const running = log.current === key;
        li.className = done ? "done" : running ? "running" : "";
        const mark = done ? "[√]" : running ? `[${reduced ? "*" : SPIN[frame]}]` : "[ ]";
        const secs = done ? `${done.secs.toFixed(1)}s` : running ? `${((performance.now() - log.at) / 1000).toFixed(1)}s` : "";
        li.append(txt("span", mark, "st"), txt("span", label), txt("span", secs, "secs"));
        return li;
      }));
    };
    const onProgress = (key) => {
      const now = performance.now();
      if (log.current) log.done.push({ key: log.current, secs: (now - log.at) / 1000 });
      log.current = key; log.at = now; paintLog();
    };
    paintLog();
    const tick = setInterval(() => { frame = (frame + 1) % SPIN.length; paintLog(); }, 120);
    try {
      await relaySession();
      const r = await rpc("analyze", t, onProgress);
      onProgress(null);
      $("log").append(txt("li", `done in ${((performance.now() - started) / 1000).toFixed(1)}s`, "summary"));
      S.result = r;
      recent(t);
      paint();
      $("result").hidden = false;
    } catch (err) {
      $("failure").textContent = err.message; $("failure").hidden = false;
    } finally {
      clearInterval(tick);
      S.busy = false; $("run").disabled = false;
      $("end").hidden = false;
      $("again").focus({ preventScroll: true });
    }
  });

  function paint() {
    if (!S.result) return;
    $("tab-simple").setAttribute("aria-selected", String(S.view === "simple"));
    $("tab-detailed").setAttribute("aria-selected", String(S.view === "detailed"));
    $("readout").innerHTML = S.result[S.view]; // web/render.py: every text node is html-escaped there
  }
  for (const [id, view] of [["tab-simple", "simple"], ["tab-detailed", "detailed"]]) {
    $(id).addEventListener("click", () => {
      S.view = view;
      try { localStorage.setItem("valuelens.view", view); } catch (_) {}
      paint();
    });
  }

  $("again").addEventListener("click", () => {
    $("tk").value = ""; resetCheck(); $("tk-err").textContent = "";
    stage(2); window.scrollTo(0, 0);
  });
  $("exit").addEventListener("click", async () => {
    $("exit").disabled = true;
    try { await rpc("forget"); } catch (_) { /* nothing to clear */ }
    for (const id of ["name", "email"]) $(id).value = "";
    S.verified = false; S.result = null; $("readout").replaceChildren();
    for (const i of [1, 2, 3]) $("stage" + i).hidden = true;
    document.querySelector(".stages").hidden = true; $("st-creds").hidden = true;
    $("closed").hidden = false; window.scrollTo(0, 0);
  });
  $("restart").addEventListener("click", () => {
    latestVersion().then((v) => {
      if (freshPage(v)) return;
      document.querySelector(".stages").hidden = false;
      $("cred-checks").replaceChildren(); $("cred-result").hidden = true; $("cred-err").textContent = "";
      $("cred-go").textContent = "VERIFY"; $("cred-cancel").hidden = true; $("exit").disabled = false;
      $("tk").value = ""; resetCheck(); $("tk-err").textContent = "";
      $("log").replaceChildren(); $("result").hidden = true; $("failure").hidden = true; $("end").hidden = true;
      stage(1); window.scrollTo(0, 0);
    });
  });

  // ---- stay current: a tab restored from the browser's cache can hold an older version ---------
  // version.json is fetched uncached; if the site has changed, load the new page (once per version).
  function latestVersion() {
    if (!CONFIG.version) return Promise.resolve(null); // local server: nothing is cached
    return fetch("version.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => (j && j.version) || null, () => null);
  }
  function freshPage(v) {
    if (!v || v === CONFIG.version) return false;
    try {
      if (sessionStorage.getItem("valuelens.loaded") === v) return false; // tried already: no loop
      sessionStorage.setItem("valuelens.loaded", v);
    } catch (_) { return false; }
    location.replace(`${location.pathname}?v=${encodeURIComponent(v)}`);
    return true;
  }
  latestVersion().then(freshPage);

  // ---- tooltips: Escape closes ----------------------------------------------------------------------
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".tipwrap").forEach((t) => t.classList.add("closed"));
  });
  document.querySelectorAll(".tipwrap").forEach((t) => {
    t.addEventListener("mouseenter", () => t.classList.remove("closed"));
    t.addEventListener("focusin", () => t.classList.remove("closed"));
  });

  recent();
  stage(1);
})();
