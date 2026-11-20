// Runs web/static/js/filament_usage.js against a small stand-in for the filament page (document, the
// browser's storage, a MutationObserver) and prints what it showed, as JSON. Used by
// test_filament_usage_script.py (needs node).
//
// The page has the switch (hidden, Ø 30 days selected) and, per view, one span in the head and one in a
// row; only the spans of Ø 30 days are shown, as the server sends them.
"use strict";
const fs = require("fs");
const vm = require("vm");

const SOURCE = fs.readFileSync(process.argv[2], "utf8");
const VIEWS = ["avg30", "avg7", "today", "yesterday", "week", "month", "year", "total"];

function makeStorage(options) {
  const data = new Map(options.stored ? [["rb_usage_view", options.stored]] : []);
  const fail = () => { throw new Error("storage blocked"); };
  return {
    data,
    getItem: (k) => (options.storageThrows ? fail() : data.has(k) ? data.get(k) : null),
    setItem: (k, v) => (options.storageThrows ? fail() : void data.set(k, String(v))),
  };
}

function element(attrs, hidden) {
  return {
    attrs,
    hidden,
    getAttribute: (n) => (Object.prototype.hasOwnProperty.call(attrs, n) ? attrs[n] : null),
    hasAttribute: (n) => Object.prototype.hasOwnProperty.call(attrs, n),
  };
}

// The list region as the server renders it.
function region() {
  const select = element({ "data-usage-select": "" }, false);
  select.options = VIEWS.map((v) => ({ value: v }));
  select.value = "avg30";
  const choice = element({ "data-usage-choice": "" }, true);
  choice.querySelector = (sel) => (sel === "[data-usage-select]" ? select : null);
  const spans = [];
  for (const view of VIEWS) {
    spans.push(element({ "data-usage-head": view }, view !== "avg30"));
    spans.push(element({ "data-usage": view }, view !== "avg30"));
  }
  return { select, choice, spans };
}

function run(options) {
  const storage = makeStorage(options);
  let page = options.noList ? null : region();
  const listeners = {};
  let observer = null;
  const doc = {
    body: {},
    querySelector: (sel) => (sel === "[data-usage-choice]" && page ? page.choice : null),
    querySelectorAll: (sel) => (sel === "[data-usage], [data-usage-head]" && page ? page.spans : []),
    addEventListener: (n, fn) => { (listeners[n] = listeners[n] || []).push(fn); },
  };
  const win = { localStorage: storage };
  const context = {
    window: win,
    document: doc,
    MutationObserver: function (fn) {
      observer = fn;
      this.observe = () => {};
    },
  };
  win.MutationObserver = context.MutationObserver;
  let error = null;
  try {
    vm.runInNewContext(SOURCE, context);
  } catch (e) {
    error = String(e);
  }
  const state = () =>
    page && {
      choiceShown: !page.choice.hidden,
      selected: page.select.value,
      shown: page.spans.filter((s) => !s.hidden).map((s) => s.getAttribute("data-usage") || s.getAttribute("data-usage-head")),
    };
  const out = { error, start: state() };
  if (options.choose && page) {
    page.select.value = options.choose;
    try {
      (listeners.change || []).forEach((fn) => fn({ target: page.select }));
    } catch (e) {
      out.error = String(e);
    }
    out.afterChange = state();
  }
  if (options.swap && page) {
    page = region(); // live.js put a fresh list region in
    out.beforeObserver = state();
    if (observer) observer();
    out.afterSwap = state();
  }
  out.stored = storage.data.has("rb_usage_view") ? storage.data.get("rb_usage_view") : null;
  return out;
}

const cases = {
  first_visit: run({}),
  remembered: run({ stored: "total" }),
  unknown_stored: run({ stored: "decade" }),
  chosen: run({ choose: "today" }),
  blocked_storage: run({ storageThrows: true, choose: "week" }),
  swapped: run({ stored: "year", swap: true }),
  no_list: run({ noList: true }),
};
process.stdout.write(JSON.stringify(cases));
