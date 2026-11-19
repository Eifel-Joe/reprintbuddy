/* Keeps the place on the page after a form (p645, Spec 10.8). No texts and no addresses in here.

   Root: almost every action is a POST that answers with a redirect to the page the person was on
   (Post/Redirect/Get). The browser treats the target of the redirect as a new navigation and starts
   it at the top, so after a switch or a saved form on a long list the place is lost.

   Fix: when a form is sent, the position of the page is put into the storage of this tab; the page
   that comes back reads it once and sets it again. One script for every form (the `submit` event of
   the document also covers the forms that live.js swaps in) instead of an anchor in each of about
   seventy forms or a rebuild of every form to send by fetch (both were considered and dropped:
   v121-scroll plan, 2.2).

   When the position is NOT used (the entry is read and removed on every page load, so it is used at
   most once): an address with an anchor names its own place; another page or another tab of the
   settings starts at the top (a new article leads to its own page); a page that answers the POST
   itself (data-live-src is empty) is not scrolled to the old place, it shows an error; a reload or a
   step back is restored by the browser; a visit that was no redirect (a link to the same page, a POST
   that only downloaded a file) is not the answer of the form; an entry older than MAX_AGE_MS or a
   damaged one is not trusted.

   After the position is set, a message of trouble (the message box, a field error) is scrolled into
   view, as far as needed and no further ("nearest": nothing if it is in view already). A page that
   answers the POST itself does the same for the error it shows next to the form.

   Care taken:
   - Attributes, never properties: a field called "target" (the target quantity of the article list)
     is `form.target` in the browser, and `form.method` or `form.action` can be hidden the same way.
   - The page head is the landmark: a message that came in above it (or one that is gone) moves the
     whole page; the position is corrected by how far the head moved. At the very top nothing is added,
     so the message stays in view.
   - One more try after `load` (images make the page too short at first) only when the person has not
     scrolled in between.
   - An error in a layout that is hidden (the list draws table and tiles, one is hidden) is skipped.

   Without this script, or with a storage that refuses, the page starts at the top as it did. */
(function () {
  "use strict";

  var KEY = "rb_scroll";
  var MAX_AGE_MS = 15000;
  var TROUBLE = "[data-flashes] .alert, .field-error";
  // On a page that answers the POST itself the refusal is written into the page, next to the form.
  var TROUBLE_AT_THE_FORM = TROUBLE + ', main .alert[role="alert"]';
  var HEAD = "main .page-head";

  function store() {
    try {
      return window.sessionStorage;
    } catch (error) {
      return null; // blocked storage may throw on the access itself
    }
  }

  function tabOf(search) {
    var found = /[?&]tab=([^&#]*)/.exec(search || "");
    return found ? found[1] : "";
  }

  function attribute(element, name) {
    var value = element && element.getAttribute ? element.getAttribute(name) : null;
    return value === null || value === undefined ? "" : String(value);
  }

  // Document coordinate of the page head; null on a page without one.
  function headTop() {
    var head = document.querySelector(HEAD);
    if (!head || !head.getBoundingClientRect) {
      return null;
    }
    var top = head.getBoundingClientRect().top + window.scrollY;
    return isFinite(top) ? top : null;
  }

  // --- remember when a form is sent ------------------------------------------------------------

  function sendsPost(form, submitter) {
    var method = attribute(submitter, "formmethod") || attribute(form, "method") || "get";
    return method.toLowerCase() === "post";
  }

  function leavesThisTab(form, submitter) {
    var target = (attribute(submitter, "formtarget") || attribute(form, "target")).toLowerCase();
    return target !== "" && target !== "_self";
  }

  document.addEventListener("submit", function (event) {
    // Other scripts of the page run first (they sit on the form); one that cancelled the event
    // sends nothing, so there is nothing to come back to.
    if (event.defaultPrevented) {
      return;
    }
    var form = event.target;
    if (!form || !sendsPost(form, event.submitter) || leavesThisTab(form, event.submitter)) {
      return;
    }
    try {
      var storage = store();
      if (storage) {
        storage.setItem(
          KEY,
          JSON.stringify({
            path: window.location.pathname,
            tab: tabOf(window.location.search),
            y: window.scrollY,
            head: headTop(),
            at: Date.now()
          })
        );
      }
    } catch (error) {
      /* the page starts at the top, as before */
    }
  });

  // --- set it again on the page that comes back ------------------------------------------------

  function takeEntry() {
    var storage = store();
    if (!storage) {
      return null;
    }
    var raw = null;
    try {
      raw = storage.getItem(KEY);
      storage.removeItem(KEY); // used at most once, whatever comes of it
    } catch (error) {
      return null;
    }
    if (raw === null) {
      return null;
    }
    try {
      var entry = JSON.parse(raw);
      var head = entry && entry.head;
      if (
        entry && typeof entry === "object" &&
        typeof entry.path === "string" &&
        typeof entry.y === "number" && isFinite(entry.y) && entry.y >= 0 &&
        typeof entry.at === "number" && isFinite(entry.at) &&
        (head === undefined || head === null || (typeof head === "number" && isFinite(head)))
      ) {
        return {
          path: entry.path,
          tab: typeof entry.tab === "string" ? entry.tab : "",
          y: entry.y,
          head: typeof head === "number" ? head : null,
          at: entry.at
        };
      }
    } catch (error) {
      /* damaged: dropped */
    }
    return null;
  }

  // How this page was reached. The browser puts a reload and a step back where they were; only a
  // navigation is ours, and only one that came through a redirect is the answer of a form (a link
  // to the same page has none). Without the numbers the page is taken as the answer.
  function arrival() {
    try {
      var entries = window.performance.getEntriesByType("navigation");
      var first = entries && entries[0];
      if (first) {
        return { type: first.type, redirected: typeof first.redirectCount === "number" ? first.redirectCount > 0 : true };
      }
    } catch (error) {
      /* the numbers are not there */
    }
    return { type: "navigate", redirected: true };
  }

  function run() {
    var entry = takeEntry();
    var location = window.location;
    var how = arrival();
    if (location.hash || how.type !== "navigate") {
      return;
    }
    var app = document.getElementById("app");
    if (!app) {
      return;
    }
    if (!app.getAttribute("data-live-src")) {
      // The page answers the POST itself (a refusal with what was typed): it starts at the top,
      // the error must still be seen.
      reveal(TROUBLE_AT_THE_FORM);
      return;
    }
    if (
      entry === null ||
      !how.redirected ||
      Date.now() - entry.at > MAX_AGE_MS ||
      entry.path !== location.pathname ||
      entry.tab !== tabOf(location.search)
    ) {
      return;
    }
    var y = entry.y;
    if (y > 0 && entry.head !== null) {
      var head = headTop();
      if (head !== null) {
        y = Math.max(0, y + (head - entry.head)); // a message above the head moved the page
      }
    }
    window.scrollTo(0, y);
    var placed = window.scrollY;
    // Images that are not loaded yet leave the page too short for the browser to go that far.
    if (Math.abs(placed - y) > 1) {
      window.addEventListener("load", function () {
        if (Math.abs(window.scrollY - placed) <= 1) {
          window.scrollTo(0, y); // nobody scrolled in between
          reveal(TROUBLE);
        }
      });
    }
    reveal(TROUBLE);
  }

  // Trouble stays in view: a refused value at its field, a warning at the top. The first one that is
  // drawn (a hidden layout has no boxes, and scrolling to it would do nothing).
  function reveal(selector) {
    var found = document.querySelectorAll(selector);
    for (var i = 0; i < found.length; i++) {
      var element = found[i];
      if (element.getClientRects && element.getClientRects().length > 0 && element.scrollIntoView) {
        element.scrollIntoView({ block: "nearest" });
        return;
      }
    }
  }

  try {
    run();
  } catch (error) {
    /* never in the way of the page */
  }
})();
