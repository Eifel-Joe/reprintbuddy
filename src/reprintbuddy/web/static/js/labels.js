/* Preview of the label (settings tab "Labels" and the card "Label" on an article's page). The picture is
   made by the server (with the real font and the real fitting rules); its answer says what the label
   cannot show right, and this script shows the notes for that: a line cut off, a character the font
   does not have, right-to-left letters, a code too fine for phones, the last details left off because the height is full (header X-Label-Notes,
   the details left off in X-Label-Dropped).
   The picture is asked for once, here, and its notes are read from that same answer: the page has no
   <img src> of its own (that would have the server draw the label twice). In the settings, when a
   detail is ticked or unticked the sample label is drawn again with those details.
   Without this file everything still works: a <noscript> picture shows the saved choice (settings) or
   the label (article's page), the trial print uses what is ticked; only the notes are missing. */
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
  var latest = 0;

  function ticked() {
    var boxes = document.querySelectorAll('input[name="label_field"]:checked');
    return Array.prototype.map.call(boxes, function (box) { return box.value; }).join(",");
  }

  function showNotes(header, dropped) {
    if (!notes) {
      return;
    }
    var named = (header || "").split(",");
    Array.prototype.forEach.call(notes.querySelectorAll("[data-note]"), function (note) {
      note.hidden = named.indexOf(note.getAttribute("data-note")) === -1;
    });
    var list = notes.querySelector("[data-dropped]");
    if (list) {  /* the note "rows" names the details that were left off (header X-Label-Dropped) */
      list.textContent = (dropped || "").split(",").map(function (key) {
        var name = notes.querySelector('[data-field-name="' + key + '"]');
        return name ? name.textContent : "";
      }).filter(Boolean).join(", ");
    }
  }

  function update() {
    var url = fixedUrl || "/settings/labels/preview.png?fields=" + encodeURIComponent(ticked());
    var mine = ++latest;
    fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) {
        throw new Error("preview");
      }
      var header = response.headers.get("X-Label-Notes");
      var dropped = response.headers.get("X-Label-Dropped");
      return response.blob().then(function (blob) {
        if (mine !== latest) {  /* a newer tick has asked again: this answer is old */
          return;
        }
        if (current) {
          URL.revokeObjectURL(current);
        }
        current = URL.createObjectURL(blob);
        image.src = current;
        showNotes(header, dropped);
      });
    }).catch(function () {
      if (!current) {  /* no picture yet: let the browser try for itself (then without notes) */
        image.src = url;
      }
    });
  }

  update();  /* the page draws nothing itself: ask for the picture and read its notes */

  document.addEventListener("change", function (event) {
    if (!fixedUrl && event.target && event.target.name === "label_field") {
      window.clearTimeout(timer);
      timer = window.setTimeout(update, 60);
    }
  });
})();
