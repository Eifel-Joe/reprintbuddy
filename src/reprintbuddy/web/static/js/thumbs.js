/* A preview picture that cannot be loaded disappears, so the placeholder behind it shows (file
   choice, p78: this used to be an onerror attribute in the template, an inline script). The error
   event does not bubble, so one listener on the document catches it on the way down. Pictures that
   failed before this file ran are looked at once. No texts in here. */
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
      // A picture that waits to be loaded later (loading="lazy") has not failed; its error event comes by itself.
      if (pictures[i].getAttribute("loading") !== "lazy" && pictures[i].complete && pictures[i].naturalWidth === 0 && pictures[i].getAttribute("src")) {
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
