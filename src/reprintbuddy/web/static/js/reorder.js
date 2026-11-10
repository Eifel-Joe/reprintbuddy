/* The reorder page (Spec 9.3, 9.4), templates/reorder.html.

   A part with several suppliers is marked "ordered" with the supplier chosen in its tile. When
   the choice changes, the packs field takes that supplier's suggested quantity (the option's
   data-packs), as long as nobody has typed in it: a typed number is never overwritten. Without
   this script the field keeps the default's quantity; every supplier's quantity is on the tile.

   The handlers sit on the document, so a tile swapped in by the live channel (live.js) works
   without binding anything again. */
(function () {
  "use strict";

  document.addEventListener("input", function (event) {
    var target = event.target;
    if (target.name === "packs" && target.closest && target.closest("form.reorder-mark")) {
      target.setAttribute("data-typed", "");
    }
  });

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select.hasAttribute || !select.hasAttribute("data-reorder-supplier")) {
      return;
    }
    var form = select.closest("form");
    var packs = form ? form.querySelector("input[name='packs']") : null;
    var option = select.options[select.selectedIndex];
    if (!packs || !option || packs.hasAttribute("data-typed")) {
      return;
    }
    packs.value = option.getAttribute("data-packs") || packs.value;
  });
})();
