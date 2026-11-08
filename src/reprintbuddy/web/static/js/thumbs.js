/* A preview picture that cannot be loaded disappears, so the placeholder behind it shows (file
   choice, p78: this used to be an onerror attribute in the template, an inline script). The error
   event does not bubble, so one listener on the document catches it on the way down. Pictures that
   failed before this file ran are looked at once, lazy ones too (v0.3.9 p120: a lazy picture in sight
   can fail before the script runs, and its error event is gone by then). A picture that has not
   started to load (a lazy one far below) is not complete and has no currentSrc: it is left alone, its
   own error event comes later. No texts in here. */
(function () {
  "use strict";

  var SELECTOR = "img[data-remove-on-error]";

  document.addEventListener(
    "error",
    function (event) {
      var target = event.target;
      if (target && target.matches && target.matches(SELECTOR)) {
        target.remove();
      }
    },
    true
  );

  function sweep() {
    var pictures = document.querySelectorAll(SELECTOR);
    for (var i = 0; i < pictures.length; i += 1) {
      // Failed: finished loading without a picture. Not started (lazy, still waiting): complete is false, no currentSrc.
      if (pictures[i].complete && pictures[i].naturalWidth === 0 && pictures[i].currentSrc) {
        pictures[i].remove();
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", sweep);
  } else {
    sweep();
  }
})();
