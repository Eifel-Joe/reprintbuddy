/* Preview of the label (settings tab "Labels" and the card "Label" on an article's page). The picture is
   made by the server (with the real font and the real fitting rules); its answer says what the label
   cannot show right, and this script shows the notes for that: a line cut off, a character the font
   does not have, right-to-left letters, a code too fine for phones (header X-Label-Notes).
   In the settings, when a detail is ticked or unticked the sample label is drawn again with those
   details. On the article's page the picture is there already; only its notes are asked for.
   Without this file everything still works: the settings show the preview of the saved choice, the
   trial print uses what is ticked, the article's page shows its picture without notes. */
(function () {
  "use strict";

  var image = document.getElementById("label-preview");
  var notes = document.getElementById("label-notes");
  if (!image) {
    return;
  }
  var fixedUrl = image.getAttribute("data-label-url");  /* the article's page: no ticks to follow */
  var timer = null;
  var current = null;

  function ticked() {
    var boxes = document.querySelectorAll('input[name="label_field"]:checked');
    return Array.prototype.map.call(boxes, function (box) { return box.value; }).join(",");
  }

  function showNotes(header) {
    if (!notes) {
      return;
    }
    var named = (header || "").split(",");
    Array.prototype.forEach.call(notes.querySelectorAll("[data-note]"), function (note) {
      note.hidden = named.indexOf(note.getAttribute("data-note")) === -1;
    });
  }

  function update() {
    var url = fixedUrl || "/settings/labels/preview.png?fields=" + encodeURIComponent(ticked());
    fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) {
        throw new Error("preview");
      }
      var header = response.headers.get("X-Label-Notes");
      if (fixedUrl) {
        showNotes(header);
        return null;
      }
      return response.blob().then(function (blob) {
        if (current) {
          URL.revokeObjectURL(current);
        }
        current = URL.createObjectURL(blob);
        image.src = current;
        showNotes(header);
      });
    }).catch(function () { /* keep the picture and the notes that are there */ });
  }

  update();  /* the page draws nothing itself: ask for the picture (settings) and for the notes */

  document.addEventListener("change", function (event) {
    if (!fixedUrl && event.target && event.target.name === "label_field") {
      window.clearTimeout(timer);
      timer = window.setTimeout(update, 60);
    }
  });
})();
