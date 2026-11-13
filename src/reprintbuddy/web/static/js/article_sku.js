/* The article number of an article is a required field (Spec 5.2), unless the article is a part
   only ("Sold alone" off). The page renders the field as required or not; this follows the switch
   while the form is open. The handler sits on the document, so a form swapped in by the live
   channel (live.js) works without binding anything again. Without the script the server still
   refuses a missing number and says so. */
(function () {
  "use strict";

  document.addEventListener("change", function (event) {
    var switchBox = event.target;
    if (!switchBox || switchBox.name !== "sold_alone" || !switchBox.form) {
      return;
    }
    var number = switchBox.form.querySelector("input[name='sku']");
    if (number) {
      number.required = switchBox.checked;
    }
  });
})();
