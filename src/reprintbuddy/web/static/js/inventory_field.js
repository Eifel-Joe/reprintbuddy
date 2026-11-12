/* The inventory on the bookings page (p362, Spec 6.1): "awaiting assembly" exists only for print
   articles. When an article made of parts is chosen (its option carries data-stock-only), that value
   is hidden and "stock" is chosen; back on a print article it shows again. Without this script the
   server refuses the count with its message, as before. One listener on the document, so a region
   fetched again by live.js keeps working. No texts in here. */
(function () {
  "use strict";

  var ARTICLE = 'form[action="/bookings/inventory"] select[name="article_id"]';
  var AWAITING = "awaiting_assembly";

  function apply(articles, field) {
    var chosen = articles.selectedIndex >= 0 ? articles.options[articles.selectedIndex] : null;
    var stockOnly = Boolean(chosen && chosen.hasAttribute("data-stock-only"));
    for (var i = 0; i < field.options.length; i += 1) {
      if (field.options[i].value === AWAITING) {
        field.options[i].hidden = stockOnly;
        field.options[i].disabled = stockOnly;
      }
    }
    if (stockOnly && field.value === AWAITING) {
      field.value = "stock";
    }
  }

  var api = { apply: apply };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  function fieldOf(articles) {
    return articles.form ? articles.form.querySelector('select[name="field"]') : null;
  }

  function sweep() {
    var all = document.querySelectorAll(ARTICLE);
    for (var i = 0; i < all.length; i += 1) {
      var field = fieldOf(all[i]);
      if (field) {
        apply(all[i], field);
      }
    }
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target && target.matches && target.matches(ARTICLE)) {
      var field = fieldOf(target);
      if (field) {
        apply(target, field);
      }
    }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", sweep);
  } else {
    sweep();
  }
})();
