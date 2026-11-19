// Runs web/static/js/keepscroll.js against a small stand-in for the browser (window, document, the
// tab's storage, a clock) and prints what it did in a series of page visits, as JSON. Used by
// test_keepscroll_script.py (needs node).
//
// A "trip" is one form sent on a page and the page that comes back: `trip(send, land)` loads the script
// on the first page (`send`), scrolls, sends a form, then loads the script again on the second page
// (`land`) with the same storage, as a browser tab would keep it.
"use strict";
const fs = require("fs");
const vm = require("vm");

const SOURCE = fs.readFileSync(process.argv[2], "utf8");
const KEY = "rb_scroll";

function makeStorage(options) {
  const data = new Map();
  const fail = () => { throw new Error("storage blocked"); };
  const storage = {
    data,
    getItem: (k) => (options.storageThrows ? fail() : data.has(k) ? data.get(k) : null),
    setItem: (k, v) => (options.storageThrows ? fail() : void data.set(k, String(v))),
    removeItem: (k) => (options.storageThrows ? fail() : void data.delete(k)),
  };
  return storage;
}

// One loaded page. `o`: path, search, hash, liveSrc, navType, perfMissing, markers, maxY, now.
function visit(o, shared) {
  const opts = Object.assign(
    { path: "/articles", search: "", hash: "", liveSrc: "/articles", navType: "navigate", perfMissing: false,
      markers: false, maxY: 100000, storageThrows: false },
    o
  );
  const log = [];
  const listeners = { document: {}, window: {} };
  const app = { getAttribute: (n) => (n === "data-live-src" ? opts.liveSrc : null) };
  const marker = { scrollIntoView: (arg) => log.push("into:" + (arg && arg.block)) };
  const doc = {
    readyState: "interactive",
    getElementById: (id) => (id === "app" ? app : null),
    querySelector: (sel) => (opts.markers && /alert|field-error/.test(sel) ? marker : null),
    addEventListener: (n, fn) => { (listeners.document[n] = listeners.document[n] || []).push(fn); },
  };
  const win = {
    sessionStorage: shared.storage,
    location: { pathname: opts.path, search: opts.search, hash: opts.hash },
    scrollY: 0,
    maxY: opts.maxY,
    scrollTo: (x, y) => {
      const wanted = typeof x === "object" && x !== null ? x.top : y;
      win.scrollY = Math.min(wanted, win.maxY);
      log.push("scrollTo:" + wanted);
    },
    addEventListener: (n, fn) => { (listeners.window[n] = listeners.window[n] || []).push(fn); },
    performance: opts.perfMissing ? {} : { getEntriesByType: () => [{ type: opts.navType }] },
  };
  const context = { window: win, document: doc, Date: { now: () => shared.clock.now }, JSON, Math, Number, isFinite };
  // The script may read these as globals or through `window`; both work in a browser.
  context.sessionStorage = win.sessionStorage;
  context.location = win.location;
  context.performance = win.performance;
  vm.createContext(context);
  let error = null;
  try {
    vm.runInContext(SOURCE, context);
  } catch (e) {
    error = String(e);
  }
  const page = {
    log, win, error, marker,
    fire(target, name, event) { (listeners[target][name] || []).forEach((fn) => fn(event)); },
    submit(form, extra) {
      const event = Object.assign({ type: "submit", target: form, defaultPrevented: false, submitter: null }, extra || {});
      try { page.fire("document", "submit", event); } catch (e) { page.error = String(e); }
    },
    load() { try { page.fire("window", "load", { type: "load" }); } catch (e) { page.error = String(e); } },
    scroll(y) { win.scrollY = y; },
  };
  return page;
}

const postForm = () => ({ method: "post", target: "" });
const getForm = () => ({ method: "get", target: "" });

function trip(send, land, options) {
  const o = Object.assign({ scrollY: 640, form: postForm(), event: {}, wait: 1000, entry: undefined }, options || {});
  const shared = { storage: makeStorage({ storageThrows: false }), clock: { now: 50000 } };
  const first = visit(send, shared);
  first.scroll(o.scrollY);
  first.submit(o.form, o.event);
  const stored = shared.storage.data.has(KEY) ? JSON.parse(shared.storage.data.get(KEY)) : null;
  if (o.entry !== undefined) shared.storage.data.set(KEY, o.entry);
  shared.clock.now += o.wait;
  const second = visit(land, shared);
  const result = {
    stored,
    stored_at_is_number: stored ? typeof stored.at === "number" : null,
    calls: second.log.slice(),
    y: second.win.scrollY,
    left_in_storage: shared.storage.data.has(KEY),
    errors: [first.error, second.error].filter(Boolean),
  };
  result.second = second;
  return result;
}

function plain(r) {
  const copy = Object.assign({}, r);
  delete copy.second;
  return copy;
}

const out = {};

// --- remembering -----------------------------------------------------------------------------------
let r = trip({ path: "/articles", search: "?q=x", liveSrc: "/articles?q=x" }, { path: "/articles", search: "?q=x", liveSrc: "/articles?q=x" });
out.basic = plain(r);

r = trip({ path: "/settings", search: "?tab=general" }, { path: "/settings", search: "?tab=general", liveSrc: "/settings?tab=general" });
out.tab_stored = plain(r);

out.get_form = plain(trip({}, {}, { form: getForm() }));
out.prevented = plain(trip({}, {}, { event: { defaultPrevented: true } }));
out.blank_target = plain(trip({}, {}, { form: { method: "post", target: "_blank" } }));
out.submitter_get = plain(trip({}, {}, { event: { submitter: { formMethod: "get", formTarget: "" } } }));
out.submitter_post_on_get_form = plain(trip({}, {}, { form: getForm(), event: { submitter: { formMethod: "post", formTarget: "" } } }));
out.submitter_blank = plain(trip({}, {}, { event: { submitter: { formMethod: "", formTarget: "_blank" } } }));
out.submitter_plain = plain(trip({}, {}, { event: { submitter: { formMethod: "", formTarget: "" } } }));

// --- restoring -------------------------------------------------------------------------------------
out.other_path = plain(trip({ path: "/articles" }, { path: "/articles/7", liveSrc: "/articles/7" }));
out.other_tab = plain(trip({ path: "/settings", search: "?tab=general" }, { path: "/settings", search: "?tab=bambuddy", liveSrc: "/settings?tab=bambuddy" }));
out.tab_appears = plain(trip({ path: "/settings", search: "" }, { path: "/settings", search: "?tab=general", liveSrc: "/settings?tab=general" }));
out.other_query_ok = plain(trip({ path: "/articles", search: "?q=x" }, { path: "/articles", search: "?q=y&layout=tiles", liveSrc: "/articles" }));
out.extra_query_next_to_tab_ok = plain(trip({ path: "/settings", search: "?tab=maintenance" }, { path: "/settings", search: "?tab=maintenance&ops=checked", liveSrc: "/settings" }));
out.anchor_wins = plain(trip({}, { hash: "#ops-update" }));
out.old = plain(trip({}, {}, { wait: 16000 }));
out.not_so_old = plain(trip({}, {}, { wait: 14000 }));
out.post_answer = plain(trip({}, { liveSrc: "" }));
out.reload = plain(trip({}, { navType: "reload" }));
out.back = plain(trip({}, { navType: "back_forward" }));
out.no_navigation_api = plain(trip({}, { perfMissing: true }));
out.zero = plain(trip({}, {}, { scrollY: 0 }));
out.fraction = plain(trip({}, {}, { scrollY: 640.6 }));

// nothing stored, nothing to restore
{
  const shared = { storage: makeStorage({}), clock: { now: 1 } };
  const page = visit({}, shared);
  out.no_entry = { calls: page.log.slice(), error: page.error };
}

// a damaged entry is dropped without a word
const damaged = {};
[
  ["not_json", "{oops"],
  ["y_text", JSON.stringify({ path: "/articles", tab: "", y: "high", at: 50000 })],
  ["y_negative", JSON.stringify({ path: "/articles", tab: "", y: -5, at: 50000 })],
  ["y_infinite", '{"path":"/articles","tab":"","y":1e999,"at":50000}'],
  ["no_path", JSON.stringify({ tab: "", y: 10, at: 50000 })],
  ["at_text", JSON.stringify({ path: "/articles", tab: "", y: 10, at: "now" })],
  ["array", "[1,2]"],
  ["null", "null"],
].forEach(([name, entry]) => {
  damaged[name] = plain(trip({}, {}, { entry }));
});
out.damaged = damaged;

// storage that refuses: sending and loading both go on quietly
{
  const shared = { storage: makeStorage({ storageThrows: true }), clock: { now: 1 } };
  const a = visit({}, shared);
  a.scroll(300);
  a.submit(postForm());
  const b = visit({}, shared);
  out.storage_blocked = { errors: [a.error, b.error].filter(Boolean), calls: b.log.slice() };
}

// --- a page that is still too short: once more after the load ------------------------------------
{
  const shared = { storage: makeStorage({}), clock: { now: 50000 } };
  const first = visit({}, shared);
  first.scroll(900);
  first.submit(postForm());
  shared.clock.now += 500;
  const second = visit({ maxY: 300 }, shared);
  const before = second.win.scrollY;
  second.win.maxY = 5000; // the images have arrived
  second.load();
  out.short_page = { before, after: second.win.scrollY, calls: second.log.slice() };
}

// a page that was long enough at once does not scroll again
{
  const shared = { storage: makeStorage({}), clock: { now: 50000 } };
  const first = visit({}, shared);
  first.scroll(400);
  first.submit(postForm());
  const second = visit({}, shared);
  second.load();
  out.long_page_once = { calls: second.log.slice() };
}

// a load without a trip does nothing
{
  const shared = { storage: makeStorage({}), clock: { now: 1 } };
  const page = visit({}, shared);
  page.load();
  out.load_only = { calls: page.log.slice() };
}

// --- trouble on the page: the view goes to it, once restored ---------------------------------------
r = trip({}, { markers: true });
out.marker_after_restore = plain(r);
{
  const shared = { storage: makeStorage({}), clock: { now: 1 } };
  const page = visit({ markers: true }, shared);
  out.marker_without_trip = { calls: page.log.slice() };
}
out.marker_but_anchor = plain(trip({}, { markers: true, hash: "#x" }));
out.marker_but_post_answer = plain(trip({}, { markers: true, liveSrc: "" }));

console.log(JSON.stringify(out));
