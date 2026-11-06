/* The purchased parts of an article (Spec 5.2), templates/_article_parts.html.

   Adds and removes rows of the parts list in place, and offers in each row only the parts no
   other row has chosen. Without this script the buttons send the form, which comes back with
   the row added or removed and nothing saved; the page stays usable either way.

   The handlers sit on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. A removed row changes no field, so the form is marked with
   data-changed: live.js then treats it as changed and does not swap the user's work away. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function blockOf(node) {
    return node && node.closest ? node.closest("[data-parts]") : null;
  }

  function markChanged(block) {
    var form = block.closest("form");
    if (form) {
      form.setAttribute("data-changed", "");
    }
  }

  // A part chosen in one row is not offered in another; a row always keeps its own choice
  // (a disabled selected option would not be sent, and the rows would no longer pair up).
  function refresh(block) {
    var selects = block.querySelectorAll("[data-parts-rows] select");
    var taken = {};
    var count = 0;
    each(selects, function (select) {
      if (select.value) {
        if (!taken[select.value]) {
          count += 1;
        }
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
    var add = block.querySelector("[data-parts-add]");
    var template = block.querySelector("[data-parts-template]");
    if (add && template) {
      var offered = template.content.querySelectorAll("option:not([value=''])").length;
      add.disabled = count >= offered; // every part is in the list already
    }
  }

  function newRow(block) {
    var template = block.querySelector("[data-parts-template]");
    var row = template.content.querySelector("[data-parts-row]").cloneNode(true);
    block.querySelector("[data-parts-rows]").appendChild(row);
    return row;
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    var add = target.closest ? target.closest("[data-parts-add]") : null;
    var remove = target.closest ? target.closest("[data-parts-remove]") : null;
    var block = blockOf(add || remove);
    if (!block) {
      return;
    }
    event.preventDefault(); // no round trip to the server
    if (add) {
      var row = newRow(block);
      refresh(block);
      row.querySelector("select").focus();
    } else {
      remove.closest("[data-parts-row]").remove();
      if (!block.querySelector("[data-parts-rows] [data-parts-row]")) {
        newRow(block); // the list keeps one row to choose in
      }
      refresh(block);
      block.querySelector("[data-parts-add]").focus();
    }
    markChanged(block);
  });

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.tagName === "SELECT" && target.closest("[data-parts-rows]")) {
      refresh(blockOf(target));
    }
  });

  each(document.querySelectorAll("[data-parts]"), refresh);
})();
