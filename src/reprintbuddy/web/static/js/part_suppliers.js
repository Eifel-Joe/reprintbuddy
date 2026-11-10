/* The suppliers of a purchased part (Spec 5.3), templates/part_form.html.

   Adds and removes supplier blocks in place and numbers them again: the title "Supplier n", the
   ids the labels point to, the value of the "default" choice and of the remove button. Without
   this script the buttons send the form, which comes back with the block added or removed and
   nothing saved; the page stays usable either way. As on the server, the last block cannot be
   removed, no block is added beyond the limit, and removing the default leaves none chosen.

   The handlers sit on the document, so a form swapped in by the live channel (live.js) works
   without binding anything again. A removed block changes no field, so the form is marked with
   data-changed: live.js then treats it as changed and does not swap the user's work away. */
(function () {
  "use strict";

  function each(list, fn) {
    Array.prototype.forEach.call(list, fn);
  }

  function sectionOf(node) {
    return node && node.closest ? node.closest("[data-suppliers]") : null;
  }

  function blocks(section) {
    return section.querySelectorAll("[data-supplier-rows] [data-supplier]");
  }

  function renumber(section) {
    var all = blocks(section);
    var title = section.getAttribute("data-title") || "{n}";
    var max = parseInt(section.getAttribute("data-max"), 10) || 10;
    each(all, function (block, i) {
      var heading = block.querySelector("[data-supplier-title]");
      if (heading) {
        heading.textContent = title.replace("{n}", String(i + 1));
      }
      each(block.querySelectorAll("[id^='supplier-']"), function (field) {
        field.id = field.id.replace(/^supplier-[^-]+-/, "supplier-" + i + "-");
      });
      each(block.querySelectorAll("label[for^='supplier-']"), function (label) {
        label.htmlFor = label.htmlFor.replace(/^supplier-[^-]+-/, "supplier-" + i + "-");
      });
      var radio = block.querySelector("input[name='supplier_default']");
      if (radio) {
        radio.value = String(i);
      }
      var remove = block.querySelector("[data-supplier-remove]");
      if (remove) {
        remove.value = String(i);
        remove.disabled = all.length <= 1;
      }
    });
    var add = section.querySelector("[data-suppliers-add]");
    if (add) {
      add.disabled = all.length >= max;
    }
  }

  function markChanged(section) {
    var form = section.closest("form");
    if (form) {
      form.setAttribute("data-changed", "");
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    var add = target.closest ? target.closest("[data-suppliers-add]") : null;
    var remove = target.closest ? target.closest("[data-supplier-remove]") : null;
    var section = sectionOf(add || remove);
    if (!section) {
      return;
    }
    event.preventDefault(); // no round trip to the server
    if (add) {
      var template = section.querySelector("[data-supplier-template]");
      var block = template.content.querySelector("[data-supplier]").cloneNode(true);
      section.querySelector("[data-supplier-rows]").appendChild(block);
      renumber(section);
      var first = block.querySelector("input[name='supplier']");
      if (first) {
        first.focus();
      }
    } else if (blocks(section).length > 1) {
      remove.closest("[data-supplier]").remove();
      renumber(section);
      section.querySelector("[data-suppliers-add]").focus();
    }
    markChanged(section);
  });

  each(document.querySelectorAll("[data-suppliers]"), renumber);
})();
