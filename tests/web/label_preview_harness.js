// Runs web/static/js/labels.js against a small stand-in for the page and prints what the notes looked like
// after each answer of the server, as JSON. Used by test_label_preview_script.py (needs node).
"use strict";
const fs = require("fs");
const vm = require("vm");

function element(attrs) {
  const store = Object.assign({}, attrs || {});
  return {
    hidden: false, src: "", textContent: "",
    getAttribute: (name) => (name in store ? store[name] : null),
    removeAttribute(name) { if (name === "hidden") this.hidden = false; delete store[name]; },
    querySelectorAll: () => [],
    querySelector: () => null,
  };
}

function page(mode) {
  const image = element(mode === "article" ? { "data-label-url": "/articles/1/label.png" } : {});
  image.hidden = true;
  const failed = element();
  failed.hidden = true;
  const rows = element({ "data-note": "rows" });
  rows.hidden = true;
  const notes = element();
  notes.querySelectorAll = (selector) => (selector === "[data-note]" ? [rows] : []);
  notes.querySelector = (selector) => (selector === "[data-failed]" ? failed : null);
  const listeners = {};
  const document = {
    getElementById: (id) => (id === "label-preview" ? image : id === "label-notes" ? notes : null),
    querySelectorAll: () => [],
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  return { image, failed, rows, document, listeners };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

const answer = (notes) => ({
  ok: true,
  headers: { get: (name) => (name === "X-Label-Notes" ? notes : name === "X-Label-Dropped" ? "" : null) },
  blob: () => Promise.resolve({}),
});

async function run(mode, plan) {
  const p = page(mode);
  const pending = [];
  const context = {
    document: p.document,
    window: { setTimeout: (fn) => { fn(); return 1; }, clearTimeout: () => {} },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
    Array, Error, encodeURIComponent,
    fetch: () => { const d = deferred(); pending.push(d); return d.promise; },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
  const states = [];
  const settle = () => new Promise((r) => setImmediate(r));
  const snapshot = () => states.push({ failed: !p.failed.hidden, rows: !p.rows.hidden, src: p.image.src });
  for (const step of plan) {
    if (step.tick) { p.listeners.change({ target: { name: "label_field" } }); }
    else {
      const d = pending[step.request];
      if (step.status) d.resolve({ ok: false, status: step.status, headers: { get: () => null }, blob: () => Promise.resolve({}) });
      else if (step.ok !== undefined) d.resolve(answer(step.ok));
      else d.reject(new Error("net"));
      await settle();
      snapshot();
    }
  }
  return states;
}

(async () => {
  const out = {};
  // article's page: the first answer is fine, a later one fails (the page cannot ask again by itself: the
  // settings tab asks again on a tick, so the same sequence is driven there)
  out.settings_fail_then_recover = await run("settings", [
    { request: 0, ok: "rows" }, { tick: 1 }, { request: 1 }, { tick: 1 }, { request: 2, ok: "" },
  ]);
  out.settings_old_failure_is_ignored = await run("settings", [
    { request: 0, ok: "" }, { tick: 1 }, { tick: 1 }, { request: 2, ok: "rows" }, { request: 1 },
  ]);
  // an error status of the server is a failure too, not only a network error
  out.settings_error_status = await run("settings", [{ request: 0, ok: "rows" }, { tick: 1 }, { request: 1, status: 500 }]);
  // a late good answer of an old request must not hide the failure of the newer one
  out.settings_old_success_after_new_failure = await run("settings", [
    { request: 0, ok: "" }, { tick: 1 }, { tick: 1 }, { request: 2 }, { request: 1, ok: "rows" },
  ]);
  out.article_first_load_fails = await run("article", [{ request: 0 }]);
  console.log(JSON.stringify(out));
})();
