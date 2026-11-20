/* "To check": a box in the table head that picks all article rows (half state when only some are picked),
   and "Selected all good" stays off while nothing is picked (p117). Without this file the head has no
   box and the button is on; the server then answers "nothing is selected". The text of the box comes
   from the page (data-label). The list is swapped by live.js now and then, so the box is put back
   whenever the page changes.

   p713 (Spec 8.2): a row with several prints unfolds (details.inspect-prints). live.js swaps the list
   now and then, and confirming one print loads the page anew; both would fold the row again while
   somebody is counting at the shelf. So the open rows (their article ids) are kept in the tab's store
   and opened again after every swap and every load. Rejected: holding the list while a row is open
   (as the queue holds it while a menu is open): a row stays open for long at the shelf, and the list
   would not show new prints meanwhile. Without the store (blocked, private mode) the rows just fold. */
(function () {
  "use strict";

  var HEAD = ".inspect-head-select";
  var ROWS = 'input[type="checkbox"][name="group"][form="inspect-selected"]'; // one box per article row (p713)
  var BUTTON = '#inspect-selected button[type="submit"]';

  // What the head box and the button show for `total` rows of which `ticked` are picked.
  function state(total, ticked) {
    return {
      checked: total > 0 && ticked === total,
      half: ticked > 0 && ticked < total,
      none: ticked === 0
    };
  }

  var STORE = "rb_inspect_open";
  var MAX_OPEN = 50; // the newest ones; older ids of articles long inspected drop out

  // The article ids kept open, from the stored text (a JSON list of strings); anything else is none.
  function openIds(stored) {
    var list;
    try {
      list = JSON.parse(stored || "[]");
    } catch (e) {
      return [];
    }
    if (!Array.isArray(list)) {
      return [];
    }
    return list.filter(function (id) {
      return typeof id === "string";
    });
  }

  // The stored text after the row of `id` was opened (`open`) or folded.
  function remember(stored, id, open) {
    var list = openIds(stored).filter(function (other) {
      return other !== id;
    });
    if (open) {
      list.push(id);
    }
    return JSON.stringify(list.slice(-MAX_OPEN));
  }

  function load() {
    try {
      return window.sessionStorage.getItem(STORE);
    } catch (e) {
      return null;
    }
  }

  function save(text) {
    try {
      window.sessionStorage.setItem(STORE, text);
    } catch (e) {
      return; // blocked: the rows fold on the next swap, nothing else changes
    }
  }

  function reopen() {
    var ids = openIds(load());
    Array.prototype.forEach.call(document.querySelectorAll("details.inspect-prints"), function (details) {
      if (!details.open && ids.indexOf(details.getAttribute("data-article")) >= 0) {
        details.open = true;
      }
    });
  }

  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll(ROWS));
  }

  function sync() {
    var all = rows();
    var ticked = all.filter(function (box) {
      return box.checked;
    }).length;
    var now = state(all.length, ticked);
    var head = document.querySelector(HEAD + " input");
    if (head) {
      head.checked = now.checked;
      head.indeterminate = now.half;
    }
    var button = document.querySelector(BUTTON);
    if (button) {
      button.disabled = now.none;
    }
  }

  function build() {
    var cell = document.querySelector(HEAD);
    if (!cell || cell.querySelector("input")) {
      return;
    }
    var text = cell.getAttribute("data-label") || "";
    var label = document.createElement("label");
    label.className = "inspect-all";
    var holder = document.createElement("span"); // as wide as the box column of the rows, so the boxes line up
    holder.className = "inspect-all-box";
    var box = document.createElement("input");
    box.type = "checkbox";
    holder.appendChild(box);
    var span = document.createElement("span");
    span.className = "inspect-all-text";
    span.textContent = text;
    label.appendChild(holder);
    label.appendChild(span);
    cell.appendChild(label);
  }

  function enhance() {
    build();
    sync();
    reopen();
  }

  if (typeof document === "undefined") {
    module.exports = { state: state, openIds: openIds, remember: remember };
    return;
  }

  // "toggle" does not bubble: listen while it goes down. An error opens its row from the server; that is kept too.
  document.addEventListener("toggle", function (event) {
    var details = event.target;
    if (details && details.matches && details.matches("details.inspect-prints")) {
      save(remember(load(), details.getAttribute("data-article"), details.open));
    }
  }, true);

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (!target || !target.matches) {
      return;
    }
    if (target.matches(HEAD + " input")) {
      rows().forEach(function (box) {
        box.checked = target.checked;
      });
      sync();
    } else if (target.matches(ROWS)) {
      sync();
    }
  });

  // The head box is in no form, so live.js would swap the list away under a focused box: it holds the
  // region while it has the focus (like the drag script does) and lets go afterwards.
  document.addEventListener("focusin", function (event) {
    var target = event.target;
    var region = target && target.matches && target.matches(HEAD + " input") ? target.closest("[data-live-region]") : null;
    if (region) {
      region.setAttribute("data-live-busy", "");
    }
  });
  document.addEventListener("focusout", function (event) {
    var target = event.target;
    var region = target && target.matches && target.matches(HEAD + " input") ? target.closest("[data-live-region]") : null;
    if (region && region.hasAttribute("data-live-busy")) {
      region.removeAttribute("data-live-busy");
      document.dispatchEvent(new CustomEvent("live-idle"));
    }
  });

  var pending = false;
  new MutationObserver(function () {
    if (!pending) {
      pending = true;
      window.setTimeout(function () {
        pending = false;
        enhance();
      }, 0);
    }
  }).observe(document.body, { childList: true, subtree: true });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance);
  } else {
    enhance();
  }
})();
