/* "To check": a box in the table head that picks all prints (half state when only some are picked),
   and "Selected all good" stays off while nothing is picked (p117). Without this file the head has no
   box and the button is on; the server then answers "nothing is selected". The text of the box comes
   from the page (data-label). The list is swapped by live.js now and then, so the box is put back
   whenever the page changes. */
(function () {
  "use strict";

  var HEAD = ".inspect-head-select";
  var ROWS = 'input[type="checkbox"][name="ids"][form="inspect-selected"]';
  var BUTTON = '#inspect-selected button[type="submit"]';

  // What the head box and the button show for `total` rows of which `ticked` are picked.
  function state(total, ticked) {
    return {
      checked: total > 0 && ticked === total,
      half: ticked > 0 && ticked < total,
      none: ticked === 0
    };
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
  }

  if (typeof document === "undefined") {
    module.exports = { state: state };
    return;
  }

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
