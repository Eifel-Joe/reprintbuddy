// Runs web/static/js/keepscroll.js against a small stand-in for the browser (window, document, the
// tab's storage, a clock) and prints what it did in a series of page visits, as JSON. Used by
// test_keepscroll_script.py (needs node).
//
// A "trip" is one form sent on a page and the page that comes back: `trip(send, land)` loads the script
// on the first page (`send`), scrolls, sends a form, then loads the script again on the second page
// (`land`) with the same storage, as a browser tab would keep it.
//
// The forms are made like the browser's: the properties `method` and `target` of a form are the
// controls of that name when it has one (the target quantity field of the article list is called
// "target"), so only the attributes tell the truth. The stand-in gives those properties an object each.
"use strict";
const fs = require("fs");
const vm = require("vm");

const SOURCE = fs.readFileSync(process.argv[2], "utf8");
const KEY = "rb_scroll";

function makeStorage(options) {
  const data = new Map();
  const fail = () => { throw new Error("storage blocked"); };
  return {
    data,
    getItem: (k) => (options.storageThrows ? fail() : data.has(k) ? data.get(k) : null),
    setItem: (k, v) => (options.storageThrows ? fail() : void data.set(k, String(v))),
    removeItem: (k) => (options.storageThrows ? fail() : void data.delete(k)),
  };
}

// An element that knows only its attributes.
function withAttributes(attrs) {
  return {
    getAttribute: (n) => (Object.prototype.hasOwnProperty.call(attrs, n) ? attrs[n] : null),
    hasAttribute: (n) => Object.prototype.hasOwnProperty.call(attrs, n),
  };
}

// `markers`: true (one visible message in the box) or a list of {kind, visible, id}; kind is "flash"
// (the message box), "field" (a field error) or "row" (an error written into the page, found only
// on a page that answers the POST itself).
const KINDS = { flash: "[data-flashes] .alert", field: ".field-error", row: "main .alert" };

// One loaded page. `o`: path, search, hash, liveSrc, navType, redirects, perfMissing, markers, maxY,
// headTop (document coordinate of the page head, or null for a page without one).
function visit(o, shared) {
  const opts = Object.assign(
    { path: "/articles", search: "", hash: "", liveSrc: "/articles", navType: "navigate", redirects: 1,
      perfMissing: false, markers: false, maxY: 100000, headTop: null },
    o
  );
  const markers = opts.markers === true ? [{ kind: "flash", visible: true, id: "m" }] : opts.markers || [];
  const log = [];
  const listeners = { document: {}, window: {} };
  const app = withAttributes({ "data-live-src": opts.liveSrc });
  const elementOf = (m) => ({
    scrollIntoView: (arg) => log.push("into:" + (arg && arg.block) + ":" + m.id),
    getClientRects: () => (m.visible === false ? [] : [{}]),
  });
  const doc = {
    readyState: "interactive",
    getElementById: (id) => (id === "app" ? app : null),
    querySelector: (sel) => {
      if (/page-head/.test(sel) && opts.headTop !== null) {
        return { getBoundingClientRect: () => ({ top: opts.headTop - win.scrollY }) };
      }
      return null;
    },
    querySelectorAll: (sel) =>
      markers.filter((m) => sel.indexOf(KINDS[m.kind]) >= 0).map(elementOf),
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
    performance: opts.perfMissing
      ? {}
      : {
          getEntriesByType: () => [
            opts.redirects === undefined ? { type: opts.navType } : { type: opts.navType, redirectCount: opts.redirects },
          ],
        },
  };
  const context = { window: win, document: doc, Date: { now: () => shared.clock.now }, JSON, Math, Number, isFinite };
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
    log, win, get error() { return error; },
    fire(target, name, event) { (listeners[target][name] || []).forEach((fn) => fn(event)); },
    submit(form, extra) {
      const event = Object.assign({ type: "submit", target: form, defaultPrevented: false, submitter: null }, extra || {});
      try { page.fire("document", "submit", event); } catch (e) { error = String(e); }
    },
    load() { try { page.fire("window", "load", { type: "load" }); } catch (e) { error = String(e); } },
    scroll(y) { win.scrollY = y; },
  };
  return page;
}

// A form as the browser has it: the properties are the controls of the same name.
function form(attrs) {
  return Object.assign(withAttributes(attrs), { method: {}, target: {}, action: {} });
}
const postForm = (extra) => form(Object.assign({ method: "post" }, extra || {}));
const getForm = () => form({ method: "get" });
const submitter = (attrs) => Object.assign(withAttributes(attrs), { formMethod: {}, formTarget: {} });

function trip(send, land, options) {
  const o = Object.assign({ scrollY: 640, form: postForm(), event: {}, wait: 1000, entry: undefined }, options || {});
  const shared = { storage: makeStorage({}), clock: { now: 50000 } };
  const first = visit(send, shared);
  first.scroll(o.scrollY);
  first.submit(o.form, o.event);
  const stored = shared.storage.data.has(KEY) ? JSON.parse(shared.storage.data.get(KEY)) : null;
  if (o.entry !== undefined) shared.storage.data.set(KEY, o.entry);
  shared.clock.now += o.wait;
  const second = visit(land, shared);
  return {
    stored,
    stored_at_is_number: stored ? typeof stored.at === "number" : null,
    calls: second.log.slice(),
    y: second.win.scrollY,
    left_in_storage: shared.storage.data.has(KEY),
    errors: [first.error, second.error].filter(Boolean),
  };
}

const out = {};

// --- remembering -----------------------------------------------------------------------------------
out.basic = trip({ path: "/articles", search: "?q=x", liveSrc: "/articles?q=x" }, { path: "/articles", search: "?q=x", liveSrc: "/articles?q=x" });
out.tab_stored = trip({ path: "/settings", search: "?tab=general" }, { path: "/settings", search: "?tab=general", liveSrc: "/settings?tab=general" });

out.get_form = trip({}, {}, { form: getForm() });
out.prevented = trip({}, {}, { event: { defaultPrevented: true } });
out.blank_target = trip({}, {}, { form: postForm({ target: "_blank" }) });
out.self_target = trip({}, {}, { form: postForm({ target: "_self" }) });
out.submitter_get = trip({}, {}, { event: { submitter: submitter({ formmethod: "get" }) } });
out.submitter_post_on_get_form = trip({}, {}, { form: getForm(), event: { submitter: submitter({ formmethod: "post" }) } });
out.submitter_blank = trip({}, {}, { event: { submitter: submitter({ formtarget: "_blank" }) } });
out.submitter_plain = trip({}, {}, { event: { submitter: submitter({}) } });
// a field called "target" or "method" or "action" hides the form's own property
out.field_named_target = trip({}, {}, { form: postForm() });
out.field_named_method_get_form = trip({}, {}, { form: getForm() });

// --- restoring -------------------------------------------------------------------------------------
out.other_path = trip({ path: "/articles" }, { path: "/articles/7", liveSrc: "/articles/7" });
out.other_tab = trip({ path: "/settings", search: "?tab=general" }, { path: "/settings", search: "?tab=bambuddy", liveSrc: "/settings?tab=bambuddy" });
out.tab_appears = trip({ path: "/settings", search: "" }, { path: "/settings", search: "?tab=general", liveSrc: "/settings?tab=general" });
out.other_query_ok = trip({ path: "/articles", search: "?q=x" }, { path: "/articles", search: "?q=y&layout=tiles", liveSrc: "/articles" });
out.extra_query_next_to_tab_ok = trip({ path: "/settings", search: "?tab=maintenance" }, { path: "/settings", search: "?tab=maintenance&ops=checked", liveSrc: "/settings" });
out.anchor_wins = trip({}, { hash: "#ops-update" });
out.old = trip({}, {}, { wait: 16000 });
out.not_so_old = trip({}, {}, { wait: 14000 });
out.post_answer = trip({}, { liveSrc: "" });
out.reload = trip({}, { navType: "reload" });
out.back = trip({}, { navType: "back_forward" });
out.no_navigation_api = trip({}, { perfMissing: true });
out.no_redirect = trip({}, { redirects: 0 }); // a link to the same page, not the answer of the form
out.redirect_count_unknown = trip({}, { redirects: undefined });
out.zero = trip({}, {}, { scrollY: 0 });
out.fraction = trip({}, {}, { scrollY: 640.6 });

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
  ["head_text", JSON.stringify({ path: "/articles", tab: "", y: 10, at: 50000, head: "top" })],
  ["array", "[1,2]"],
  ["null", "null"],
].forEach(([name, entry]) => {
  damaged[name] = trip({}, {}, { entry });
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

  // somebody who scrolled while the images came is not pulled back
  first.scroll(900);
  first.submit(postForm());
  const fourth = visit({ maxY: 300 }, shared);
  fourth.scroll(120); // the person scrolls
  fourth.win.maxY = 5000;
  fourth.load();
  out.short_page_but_scrolled = { y: fourth.win.scrollY, calls: fourth.log.slice() };
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

// --- the page head moved (a message above it): the place is kept by what is seen ------------------
out.head_moved_down = trip({ headTop: 100 }, { headTop: 160 }); // a message of 60 px came in
out.head_moved_up = trip({ headTop: 160 }, { headTop: 100 }, { scrollY: 640 }); // the old message is gone
out.head_moved_at_top = trip({ headTop: 100 }, { headTop: 160 }, { scrollY: 0 }); // at the top: the message must be seen
out.head_moved_beyond_top = trip({ headTop: 300 }, { headTop: 100 }, { scrollY: 50 }); // not below zero
out.head_on_one_page_only = trip({ headTop: 100 }, {});
out.head_same = trip({ headTop: 100 }, { headTop: 100 });

// --- trouble on the page: the view goes to it, once restored ---------------------------------------
out.marker_after_restore = trip({}, { markers: true });
out.marker_field_error = trip({}, { markers: [{ kind: "field", id: "f" }] });
out.marker_hidden_first = trip({}, { markers: [{ kind: "field", visible: false, id: "hidden" }, { kind: "field", visible: true, id: "shown" }] });
out.marker_only_hidden = trip({}, { markers: [{ kind: "field", visible: false, id: "hidden" }] });
out.marker_row_on_get_page = trip({}, { markers: [{ kind: "row", id: "r" }] }); // a note that is always there
{
  const shared = { storage: makeStorage({}), clock: { now: 1 } };
  const page = visit({ markers: true }, shared);
  out.marker_without_trip = { calls: page.log.slice() };
}
out.marker_but_anchor = trip({}, { markers: true, hash: "#x" });
out.marker_but_reload = trip({}, { markers: true, navType: "reload" });
// the page that answers the POST itself: no position, but the error is shown
out.post_answer_with_row_error = trip({}, { liveSrc: "", markers: [{ kind: "row", id: "r" }] });
out.post_answer_with_field_error = trip({}, { liveSrc: "", markers: [{ kind: "field", id: "f" }] });
out.post_answer_without_error = trip({}, { liveSrc: "" });
{
  const shared = { storage: makeStorage({}), clock: { now: 1 } };
  const page = visit({ liveSrc: "", markers: [{ kind: "row", id: "r" }] }, shared); // no entry at all
  out.post_answer_without_trip = { calls: page.log.slice() };
}

console.log(JSON.stringify(out));
