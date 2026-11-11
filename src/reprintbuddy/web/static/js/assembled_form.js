/* The form of an article made of parts (Spec 5.6, 0.4.0 p341), templates/assembled_form.html.

   An article chosen in the base parts is offered in no other row; one chosen in choice parts 1 is
   not offered in the base parts or in choice parts 2, and the other way round. Inside one field of
   choice parts an article may repeat over its options (the side part in -L32 and -L40), so a row
   there only hides what the base parts and the other field hold. The server renders the form that
   way; this script keeps it so while the user chooses. A row always keeps its own choice (a
   disabled selected option would not be sent). Without this script the server still refuses an
   article in the wrong place, with its name.

   The handler sits on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function refresh(form) {
    var selects = form.querySelectorAll("select[data-assembled-article]");
    var chosen = { base: {}, c1: {}, c2: {} };
    each(selects, function (select) {
      var place = select.getAttribute("data-assembled-article");
      if (select.value && chosen[place]) {
        chosen[place][select.value] = (chosen[place][select.value] || 0) + 1;
      }
    });
    each(selects, function (select) {
      var place = select.getAttribute("data-assembled-article");
      each(select.options, function (option) {
        if (!option.value || option.value === select.value) {
          option.disabled = false;
          option.hidden = false;
          return;
        }
        var blocked = false;
        Object.keys(chosen).forEach(function (other) {
          if (other !== place && chosen[other][option.value]) {
            blocked = true;
          }
        });
        if (place === "base" && chosen.base[option.value]) {
          blocked = true; // a base part stands in one row only
        }
        option.disabled = blocked;
        option.hidden = blocked;
      });
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.tagName === "SELECT" && target.hasAttribute("data-assembled-article")) {
      var form = target.closest("[data-assembled-form]");
      if (form) {
        refresh(form);
      }
    }
  });

  // Rows, options and choice parts 2 added in place (more_rows.js) follow the same rules.
  document.addEventListener("more:added", function (event) {
    var form = event.target.closest ? event.target.closest("[data-assembled-form]") : null;
    if (form) {
      refresh(form);
    }
  });

  each(document.querySelectorAll("[data-assembled-form]"), refresh);
})();
