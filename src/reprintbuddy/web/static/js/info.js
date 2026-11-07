/* Info buttons: close an open explanation with a tap outside or Escape, keep only one open, and
   put it under its button. Without this file the buttons still work (a <details>: the button opens
   and closes it, the explanation sits under its field). No texts in here: they come from the page. */
(function () {
  "use strict";

  var SELECTOR = "details.info";
  /* The explanation starts a little left of the button's symbol, so its edge is not under it. */
  var EDGE = 12;
  var MARGIN = 8;

  /* Where the explanation starts, in px from the left edge of the row that frames it (its field):
     under the button, but never left of the field, never right of it (when it fits, otherwise at
     the field's start), and never outside the screen. A pure function of numbers, so a test can
     run it without a browser. */
  function place(buttonLeft, frameLeft, frameWidth, popupWidth, viewportWidth) {
    var x = buttonLeft - frameLeft - EDGE;
    x = Math.min(x, frameWidth - popupWidth);
    x = Math.max(x, 0);
    if (frameLeft + x + popupWidth > viewportWidth - MARGIN) {
      x = viewportWidth - MARGIN - popupWidth - frameLeft;
    }
    if (frameLeft + x < MARGIN) {
      x = MARGIN - frameLeft;
    }
    return x;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { place: place };
  }
  if (typeof document === "undefined") {
    return;
  }

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

  /* Measure the open explanation and set --info-x on its <details>. */
  function position(item) {
    var popup = item.querySelector(".info-pop");
    var icon = item.querySelector("summary .icon") || item.querySelector("summary");
    var frame = popup ? popup.offsetParent : null;
    if (!popup || !icon || !frame) {
      return;
    }
    item.style.setProperty("--info-x", "0px");
    var frameBox = frame.getBoundingClientRect();
    var x = place(
      icon.getBoundingClientRect().left,
      frameBox.left,
      frameBox.width,
      popup.getBoundingClientRect().width,
      document.documentElement.clientWidth
    );
    item.style.setProperty("--info-x", Math.round(x) + "px");
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
        position(item);
      }
    },
    true
  );

  /* A window that changes size (turning a phone) moves the fields: place the open one again. */
  window.addEventListener("resize", function () {
    open().forEach(position);
  });
})();
