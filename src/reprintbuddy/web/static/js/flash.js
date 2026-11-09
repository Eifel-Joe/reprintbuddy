/* Messages at the top of a page (p161): good news goes away by itself after a few seconds,
   trouble (a warning, an error) stays until it is dismissed with its button. The messages stand in
   a box marked `data-flashes`; the label of the button is `data-label-close` of that box, so there
   are no texts in here. Without this file every message stays, as it did before.

   Care taken:
   - The message is in the page from the start (role "status" or "alert"), so a screen reader says
     it when the page is read; the clock only runs while the page is in view (a tab in the
     background does not use it up) and stands still while the pointer or the focus is on the message.
   - With "reduce motion" the message is removed at once, without fading. */
(function () {
  "use strict";

  var DELAY_MS = 8000; // good news: how long it stays
  var FADE_MS = 250; // the fade at the end (css: .flash-leaving), only without "reduce motion"

  var boxes = document.querySelectorAll("[data-flashes]");
  if (!boxes || boxes.length === 0) {
    return;
  }

  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function remove(element) {
    if (element.parentNode) {
      element.parentNode.removeChild(element);
    }
  }

  function goAway(element) {
    if (reducedMotion()) {
      remove(element);
      return;
    }
    element.classList.add("flash-leaving");
    setTimeout(function () {
      remove(element);
    }, FADE_MS);
  }

  // Good news: a clock that runs only while the page is in view and nothing holds the message.
  function autoClose(element) {
    var remaining = DELAY_MS;
    var startedAt = 0;
    var timer = null;
    var held = 0; // pointer and focus can both hold it

    function run() {
      if (timer !== null || held > 0 || document.hidden) {
        return;
      }
      startedAt = Date.now();
      timer = setTimeout(function () {
        timer = null;
        goAway(element);
      }, remaining);
    }

    function stop() {
      if (timer === null) {
        return;
      }
      clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (Date.now() - startedAt));
    }

    function hold() {
      held += 1;
      stop();
    }

    function release() {
      held = Math.max(0, held - 1);
      run();
    }

    element.addEventListener("mouseenter", hold);
    element.addEventListener("mouseleave", release);
    element.addEventListener("focusin", hold);
    element.addEventListener("focusout", release);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stop();
      } else {
        run();
      }
    });
    run();
  }

  // Trouble: stays, with a button that takes it away.
  function dismissible(element, label) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "flash-close";
    button.setAttribute("aria-label", label);
    button.textContent = "×";
    button.addEventListener("click", function () {
      remove(element);
    });
    element.classList.add("has-close");
    element.appendChild(button);
  }

  Array.prototype.forEach.call(boxes, function (box) {
    var label = box.getAttribute("data-label-close") || "";
    Array.prototype.slice.call(box.children).forEach(function (message) {
      if (message.classList.contains("flash")) {
        autoClose(message);
      } else if (message.classList.contains("alert")) {
        dismissible(message, label);
      }
    });
  });
})();
