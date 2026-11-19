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
   itself (data-live-src is empty) shows an error that must be seen; a reload or a step back is
   restored by the browser; an entry older than MAX_AGE_MS or a damaged one is not trusted.
   After the position is set, a message of trouble (the message box, a field error) is scrolled into
   view, as far as needed and no further ("nearest": nothing if it is in view already).

   Without this script, or with a storage that refuses, the page starts at the top as it did. */
(function () {
  "use strict";

  var KEY = "rb_scroll";
  var MAX_AGE_MS = 15000;
  var TROUBLE = "[data-flashes] .alert, .field-error";

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

  // --- remember when a form is sent ------------------------------------------------------------

  function sendsPost(form, submitter) {
    var method = (submitter && submitter.formMethod) || form.method || "get";
    return String(method).toLowerCase() === "post";
  }

  function leavesThisTab(form, submitter) {
    var target = (submitter && submitter.formTarget) || form.target || "";
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
      if (
        entry && typeof entry === "object" &&
        typeof entry.path === "string" &&
        typeof entry.y === "number" && isFinite(entry.y) && entry.y >= 0 &&
        typeof entry.at === "number" && isFinite(entry.at)
      ) {
        return { path: entry.path, tab: typeof entry.tab === "string" ? entry.tab : "", y: entry.y, at: entry.at };
      }
    } catch (error) {
      /* damaged: dropped */
    }
    return null;
  }

  // The browser puts a reload and a step back where they were; only a navigation by a form (a
  // redirect lands here as "navigate") is ours. Without the API the page is taken as a new one.
  function navigationType() {
    try {
      var entries = window.performance.getEntriesByType("navigation");
      return entries && entries[0] ? entries[0].type : "navigate";
    } catch (error) {
      return "navigate";
    }
  }

  function answersPost() {
    var app = document.getElementById("app");
    return !app || !app.getAttribute("data-live-src"); // only a GET page names its own address
  }

  function restore() {
    var entry = takeEntry();
    if (entry === null) {
      return;
    }
    var location = window.location;
    if (
      location.hash ||
      answersPost() ||
      navigationType() !== "navigate" ||
      Date.now() - entry.at > MAX_AGE_MS ||
      entry.path !== location.pathname ||
      entry.tab !== tabOf(location.search)
    ) {
      return;
    }
    window.scrollTo(0, entry.y);
    // Images that are not loaded yet leave the page too short for the browser to go that far.
    if (Math.abs(window.scrollY - entry.y) > 1) {
      window.addEventListener("load", function () {
        window.scrollTo(0, entry.y);
        reveal();
      });
    }
    reveal();
  }

  // Trouble stays in view: a refused value at its field, a warning at the top.
  function reveal() {
    var trouble = document.querySelector(TROUBLE);
    if (trouble && trouble.scrollIntoView) {
      trouble.scrollIntoView({ block: "nearest" });
    }
  }

  try {
    restore();
  } catch (error) {
    /* never in the way of the page */
  }
})();
