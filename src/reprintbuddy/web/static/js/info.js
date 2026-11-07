/* Info buttons: close an open explanation with a tap outside or Escape, and keep only one open.
   Without this file the buttons still work (a <details>: the button opens and closes it). No
   texts in here: they come from the page. */
(function () {
  "use strict";

  var SELECTOR = "details.info";

  function open() {
    return Array.prototype.slice.call(document.querySelectorAll(SELECTOR + "[open]"));
  }

  function closeAll(except) {
    open().forEach(function (item) {
      if (item !== except) {
        item.removeAttribute("open");
      }
    });
  }

  document.addEventListener("click", function (event) {
    var inside = event.target.closest ? event.target.closest(SELECTOR) : null;
    closeAll(inside);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    var shown = open();
    if (shown.length === 0) {
      return;
    }
    var summary = shown[0].querySelector("summary");
    closeAll(null);
    if (summary && shown.some(function (item) { return item.contains(document.activeElement); })) {
      summary.focus();
    }
  });

  /* "toggle" does not bubble: listen in the capture phase for the one that has just opened. */
  document.addEventListener(
    "toggle",
    function (event) {
      var item = event.target;
      if (item && item.matches && item.matches(SELECTOR) && item.open) {
        closeAll(item);
      }
    },
    true
  );
})();
