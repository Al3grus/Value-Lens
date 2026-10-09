// ValueLens engine: runs the Python package in this web worker with Pyodide.
// The page sends {id, cmd, arg}; the worker answers {id, ok, result | error} and, during an
// analysis, {id, progress: step}. Boot progress is reported as {boot: step, state}.
// Downloaded data (SEC filings, rates) is cached in IndexedDB so repeats cost no requests.

let pyodide = null;
let browser = null;
let ready = null; // core + ValueLens: enough for the credential and ticker checks
let heavy = null; // numpy + pandas, loaded in the background for the analysis
const CACHE_MOUNT = "/cache";
const APP_DIR = "/home/pyodide/app";

function syncfs(populate) {
  return new Promise((resolve) => {
    try { pyodide.FS.syncfs(populate, () => resolve()); } catch (_) { resolve(); }
  });
}

async function boot(config) {
  const step = (name, state) => self.postMessage({ boot: name, state });
  step("python", "run");
  const { loadPyodide } = await import(config.pyodide + "pyodide.mjs");
  pyodide = await loadPyodide({ indexURL: config.pyodide });
  step("python", "done");

  step("valuelens", "run");
  const res = await fetch(config.bundle, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Could not load the ValueLens code (${res.status}).`);
  pyodide.unpackArchive(await res.arrayBuffer(), "zip", { extractDir: APP_DIR });
  pyodide.runPython(`import sys\nif ${JSON.stringify(APP_DIR)} not in sys.path: sys.path.insert(0, ${JSON.stringify(APP_DIR)})`);
  try {
    pyodide.FS.mkdirTree(CACHE_MOUNT);
    pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, CACHE_MOUNT);
    await syncfs(true);
  } catch (_) { /* no IndexedDB (private window): in-memory cache only */ }
  browser = pyodide.pyimport("valuelens.web.browser");
  const started = JSON.parse(browser.start(config.relay, CACHE_MOUNT + "/valuelens"));
  step("valuelens", "done");
  step("numpy+pandas", "run");
  // loadPackage only warns when a download fails, so confirm with an import.
  heavy = pyodide.loadPackage(["numpy", "pandas"], { messageCallback: () => {} })
    .then(() => pyodide.runPythonAsync("import numpy, pandas"))
    .then(
      () => step("numpy+pandas", "done"),
      (err) => { step("numpy+pandas", "failed"); throw err; },
    );
  heavy.catch(() => {});
  return started.result;
}

self.onmessage = async (event) => {
  const msg = event.data || {};
  if (msg.cmd === "boot") {
    if (!ready) ready = boot(msg.config);
    try {
      const result = await ready;
      self.postMessage({ id: msg.id, ok: true, result });
    } catch (err) {
      ready = null;
      self.postMessage({ id: msg.id, ok: false, error: `ValueLens failed to start: ${err.message || err}.` });
    }
    return;
  }
  try {
    await ready;
  } catch (_) {
    self.postMessage({ id: msg.id, ok: false, error: "ValueLens is not running. Reload the page." });
    return;
  }
  if (msg.cmd === "analyze") {
    try {
      await heavy;
    } catch (_) {
      self.postMessage({ id: msg.id, ok: false, error: "The analysis libraries (numpy, pandas) could not be downloaded. Check your connection and reload the page." });
      return;
    }
  }
  const progress = (stepName) => self.postMessage({ id: msg.id, progress: String(stepName) });
  let out;
  try {
    out = JSON.parse(browser.call(msg.cmd, msg.arg ?? "", progress));
  } catch (err) {
    out = { ok: false, error: String(err.message || err) };
  }
  if (msg.cmd === "analyze" || msg.cmd === "ticker" || msg.cmd === "identity") await syncfs(false);
  self.postMessage({ id: msg.id, ...out });
};
