/* The set form (Spec 5.4, p340; sets in sets 0.4.3 p423), templates/set_form.html.

   An article is only once in a set, on every level: what one row takes is not offered in any other
   drop-down of the form. An article takes itself ("a<id>"); a set from the group "Sets" of the base
   parts takes what its data-takes lists: the set, the sets inside it and all its articles. An entry
   is hidden when one of the things it takes is chosen in another row already. The server renders
   the form that way (routes/sets.py, _blocker); this script keeps it so while the user chooses, and
   shows under a base row what the chosen set contains (data-contains). A row always keeps its own
   choice (a disabled selected option would not be sent). Without this script the server still
   refuses an article or a set chosen twice, with its name.

   The handler sits on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function takes(option) {
    if (!option || !option.value) {
      return [];
    }
    var listed = option.getAttribute("data-takes");
    return listed ? listed.split(" ") : ["a" + option.value];
  }

  function chosen(select) {
    return select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
  }

  // Every option of a drop-down as the page drew it, per group, also those taken out since (p351).
  function drawn(node) {
    if (!node.rbDrawn) {
      node.rbDrawn = Array.prototype.slice.call(node.children);
    }
    return node.rbDrawn;
  }

  // Safari on the iPhone shows an option with `hidden` greyed instead of leaving it out, so an option
  // another row holds is taken out of the drop-down and put back in its old place once it is free
  // (p351). The chosen option of a row is never blocked, so it never moves and stays chosen.
  // Replaced: `hidden` and `disabled` on the option. The same in assembled_form.js.
  // Tests: tests/web/test_form_rows_scripts.py.
  function offerOnly(node, blocked) {
    var next = null;
    var list = drawn(node);
    for (var k = list.length - 1; k >= 0; k -= 1) {
      var child = list[k];
      if (child.tagName === "OPTION" && blocked(child)) {
        if (child.parentNode === node) {
          node.removeChild(child);
        }
        continue;
      }
      if (child.tagName === "OPTION") {
        child.disabled = false;
        child.hidden = false;
      } else if (child.tagName === "OPTGROUP") {
        offerOnly(child, blocked);
      }
      if (child.parentNode !== node) {
        node.insertBefore(child, next);
      }
      next = child;
    }
  }

  function refresh(form) {
    var selects = form.querySelectorAll("select[data-set-article]");
    var counts = {};
    each(selects, function (select) {
      takes(chosen(select)).forEach(function (token) {
        counts[token] = (counts[token] || 0) + 1;
      });
    });
    each(selects, function (select) {
      var own = takes(chosen(select));
      var value = select.value;
      offerOnly(select, function (option) {
        return !!option.value && option.value !== value && takes(option).some(function (token) {
          return (counts[token] || 0) - (own.indexOf(token) >= 0 ? 1 : 0) > 0;
        });
      });
      var row = select.closest("[data-more-row]");
      var hint = row ? row.querySelector("[data-set-contains]") : null;
      if (hint) {
        var option = chosen(select);
        hint.textContent = option ? option.getAttribute("data-contains") || "" : "";
      }
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

  // A form the live channel swaps in (live.js) is drawn by the server with the options only hidden:
  // they are taken out here as well (p351). Options this script puts back are no form, so nothing loops.
  if (window.MutationObserver && document.body) {
    new window.MutationObserver(function (records) {
      records.forEach(function (record) {
        each(record.addedNodes, function (node) {
          if (node.nodeType !== 1) {
            return;
          }
          if (node.matches("[data-set-form]")) {
            refresh(node);
          }
          each(node.querySelectorAll("[data-set-form]"), refresh);
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
