// Runs the form scripts without a browser on a small stand-in for the document (p392, p351):
//   node form_rows_harness.js <more_rows.js> <set_form.js> <assembled_form.js>
// Prints one JSON object; test_form_rows_scripts.py compares it. The stand-in knows only what these
// scripts use: simple selectors ("tag", "[attr]", "[attr=value]", lists with commas), closest,
// elements before and after, <template>, <select> with its options, and one MutationObserver.
const path = require("path");
const [moreRows, setForm, assembledForm] = process.argv.slice(2).map((p) => path.resolve(p));

function parse(selector) {
  return selector.split(",").map((part) => {
    const m = part.trim().match(/^([a-zA-Z]*)((?:\[[^\]]+\])*)$/);
    if (!m) return null; // anything finer (":not(...)") matches nothing here
    const attrs = [...m[2].matchAll(/\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g)].map((a) => [a[1], a[2]]);
    return { tag: m[1].toUpperCase(), attrs };
  }).filter(Boolean);
}

class El {
  constructor(tag, attrs = {}) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = { ...attrs }; this.children = [];
    this.parentNode = null; this.listeners = {}; this.disabled = "disabled" in attrs; this.hidden = "hidden" in attrs;
    this.selected = "selected" in attrs;
    if (this.tagName === "TEMPLATE") this.content = new El("fragment");
    if (this.tagName === "SELECT") {
      Object.defineProperty(this, "value", { get() { const o = this.options; const s = o.find((x) => x.selected) || o[0]; return s ? s.value : ""; } });
    } else {
      this.value = attrs.value || "";
    }
  }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const list = this.parentNode.children;
    return list[list.indexOf(this) + 1] || null;
  }
  get options() { return this.querySelectorAll("option"); }
  get selectedIndex() { return this.options.findIndex((o) => o.selected); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this; this.children.push(node); return node;
  }
  insertBefore(node, ref) {
    if (ref === null || ref === undefined) return this.appendChild(node);
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this; this.children.splice(this.children.indexOf(ref), 0, node); return node;
  }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; return node; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(node) { const parent = this.parentNode; parent.insertBefore(node, this); parent.removeChild(this); }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    for (let node = this; node; node = node.parentNode) (node.listeners[ev.type] || []).forEach((fn) => fn(ev));
    if (ev.bubbles !== false) (document.listeners[ev.type] || []).forEach((fn) => fn(ev));
    return true;
  }
  matches(selector) {
    return parse(selector).some((s) => (!s.tag || s.tag === this.tagName)
      && s.attrs.every(([k, v]) => k in this.attrs && (v === undefined || this.attrs[k] === v)));
  }
  closest(selector) {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (n) => n.children.forEach((c) => {
      if (c.matches(selector)) found.push(c);
      if (c.tagName !== "TEMPLATE") walk(c);
    });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get classList() { return { add() {}, remove() {} }; }
  getBoundingClientRect() { return { top: 0, bottom: 0 }; }
  focus() {}
}

const el = (tag, attrs = {}, ...children) => {
  const node = new El(tag, attrs);
  children.forEach((c) => node.appendChild(c));
  return node;
};

let observers = [];
const body = el("body");
global.document = {
  body, listeners: {}, readyState: "complete",
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
  querySelectorAll: (s) => body.querySelectorAll(s),
  importNode: (node) => node,
};
global.window = {
  setTimeout, clearTimeout, innerHeight: 800, matchMedia: () => ({ matches: true }),
  MutationObserver: class { constructor(cb) { this.cb = cb; observers.push(this); } observe() {} },
};
global.MutationObserver = window.MutationObserver;
global.CustomEvent = class { constructor(type, init = {}) { this.type = type; this.bubbles = !!init.bubbles; this.detail = init.detail; } };
// The live channel swaps a region: put the new one in and tell every observer, as a browser does.
const swapIn = (old, fresh) => {
  old.replaceWith(fresh);
  observers.forEach((o) => o.cb([{ addedNodes: [fresh] }]));
};
const out = {};

// ---- p392: "more rows" in a full part after the live channel swapped the form ----------------------
const part = (rows) => el("div", { "data-live-region": "form" },
  el("div", { "data-more-group": "rows", "data-more-max": "2", "data-more-step": "1" },
    el("input", { type: "hidden", name: "rows", value: String(rows) }),
    ...Array.from({ length: rows }, () => el("div", { "data-more-row": "" })),
    el("template", { "data-more-template": "" }),
    el("button", { "data-more-add": "" })));
const first = part(2);
body.appendChild(first);
require(moreRows);
// 0.4.7 (R7): the drop-down helpers of both forms live in more_rows.js, loaded first on both pages and
// alone on the order form, where nothing may break.
out.shared_rows = [typeof (window.ReprintbuddyRows && window.ReprintbuddyRows.offerOnly),
  typeof (window.ReprintbuddyRows && window.ReprintbuddyRows.drawn)];
const button = (region) => region.querySelector("[data-more-add]");
out.full_at_load = button(first).disabled;
const fresh = part(2);
swapIn(first, fresh);
out.full_after_swap = button(fresh).disabled;
const roomy = part(1);
swapIn(fresh, roomy);
out.room_after_swap = button(roomy).disabled;

// ---- p351: a drop-down leaves out what another row holds, instead of only hiding it ------------------
// What a row offers: the values of the options that are in its drop-down, in their order.
const offered = (select) => select.options.map((o) => o.value);
const choose = (select, value) => {
  select.options.forEach((o) => { o.selected = o.value === value; });
  select.dispatchEvent({ type: "change", bubbles: true });
};
const option = (value, attrs = {}) => el("option", { value, ...attrs });
// As the server draws it (routes/sets.py): the article chosen in row 1 is hidden in row 2 already.
const setSelect = (chosen, hiddenValues) => {
  const mark = (v) => ({ ...(chosen === v ? { selected: "" } : {}), ...(hiddenValues.includes(v) ? { disabled: "", hidden: "" } : {}) });
  return el("select", { "data-set-article": "" }, option("", chosen ? {} : { selected: "" }),
    el("optgroup", { label: "Articles" }, option("1", mark("1")), option("2", mark("2")), option("3", mark("3"))),
    el("optgroup", { label: "Sets" }, option("s9", { "data-takes": "s9 a3", ...mark("s9") })));
};
const setFormOf = () => {
  const one = setSelect("1", []);
  const two = setSelect("", ["1"]);
  return [el("div", { "data-live-region": "main" }, el("form", { "data-set-form": "" },
    el("div", { "data-more-row": "" }, one), el("div", { "data-more-row": "" }, two))), one, two];
};
const [setRegion, s1, s2] = setFormOf();
body.appendChild(setRegion);
require(setForm);
const sets = {};
sets.at_load = [offered(s1), offered(s2), s2.querySelectorAll("option[hidden]").length];
choose(s2, "s9"); // the set takes article 3 too
sets.after_choosing_a_set = [offered(s1), offered(s2), s1.value];
choose(s2, "");
sets.after_taking_it_back = [offered(s1), offered(s2), s1.value];
const [setRegion2, t1, t2] = setFormOf();
swapIn(setRegion, setRegion2);
sets.after_live_swap = [offered(t1), offered(t2)];
out.sets = sets;

const aSelect = (place, chosen, hiddenValues) => el("select", { "data-assembled-article": place },
  option("", chosen ? {} : { selected: "" }),
  ...["1", "2", "3"].map((v) => option(v, { ...(chosen === v ? { selected: "" } : {}), ...(hiddenValues.includes(v) ? { disabled: "", hidden: "" } : {}) })));
const b1 = aSelect("base", "1", []);
const b2 = aSelect("base", "", ["1"]);
const c1 = aSelect("c1", "", ["1"]);
body.appendChild(el("form", { "data-assembled-form": "" }, b1, b2, c1));
require(assembledForm);
const parts = {};
parts.at_load = [offered(b1), offered(b2), offered(c1)];
choose(c1, "2");
parts.after_choosing_in_choice_parts = [offered(b1), offered(b2), offered(c1), b1.value];
out.assembled = parts;

console.log(JSON.stringify(out));
