/* The zone of this browser for the server (Spec 10.5a). The server computes every page in the zone the
   cookie rb_zone names, without one in the zone of the server, and says which one it used in data-zone
   on <body>. This script writes the cookie on every page, the sign-in and setup pages too, so the first
   page with times is already right. When the page was computed in another zone than the browser's, it
   loads the page once more - not while a form of the page is changed (Spec 10.5.6), not on the answer
   to a form (data-form-answer: loading it again would send the form again), not while the page shows a
   message (a page after a saved form carries "Booked" once; loaded again it is gone, and somebody might
   book a second time), and not when the cookie does not hold (blocked cookies would load the page again
   and again). No texts in here.

   Root of the "once": the page is loaded again only when the cookie the request carried was not yet the
   browser's zone. A server that does not take the name (its list of zones does not have it) answers in
   its own zone again; the script then stays. Rejected: a counter in sessionStorage (blocked together
   with cookies in the same browsers, and one more thing to clear).
   Runs after live.js, which clears the mark of an automatic reload as soon as it runs. */
(function () {
  "use strict";

  var COOKIE = "rb_zone";
  // As live.js: set right before a reload by Reprintbuddy itself, which the server does not count as
  // opening the page (Spec 10.2); live.js on the next page clears it.
  var AUTO_RELOAD = "rb_auto_reload";
  // What IANA names are made of, as long as the server takes them: nothing that could end the cookie.
  var NAME = /^[A-Za-z0-9_+\-\/]{1,64}$/;

  function browserZone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (e) {
      return "";
    }
  }

  function cookie(name) {
    var parts = (document.cookie || "").split(";");
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].replace(/^\s+/, "");
      if (part.indexOf(name + "=") === 0) {
        return part.substring(name.length + 1);
      }
    }
    return null;
  }

  // "Changed" as in live.js: a field differs from the value the page came with, or a page script
  // marked the form with data-changed.
  function selectChanged(select) {
    var options = select.options;
    var i;
    if (select.multiple) {
      for (i = 0; i < options.length; i++) {
        if (options[i].selected !== options[i].defaultSelected) {
          return true;
        }
      }
      return false;
    }
    var start = -1;
    for (i = 0; i < options.length; i++) {
      if (options[i].defaultSelected) {
        start = i;
      }
    }
    if (start < 0) {
      for (i = 0; i < options.length; i++) {
        if (!options[i].disabled) {
          start = i;
          break;
        }
      }
    }
    return select.selectedIndex !== start;
  }

  function fieldChanged(field) {
    var tag = field.tagName;
    if (tag === "SELECT") {
      return selectChanged(field);
    }
    if (tag === "TEXTAREA") {
      return field.value !== field.defaultValue;
    }
    if (tag !== "INPUT") {
      return false;
    }
    var type = (field.type || "").toLowerCase();
    if (type === "hidden" || type === "submit" || type === "button" || type === "reset" || type === "image") {
      return false;
    }
    if (type === "checkbox" || type === "radio") {
      return field.checked !== field.defaultChecked;
    }
    return field.value !== field.defaultValue;
  }

  function anyFormChanged() {
    var forms = document.forms || [];
    for (var i = 0; i < forms.length; i++) {
      if (forms[i].hasAttribute("data-changed")) {
        return true;
      }
      for (var j = 0; j < forms[i].elements.length; j++) {
        if (fieldChanged(forms[i].elements[j])) {
          return true;
        }
      }
    }
    return false;
  }

  var body = document.body;
  var zone = browserZone();
  if (!body || !NAME.test(zone)) {
    return; // no zone to report: the pages stay in the zone of the server
  }
  var carried = cookie(COOKIE); // what the request for this page carried
  try {
    document.cookie = COOKIE + "=" + zone + "; path=/; max-age=31536000; SameSite=Lax" +
      (location.protocol === "https:" ? "; Secure" : "");
  } catch (e) {
    return;
  }
  if (cookie(COOKIE) !== zone || carried === zone) {
    return; // the cookie does not hold, or the server had this zone already: never a loop
  }
  if (body.getAttribute("data-zone") === zone || body.hasAttribute("data-form-answer") || anyFormChanged() ||
      document.querySelector("[data-flashes]")) {
    return;
  }
  // Only where live.js runs on the next page to clear the mark; after the sign-in page it would keep the
  // page that follows from counting as opened.
  if (document.getElementById("app")) {
    document.cookie = AUTO_RELOAD + "=1; max-age=30; path=/; samesite=strict";
  }
  window.location.reload();
})();
