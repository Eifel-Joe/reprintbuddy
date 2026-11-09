// Runs web/static/js/flash.js against a small stand-in for the page, with a clock of its own, and prints
// what the messages looked like at the moments that matter, as JSON. Used by test_flash_dismiss.py (needs node).
"use strict";
const fs = require("fs");
const vm = require("vm");

function element(tag, classes, attrs) {
  const store = Object.assign({}, attrs || {});
  const listeners = {};
  const set = new Set(classes || []);
  const el = {
    tagName: tag.toUpperCase(), children: [], parentNode: null, textContent: "", type: "",
    classList: { contains: (c) => set.has(c), add: (c) => set.add(c), remove: (c) => set.delete(c) },
    getAttribute: (name) => (name in store ? store[name] : null),
    setAttribute: (name, value) => { store[name] = String(value); },
    appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
    removeChild(child) { el.children = el.children.filter((c) => c !== child); child.parentNode = null; return child; },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
    dispatch: (name) => (listeners[name] || []).forEach((fn) => fn({ type: name, target: el })),
  };
  Object.defineProperty(el, "className", { get: () => Array.from(set).join(" "), set: (v) => { set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => set.add(c)); } });
  return el;
}

function world(options) {
  const opts = Object.assign({ reduced: false, hidden: false, boxes: true }, options || {});
  let now = 0;
  const timers = [];
  let next = 1;
  const docListeners = {};
  const container = element("div", ["flashes"], { "data-flashes": "", "data-label-close": "Dismiss message" });
  const success = container.appendChild(element("p", ["flash"], { role: "status" }));
  success.textContent = "Saved.";
  const alert = container.appendChild(element("p", ["alert"], { role: "alert" }));
  const body = element("body");
  const main = element("main");
  main.focused = false;
  main.focus = () => { main.focused = true; };
  const document = {
    body,
    hidden: opts.hidden,
    readyState: "complete",
    querySelectorAll: (sel) => (sel === "[data-flashes]" && opts.boxes ? [container] : []),
    querySelector: (sel) => (sel === "main" ? main : null),
    createElement: (tag) => element(tag),
    addEventListener: (name, fn) => { (docListeners[name] = docListeners[name] || []).push(fn); },
  };
  const context = {
    document,
    window: { matchMedia: (q) => ({ matches: opts.reduced && /prefers-reduced-motion/.test(q) }) },
    Date: { now: () => now },
    setTimeout: (fn, ms) => { const id = next++; timers.push({ id, at: now + ms, fn }); return id; },
    clearTimeout: (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); },
    Array, Math,
  };
  context.window.setTimeout = context.setTimeout;
  context.window.clearTimeout = context.clearTimeout;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
  function advance(ms) {
    const end = now + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      if (!timers.length || timers[0].at > end) break;
      const t = timers.shift();
      now = t.at;
      t.fn();
    }
    now = end;
  }
  const state = (el) => {
    const present = container.children.indexOf(el) >= 0;
    return { present, leaving: present && el.classList.contains("flash-leaving") };
  };
  const setHidden = (value) => { document.hidden = value; (docListeners.visibilitychange || []).forEach((fn) => fn({})); };
  container.querySelector = (sel) => {
    const find = (el) => {
      for (const c of el.children) {
        if (sel === ".flash-close" && c.classList.contains("flash-close")) return c;
        const d = find(c);
        if (d) return d;
      }
      return null;
    };
    return find(container);
  };
  return { success, alert, container, body, main, advance, state, setHidden };
}

const out = {};

let w = world();
w.advance(7999);
out.success_before = w.state(w.success);
w.advance(1);
out.fading_mid = w.state(w.success);
w.advance(1000);
out.success_after = w.state(w.success);

w = world({ reduced: true });
w.advance(8000);
out.reduced_after = w.state(w.success);

w = world();
w.advance(100000);
const button = w.alert.children.find((c) => c.tagName === "BUTTON");
out.alert_after_long = { present: w.state(w.alert).present, button: button ? button.getAttribute("aria-label") : null };
button.dispatch("click");
out.alert_button_removes = !w.state(w.alert).present;
out.focus_goes_to_main = w.main.focused;

// good news is also said by a hidden live region, a moment after the load and long before it goes
w = world();
w.advance(99);
out.announce_before = w.body.children.length ? w.body.children[0].textContent : null;
w.advance(1);
const region = w.body.children[0];
out.announce_after = region
  ? { role: region.getAttribute("role"), live: region.getAttribute("aria-live"), cls: region.className, text: region.textContent }
  : null;
out.announce_only_good_news = w.body.children.length === 1;

// the focus on the message holds the clock, too (a keyboard user), together with the pointer
w = world();
w.advance(4000);
w.success.dispatch("focusin");
w.advance(20000);
out.focus_paused = { present: w.state(w.success).present };
w.success.dispatch("mouseenter");
w.success.dispatch("focusout");
w.advance(20000);
out.focus_and_pointer_paused = { present: w.state(w.success).present };
w.success.dispatch("mouseleave");
w.advance(4300); // the rest of the clock, and the fade
out.focus_and_pointer_released = { present: w.state(w.success).present };

w = world();
w.advance(4000);
w.success.dispatch("mouseenter");
w.advance(10000);
out.hover_paused = { present: w.state(w.success).present };
w.success.dispatch("mouseleave");
w.advance(3999);
out.hover_resumed_early = { present: w.state(w.success).present };
w.advance(1000);
out.hover_resumed_end = { present: w.state(w.success).present };

w = world({ hidden: true });
w.advance(20000);
out.hidden_tab_waiting = { present: w.state(w.success).present };
w.setHidden(false);
w.advance(9000);
out.visible_again_end = { present: w.state(w.success).present };

try {
  world({ boxes: false });
  out.no_box_ok = true;
} catch (e) {
  out.no_box_ok = false;
}

process.stdout.write(JSON.stringify(out));
