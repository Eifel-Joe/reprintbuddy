// Runs web/static/js/zone.js against a small stand-in for a page and its cookies, once per case, and prints
// what it did as JSON: the cookies it wrote and whether it loaded the page again. Used by
// test_zone_script.py (needs node).
"use strict";
const fs = require("fs");
const vm = require("vm");

const script = fs.readFileSync(process.argv[2], "utf8");

function field(tag, props) {
  return Object.assign({ tagName: tag, type: "", value: "", defaultValue: "", checked: false, defaultChecked: false }, props);
}

function form(elements, attrs) {
  const store = Object.assign({}, attrs || {});
  return { elements, hasAttribute: (name) => name in store };
}

function page(options) {
  const opts = Object.assign(
    {
      zone: "Europe/Berlin", // what the browser says
      dataZone: "UTC", // the zone the server computed the page in
      formAnswer: false,
      app: true, // a page with the menu and live.js; the sign-in page has none
      cookies: {}, // what the request carried
      blocked: false, // the browser keeps no cookie of this page
      throws: false, // writing a cookie throws (a sandboxed frame)
      https: false,
      forms: [],
      intlThrows: false,
    },
    options || {},
  );
  const jar = Object.assign({}, opts.cookies);
  const written = [];
  let reloads = 0;
  const bodyAttrs = { "data-zone": opts.dataZone };
  if (opts.formAnswer) bodyAttrs["data-form-answer"] = "";
  const body = {
    getAttribute: (name) => (name in bodyAttrs ? bodyAttrs[name] : null),
    hasAttribute: (name) => name in bodyAttrs,
  };
  const document = {
    body,
    forms: opts.forms,
    getElementById: (id) => (id === "app" && opts.app ? {} : null),
  };
  Object.defineProperty(document, "cookie", {
    get: () => Object.keys(jar).map((name) => name + "=" + jar[name]).join("; "),
    set: (text) => {
      if (opts.throws) throw new Error("SecurityError");
      written.push(text);
      if (opts.blocked) return;
      const parts = String(text).split(";").map((p) => p.trim());
      const eq = parts[0].indexOf("=");
      const name = parts[0].slice(0, eq);
      const value = parts[0].slice(eq + 1);
      if (parts.some((p) => /^max-age=0$/i.test(p))) delete jar[name];
      else jar[name] = value;
    },
  });
  const Intl = {
    DateTimeFormat: () => ({
      resolvedOptions: () => {
        if (opts.intlThrows) throw new Error("no Intl");
        return { timeZone: opts.zone };
      },
    }),
  };
  const location = { protocol: opts.https ? "https:" : "http:", reload: () => { reloads += 1; } };
  const context = { document, Intl, location, window: { location } };
  vm.createContext(context);
  let error = null;
  try {
    vm.runInContext(script, context);
  } catch (e) {
    error = String(e);
  }
  return { reloads, written, jar, error };
}

const out = {};

// The first page of a browser without the cookie, computed in the server's zone: the cookie, one reload,
// marked as a reload by Reprintbuddy itself.
out.differs = page();
// The next page carries the cookie and is computed in the browser's zone: no reload.
out.same = page({ dataZone: "Europe/Berlin", cookies: { rb_zone: "Europe/Berlin" } });
// Computed in another zone although the request carried the browser's zone (the server does not know the
// name, or another tab wrote it meanwhile): no reload, or it would go on for ever.
out.refused = page({ dataZone: "UTC", cookies: { rb_zone: "Europe/Berlin" } });
// Travelled: the cookie still names the old zone.
out.travelled = page({ dataZone: "Europe/Berlin", zone: "America/New_York", cookies: { rb_zone: "Europe/Berlin" } });
// Not loaded again: a changed form (each kind of field), the answer to a form, blocked cookies.
out.changed_text = page({ forms: [form([field("INPUT", { type: "text", value: "3", defaultValue: "" })])] });
out.changed_box = page({ forms: [form([field("INPUT", { type: "checkbox", checked: true })])] });
out.changed_select = page({
  forms: [form([{ tagName: "SELECT", multiple: false, selectedIndex: 1, options: [{ defaultSelected: true }, { defaultSelected: false }] }])],
});
out.changed_marked = page({ forms: [form([], { "data-changed": "" })] });
out.unchanged_form = page({
  forms: [form([
    field("INPUT", { type: "text", value: "a", defaultValue: "a" }),
    field("INPUT", { type: "hidden", value: "token", defaultValue: "" }),
    { tagName: "SELECT", multiple: false, selectedIndex: 0, options: [{ defaultSelected: false }, { defaultSelected: false }] },
    field("BUTTON"),
  ])],
});
out.form_answer = page({ formAnswer: true });
out.blocked = page({ blocked: true });
out.throws = page({ throws: true });
// The sign-in page (no menu, no live.js that would clear the mark): reloaded without the mark.
out.bare = page({ app: false });
out.https = page({ https: true, dataZone: "Europe/Berlin", cookies: { rb_zone: "Europe/Berlin" } });
// Nothing a cookie could be made of, or no zone at all: nothing written, nothing loaded.
out.odd_zone = page({ zone: "Europe/Berlin; path=/x" });
out.no_zone = page({ zone: "" });
out.no_intl = page({ intlThrows: true });

process.stdout.write(JSON.stringify(out));
