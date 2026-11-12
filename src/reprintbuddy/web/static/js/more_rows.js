/* The buttons that only show more room in a form (0.4.1, p380): more rows, more options, a second
   field of choice parts. One script for the set form, the form of an article made of parts and the
   new order (templates/set_form.html, assembled_form.html, order_new.html).

   The new rows open where the user is: no round trip to the server, the page stays where it is, the
   rows fade in and the first new field takes the focus. What the user typed stays. Without this
   script the buttons send the form, which comes back with the room added and nothing saved; the
   page then jumps to the first new row (#more-new) instead of starting at the top.

   Markup (the same on every page):
     data-more-group="<counter>"  a part that has rows. <counter> is the name of its hidden input
                                  with the number of rows, which the server reads when saving: this
                                  script keeps it equal to the rows. Also data-more-step (rows per
                                  click), data-more-max, and data-more-key ("i" if left out, "j" for
                                  a part inside a row of another part).
     data-more-row                a row of the part (a direct row; rows of a part inside it are not).
     <template data-more-template>  one blank row; {i} is its number counted from 0, {i1} from 1
                                  ({j}, {j1} for key "j"). A new row of an outer part may hold a
                                  group of its own with the template for the inner rows.
     data-more-add                the button: copies the template of its group.
     data-more-reveal             a place for something the form was shown without (choice parts 2):
                                  its template holds the part, its button has data-more-show.

   The handlers sit on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. Rows that were added change no field, so the form is marked with
   data-changed: live.js then does not swap the user's work away. After adding, the group sends a
   "more:added" event, which the form scripts (set_form.js, assembled_form.js) use to apply their
   rules to the new rows. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function closest(node, selector) {
    return node && node.closest ? node.closest(selector) : null;
  }

  // The rows of this part only: a row's own group has rows of its own, which are not counted here.
  function rowsOf(group) {
    return Array.prototype.filter.call(group.querySelectorAll("[data-more-row]"), function (row) {
      return closest(row.parentNode, "[data-more-group]") === group;
    });
  }

  function templateOf(host) {
    for (var node = host.firstElementChild; node; node = node.nextElementSibling) {
      if (node.tagName === "TEMPLATE" && node.hasAttribute("data-more-template")) {
        return node;
      }
    }
    return null;
  }

  function counterOf(group) {
    var name = group.getAttribute("data-more-group");
    var found = null;
    each(group.querySelectorAll('input[type="hidden"]'), function (input) {
      if (!found && input.name === name) {
        found = input;
      }
    });
    return found;
  }

  function number(group, attribute, fallback) {
    var value = parseInt(group.getAttribute(attribute), 10);
    return value > 0 ? value : fallback;
  }

  function put(text, key, index) {
    return text.split("{" + key + "}").join(String(index)).split("{" + key + "1}").join(String(index + 1));
  }

  // The tokens are put in the copy, in attributes and in text, but not in what an <option> shows: an
  // article may be called "Rail {i}". {i} and {i1} of an outer part are filled in the templates inside
  // it too (their content is a tree of its own); a template of an inner part keeps its {j}.
  function fill(node, key, index) {
    if (node.nodeType === 3) {
      node.nodeValue = put(node.nodeValue, key, index);
      return;
    }
    if (node.nodeType !== 1 || node.tagName === "OPTION") {
      return;
    }
    each(node.attributes, function (attribute) {
      attribute.value = put(attribute.value, key, index);
    });
    each(node.tagName === "TEMPLATE" ? node.content.childNodes : node.childNodes, function (child) {
      fill(child, key, index);
    });
  }

  // A copy of what the template holds, with the number of the new row put in.
  function markup(template, key, index) {
    var copy = document.importNode(template.content, true);
    if (key) {
      each(copy.childNodes, function (node) {
        fill(node, key, index);
      });
    }
    return copy;
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function firstControl(node) {
    return node.querySelector("input:not([type='hidden']):not([disabled]), select:not([disabled]), textarea:not([disabled])");
  }

  // The first new field takes the focus. Not before the script of the number fields (spinbox.js) has
  // put its arrows around the new fields: that moves a field in the page, and a field that is moved
  // loses the focus.
  function show(nodes) {
    each(nodes, function (node) {
      node.classList.add("more-added");
      node.addEventListener("animationend", function () {
        node.classList.remove("more-added");
      });
    });
    window.setTimeout(function () {
      var field = firstControl(nodes[0]);
      if (field) {
        field.focus({ preventScroll: true });
      }
      bringIntoView(nodes[0]);
    }, 0);
  }

  // The page stays where it is. It only scrolls, as little as possible, when the first new row is
  // not in view (the button of a part is at the bottom of the window, a row is taller than the room).
  function bringIntoView(node) {
    var margin = 16;
    var box = node.getBoundingClientRect();
    var by = 0;
    if (box.bottom + margin > window.innerHeight) {
      by = Math.min(box.bottom + margin - window.innerHeight, box.top - margin);
    } else if (box.top < margin) {
      by = box.top - margin;
    }
    if (by !== 0 && window.scrollBy) {
      window.scrollBy({ top: by, behavior: reducedMotion() ? "auto" : "smooth" });
    }
  }

  function markChanged(node) {
    var form = closest(node, "form");
    if (form) {
      form.setAttribute("data-changed", "");
    }
  }

  // No more rows than the server reads back: the button of a full part is disabled.
  function refreshButton(group) {
    if (!templateOf(group)) {
      return;
    }
    each(group.querySelectorAll("[data-more-add]"), function (button) {
      if (closest(button, "[data-more-group]") === group) {
        button.disabled = rowsOf(group).length >= number(group, "data-more-max", 200);
      }
    });
  }

  function addRows(button) {
    var group = closest(button, "[data-more-group]");
    var template = group && templateOf(group);
    if (!template) {
      return false; // nothing to copy: the button sends the form, as without the script
    }
    var key = group.getAttribute("data-more-key") || "i";
    var count = rowsOf(group).length;
    var room = number(group, "data-more-max", 200) - count;
    var added = [];
    for (var k = 0; k < Math.min(number(group, "data-more-step", 1), room); k += 1) {
      var fragment = markup(template, key, count + k);
      each(fragment.children, function (node) {
        added.push(node);
      });
      template.parentNode.insertBefore(fragment, template);
    }
    if (!added.length) {
      return true; // the part is full
    }
    var counter = counterOf(group);
    if (counter) {
      counter.value = String(rowsOf(group).length);
    }
    refreshButton(group);
    markChanged(group);
    show(added);
    group.dispatchEvent(new CustomEvent("more:added", { bubbles: true, detail: { rows: added } }));
    return true;
  }

  function reveal(button) {
    var host = closest(button, "[data-more-reveal]");
    var template = host && templateOf(host);
    if (!template) {
      return false;
    }
    var added = [];
    each(markup(template, "", 0).children, function (node) {
      added.push(node);
    });
    each(added, function (node) {
      host.parentNode.insertBefore(node, host);
    });
    host.remove(); // the button has done its work
    markChanged(added[0]);
    show(added);
    added[0].dispatchEvent(new CustomEvent("more:added", { bubbles: true, detail: { rows: added } }));
    each(added, refreshAll);
    return true;
  }

  // The parts of a node that came in with its template: the node itself and the parts inside it.
  function refreshAll(node) {
    if (node.hasAttribute("data-more-group")) {
      refreshButton(node);
    }
    each(node.querySelectorAll("[data-more-group]"), refreshButton);
  }

  document.addEventListener("click", function (event) {
    var add = closest(event.target, "[data-more-add]");
    var open = closest(event.target, "[data-more-show]");
    if (!add && !open) {
      return;
    }
    if (add ? addRows(add) : reveal(open)) {
      event.preventDefault(); // no round trip to the server
    }
  });

  each(document.querySelectorAll("[data-more-group]"), refreshButton);

  // A form the live channel swaps in (live.js) comes from the server with its button on, also when the
  // part is full (p392): the button is set again for every part that comes in, as the number fields
  // (spinbox.js) are. Replaced: setting it once at load only, which left the button of a swapped
  // full part on, a click that did nothing. Tests: tests/web/test_form_rows_scripts.py.
  if (window.MutationObserver && document.body) {
    new window.MutationObserver(function (records) {
      records.forEach(function (record) {
        each(record.addedNodes, function (node) {
          if (node.nodeType === 1) {
            refreshAll(node);
          }
        });
      });
    }).observe(document.body, { childList: true, subtree: true });
  }
})();
