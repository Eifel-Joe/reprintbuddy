/* Preview of the label in the settings: when a detail is ticked or unticked, the sample label is drawn
   again by the server with those details (the picture is made there, with the real font and the real
   fitting rules) and a note appears if a line had to be cut. Without this file the page still works:
   it shows the preview of the saved choice and the trial print uses what is ticked. */
(function () {
  "use strict";

  var image = document.getElementById("label-preview");
  var note = document.getElementById("label-cut");
  if (!image) {
    return;
  }
  var timer = null;
  var current = null;

  function ticked() {
    var boxes = document.querySelectorAll('input[name="label_field"]:checked');
    return Array.prototype.map.call(boxes, function (box) { return box.value; }).join(",");
  }

  function update() {
    var url = "/settings/labels/preview.png?fields=" + encodeURIComponent(ticked());
    fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) {
        throw new Error("preview");
      }
      var cut = response.headers.get("X-Label-Cut");
      return response.blob().then(function (blob) {
        if (current) {
          URL.revokeObjectURL(current);
        }
        current = URL.createObjectURL(blob);
        image.src = current;
        if (note) {
          note.hidden = !cut;
        }
      });
    }).catch(function () { /* keep the picture that is there */ });
  }

  document.addEventListener("change", function (event) {
    if (event.target && event.target.name === "label_field") {
      window.clearTimeout(timer);
      timer = window.setTimeout(update, 60);
    }
  });
})();
