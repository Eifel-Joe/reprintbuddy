/* The switch of the column "Verbrauch" on the filament page (Spec 9.3, p717). No texts in here: the
   page carries them, and every view of every row is in the page already.

   Without this script the page shows Ø 30 days and the switch stays hidden (the default of the Spec).
   With it the switch is shown; the choice is applied by hiding all views but the chosen one and kept in
   the storage of the browser, so the next visit opens with it. Blocked storage (a private window, a
   setting) only means the choice lasts until the page is left.

   live.js replaces the list region when something changes (Spec 10.5): the new region comes with the
   default again, so the choice is applied again whenever a switch appears that is still hidden.
   Rejected: a cookie read by the server (the page would show the choice without the script, against
   the Spec's "ohne JavaScript gilt die Vorgabe"). */
(function () {
  "use strict";

  var KEY = "rb_usage_view";

  function saved() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null; /* storage blocked */
    }
  }

  function save(view) {
    try {
      window.localStorage.setItem(KEY, view);
    } catch (e) {
      /* storage blocked: the choice lasts until the page is left */
    }
  }

  function show(view) {
    var nodes = document.querySelectorAll("[data-usage], [data-usage-head]");
    Array.prototype.forEach.call(nodes, function (node) {
      var own = node.getAttribute("data-usage") || node.getAttribute("data-usage-head");
      node.hidden = own !== view;
    });
  }

  function apply() {
    var choice = document.querySelector("[data-usage-choice]");
    var select = choice && choice.querySelector("[data-usage-select]");
    if (!select || !choice.hidden) {
      return; /* no list, or already applied */
    }
    var view = saved();
    var known = Array.prototype.some.call(select.options, function (option) {
      return option.value === view;
    });
    if (known) {
      select.value = view;
    }
    show(select.value);
    choice.hidden = false;
  }

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || !select.hasAttribute || !select.hasAttribute("data-usage-select")) {
      return;
    }
    show(select.value);
    save(select.value);
  });

  apply();
  if (window.MutationObserver) {
    new MutationObserver(apply).observe(document.body, { childList: true, subtree: true });
  }
})();
