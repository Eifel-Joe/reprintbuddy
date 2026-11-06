/* Fold and unfold the menu at once. Without this file the button is a link that sets the cookie
   on the server and comes back to the page; this only saves the round trip. No texts in here:
   they come from the button. */
(function () {
  "use strict";

  var app = document.getElementById("app");
  var button = document.getElementById("sidebar-toggle");
  if (!app || !button) {
    return;
  }

  function href(collapsed) {
    var here = location.pathname + location.search;
    return "/ui/sidebar?collapsed=" + (collapsed ? "1" : "0") + "&next=" + encodeURIComponent(here);
  }

  button.addEventListener("click", function (event) {
    event.preventDefault();
    var collapsed = !app.classList.contains("sidebar-collapsed");
    app.classList.toggle("sidebar-collapsed", collapsed);
    try {
      document.cookie = "rb_sidebar=" + (collapsed ? "collapsed" : "") + "; path=/; max-age=" +
        (collapsed ? 31536000 : 0) + "; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
    } catch (e) {
      /* cookies blocked: the fold lasts until the page is left */
    }
    var label = button.getAttribute(collapsed ? "data-label-expand" : "data-label-collapse");
    button.setAttribute("title", label);
    button.setAttribute("aria-label", label);
    button.setAttribute("href", href(!collapsed));
  });
})();
