/* The set form (Spec 5.4, p340), templates/set_form.html.

   An article is only once in a set: one chosen in a row of the base parts or of the choice parts
   is not offered in any other drop-down of the form. The server renders the form that way; this
   script keeps it so while the user chooses. A row always keeps its own choice (a disabled
   selected option would not be sent). Without this script the server still refuses an article
   chosen twice, with its name.

   The handler sits on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function refresh(form) {
    var selects = form.querySelectorAll("select[data-set-article]");
    var taken = {};
    each(selects, function (select) {
      if (select.value) {
        taken[select.value] = (taken[select.value] || 0) + 1;
      }
    });
    each(selects, function (select) {
      each(select.options, function (option) {
        if (!option.value) {
          return;
        }
        var elsewhere = option.value !== select.value && (taken[option.value] || 0) > 0;
        option.disabled = elsewhere;
        option.hidden = elsewhere;
      });
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.tagName === "SELECT" && target.hasAttribute("data-set-article")) {
      var form = target.closest("[data-set-form]");
      if (form) {
        refresh(form);
      }
    }
  });

  // Rows added in place (more_rows.js) follow the same rule.
  document.addEventListener("more:added", function (event) {
    var form = event.target.closest ? event.target.closest("[data-set-form]") : null;
    if (form) {
      refresh(form);
    }
  });

  each(document.querySelectorAll("[data-set-form]"), refresh);
})();
