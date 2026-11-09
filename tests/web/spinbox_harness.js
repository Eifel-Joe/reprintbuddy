// Runs static/js/spinbox.js without a browser: the numbers on their own, then the script on a small
// stand-in for the document. Prints one JSON object; test_spinbox.py compares it.
const path = require("path");
const scriptPath = path.resolve(process.argv[2]);

// --- the numbers ----------------------------------------------------------------------------
const api = require(scriptPath);
const out = {};
out.read = ["1,5", "1.5", " 12 ", "1 000", "", "abc", "1,2,3", "-3", "+4", null, "1e3", "٣"].map((t) => api.readNumber(t));
out.step = [
  [null, 1, { step: 1, min: 0, max: null }],
  [null, -1, { step: 1, min: 0, max: null }],
  [null, -1, { step: 1, min: null, max: null }],
  [0, -1, { step: 1, min: 0, max: null }],
  [0.1, 1, { step: 0.1, min: null, max: null }],
  [0.2, 1, { step: 0.1, min: null, max: null }],
  [9, 1, { step: 5, min: 0, max: 10 }],
  [15, -1, { step: 10, min: 1, max: null }],
  [500, 1, { step: 1, min: 1, max: 100 }],
  [2.5, 1, { step: 0.25, min: null, max: null }],
  [3, 1, { step: 0, min: 0, max: null }],
].map(([cur, dir, o]) => api.stepped(cur, dir, o));
out.write = [
  [1.5, 1, "de", false], [1.5, 1, "en", false], [1.5, 1, "de-DE", false], [1.5, 1, "de", true],
  [7, 0, "de", false], [0.3, 1, "de", false],
].map(([v, d, l, p]) => api.writeNumber(v, d, l, p));
out.decimals = ["1", "0.1", "0.25", "5"].map((s) => api.decimalsOf(s));

// --- the script on a stand-in document ----------------------------------------------------------
class El {
  constructor(tag, attrs = {}) {
    this.nodeType = 1; this.tagName = tag; this.attrs = { ...attrs }; this.children = [];
    this.parentNode = null; this.listeners = {}; this.className = ""; this.value = attrs.value || "";
    this.disabled = "disabled" in attrs; this.readOnly = "readonly" in attrs; this.fired = [];
    this.type = attrs.type || (tag === "button" ? "submit" : "text"); this.tabIndex = 0;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  appendChild(node) {
    if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
    node.parentNode = this; this.children.push(node); return node;
  }
  insertBefore(node, ref) { node.parentNode = this; this.children.splice(this.children.indexOf(ref), 0, node); return node; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  dispatchEvent(ev) { this.fired.push(ev.type); (this.listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; }
  matches() {
    return this.tagName === "input" && (this.attrs.type === "number" || this.attrs.inputmode === "numeric" || this.attrs.inputmode === "decimal");
  }
  querySelectorAll() {
    const found = [];
    const walk = (n) => n.children.forEach((c) => { if (c.matches()) found.push(c); walk(c); });
    walk(this);
    return found;
  }
}
const input = (attrs) => new El("input", attrs);
const body = new El("body");
const row = new El("div"); body.appendChild(row);
const text = input({ inputmode: "numeric", min: "1", max: "3", value: "2" });
const num = input({ type: "number", step: "0.5", value: "5", inputmode: "decimal" });
const free = input({ inputmode: "numeric", value: "" }); // no min: counts from 0
const optOut = input({ inputmode: "numeric", "data-no-spin": "" });
const locked = input({ inputmode: "numeric", disabled: "", value: "4" });
const plain = input({ type: "text", value: "x" });
[text, num, free, optOut, locked, plain].forEach((i) => row.appendChild(i));
let observer = null;
global.window = {
  setTimeout, clearTimeout, setInterval, clearInterval,
  MutationObserver: class { constructor(cb) { this.cb = cb; observer = this; } observe() {} },
};
global.document = { readyState: "complete", body, documentElement: { lang: "de" }, createElement: (t) => new El(t), addEventListener() {} };
delete require.cache[scriptPath];
require(scriptPath);

const wrapOf = (i) => (i.parentNode && i.parentNode.className === "spin" ? i.parentNode : null);
const buttonsOf = (i) => wrapOf(i).children[1].children; // [up, down]
const press = (button) => {
  button.listeners.pointerdown.forEach((fn) => fn({ button: 0, preventDefault() {} }));
  button.listeners.pointerup.forEach((fn) => fn({}));
};
out.wrapped = [text, num, free, optOut, locked, plain].map((i) => wrapOf(i) !== null);
out.hidden_from_assistive_tech = buttonsOf(text).map((b) => [b.attrs["aria-hidden"], b.tabIndex]);

const log = [];
const snap = (label, i) => log.push([label, i.value, i.fired.join(","), buttonsOf(i).map((b) => b.disabled)]);
snap("start", text);
press(buttonsOf(text)[0]); snap("up 2->3", text); // at max: the up arrow is off
press(buttonsOf(text)[0]); snap("up at max", text); // nothing more, no new events
press(buttonsOf(text)[1]); press(buttonsOf(text)[1]); snap("down twice", text); // 3->2->1, the down arrow off
press(buttonsOf(text)[1]); snap("down at min", text);
out.text = log;

const l2 = [];
press(buttonsOf(num)[0]); l2.push(num.value); // type=number: a point, 5 + 0.5
press(buttonsOf(num)[0]); l2.push(num.value);
out.number = l2;

press(buttonsOf(free)[0]); const a = free.value; free.value = ""; press(buttonsOf(free)[1]);
out.free = [a, free.value];
free.value = "1,5"; free.dispatchEvent(new Event("input")); press(buttonsOf(free)[0]);
out.comma_in = free.value; // an integer field reads 1,5 and counts on from it

const key = (i, k, mods = {}) => {
  let stopped = false;
  i.listeners.keydown.forEach((fn) => fn({ key: k, ...mods, preventDefault() { stopped = true; } }));
  return stopped;
};
text.value = "2";
out.keys = [key(text, "ArrowUp"), text.value, key(text, "ArrowDown"), text.value, key(text, "ArrowUp", { ctrlKey: true }), text.value, key(text, "a"), text.value];
out.number_has_no_key_handler = !num.listeners.keydown;

out.locked = (() => { const before = locked.value; press(buttonsOf(locked)[0]); return [before, locked.value, buttonsOf(locked).map((b) => b.disabled)]; })();

// a field that appears later (a cloned row, a region swapped in) is enhanced once; a second look changes nothing
const later = input({ inputmode: "numeric", value: "7" });
const swapped = new El("div"); swapped.appendChild(later); body.appendChild(swapped);
observer.cb([{ addedNodes: [swapped] }]); observer.cb([{ addedNodes: [swapped] }]);
out.later = [wrapOf(later) !== null, wrapOf(later).children.filter((c) => c.className === "spin-btns").length, later.parentNode.parentNode === swapped];
observer.cb([{ addedNodes: [wrapOf(later)] }]); // an enhanced field that moves with its wrapper is not wrapped twice
out.not_twice = later.parentNode.parentNode === swapped;
console.log(JSON.stringify(out));
process.exit(0);
