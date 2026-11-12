// Runs static/js/inventory_field.js without a browser on stand-in selects (p362). Prints one JSON
// object; test_inventory_field.py compares it.
const path = require("path");
const api = require(path.resolve(process.argv[2]));

function option(value, attrs = {}) {
  return { value, hidden: false, disabled: false, hasAttribute: (k) => k in attrs };
}

function select(options, selectedIndex) {
  const s = { options, selectedIndex };
  Object.defineProperty(s, "value", {
    get() { return s.selectedIndex >= 0 ? s.options[s.selectedIndex].value : ""; },
    set(v) { s.selectedIndex = s.options.findIndex((o) => o.value === v); },
  });
  return s;
}

function state(field) {
  return { value: field.value, awaiting: { hidden: field.options[1].hidden, disabled: field.options[1].disabled } };
}

function run(articleIndex, fieldValue) {
  const articles = select([option(""), option("1"), option("7", { "data-stock-only": "" })], articleIndex);
  const field = select([option("stock"), option("awaiting_assembly")], fieldValue === "stock" ? 0 : 1);
  api.apply(articles, field);
  return state(field);
}

const out = {
  none: run(0, "awaiting_assembly"), // nothing chosen: both values stay
  print: run(1, "awaiting_assembly"), // a print article: both values stay
  assembled: run(2, "awaiting_assembly"), // an article made of parts: only the stock
};
// back to a print article: the value comes back
const articles = select([option(""), option("1"), option("7", { "data-stock-only": "" })], 2);
const field = select([option("stock"), option("awaiting_assembly")], 0);
api.apply(articles, field);
articles.selectedIndex = 1;
api.apply(articles, field);
out.back = state(field);
process.stdout.write(JSON.stringify(out));
