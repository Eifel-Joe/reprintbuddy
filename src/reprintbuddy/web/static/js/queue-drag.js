/* The order of Reprintbuddy's own queue by dragging (Spec 7.2, Design 5). Own code, no library
   (main session, 06.11.2026); should it not work reliably on a phone, SortableJS replaces it, so
   everything about dragging lives in this one file.

   The grip of an entry (.drag-handle; `touch-action: none` in the style sheet) takes mouse and
   finger through Pointer Events; the rest of the page scrolls as usual. While dragging, the entry
   follows the pointer, a placeholder shows the gap and the region carries `data-live-busy`, so
   live.js does not swap it. On release one request "move X before Y" (or to the bottom); Escape
   or a cancelled pointer puts the entry back. When the server refuses, the entry goes back, the
   server's text is shown and the region is asked for anew. An open row menu keeps the region
   busy too, so a change from another device does not close it under the finger.

   No texts and no addresses in here: the address of each entry, the CSRF token and every text
   come from the page. */
(function () {
  "use strict";

  var MESSAGE_ID = "queue-drag-message";
  var MIN_MOVE_PX = 4; // a smaller movement is a click on the grip, not a move
  var EDGE_PX = 56; // this close to the top or bottom of the window the page scrolls along
  var SCROLL_STEP_PX = 12;
  var SCROLL_EVERY_MS = 16;

  var drag = null; // the entry being dragged, see start()
  var sending = []; // regions with a move on its way to the server

  // --- helpers -------------------------------------------------------------

  function regionOf(element) {
    return element ? element.closest("[data-live-region]") : null;
  }

  function isEntry(element) {
    return element !== null && element.classList.contains("queue-row");
  }

  function entriesOf(list) {
    return Array.prototype.filter.call(list.children, isEntry);
  }

  // The next entry after `node`, leaving out the entry being dragged.
  function nextEntry(node, skip) {
    var next = node.nextElementSibling;
    while (next !== null && (next === skip || !isEntry(next))) {
      next = next.nextElementSibling;
    }
    return next;
  }

  function renumber(list) {
    entriesOf(list).forEach(function (row, index) {
      var number = row.querySelector(".queue-pos");
      if (number) {
        number.textContent = String(index + 1);
      }
    });
  }

  function showMessage(text) {
    var message = document.getElementById(MESSAGE_ID);
    if (message) {
      message.textContent = text || "";
      message.hidden = !text;
    }
  }

  // --- busy marking (live.js leaves a region with data-live-busy alone) ------

  function updateBusy(region) {
    if (!region) {
      return;
    }
    var held =
      (drag !== null && drag.region === region) ||
      sending.indexOf(region) >= 0 ||
      region.querySelector("details.row-menu[open]") !== null;
    if (held) {
      region.setAttribute("data-live-busy", "");
    } else if (region.hasAttribute("data-live-busy")) {
      region.removeAttribute("data-live-busy");
      document.dispatchEvent(new CustomEvent("live-idle"));
    }
  }

  function refresh(region) {
    var name = region ? region.getAttribute("data-live-region") : null;
    if (name) {
      document.dispatchEvent(new CustomEvent("live-refresh", { detail: { regions: [name] } }));
    }
  }

  // --- dragging --------------------------------------------------------------

  function start(event, handle) {
    var row = handle.closest(".queue-row");
    var list = row ? row.parentElement : null;
    if (!list || !list.classList.contains("queue-list")) {
      return;
    }
    drag = {
      handle: handle,
      row: row,
      list: list,
      region: regionOf(list),
      pointerId: event.pointerId,
      startY: event.clientY,
      lastY: event.clientY,
      offsetY: 0,
      next: nextEntry(row, null), // where the entry goes back to
      placeholder: null,
      lifted: false,
      scrollStep: 0,
      scrollTimer: null
    };
    try {
      handle.setPointerCapture(event.pointerId);
    } catch (error) {
      // the pointer is already gone; pointerup or pointercancel ends it
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onCancel);
    handle.addEventListener("lostpointercapture", onCancel);
    document.addEventListener("keydown", onKey);
    updateBusy(drag.region);
  }

  // The entry leaves the list and follows the pointer; a placeholder of its size keeps the gap.
  function lift() {
    var row = drag.row;
    Array.prototype.forEach.call(row.querySelectorAll("details[open]"), function (details) {
      details.open = false;
    });
    var box = row.getBoundingClientRect();
    var placeholder = document.createElement("li");
    placeholder.className = "queue-placeholder";
    placeholder.setAttribute("aria-hidden", "true");
    placeholder.style.height = box.height + "px";
    drag.list.insertBefore(placeholder, row);
    drag.placeholder = placeholder;
    drag.offsetY = drag.startY - box.top;
    row.style.left = box.left + "px";
    row.style.width = box.width + "px";
    row.style.top = box.top + "px";
    row.classList.add("is-dragging");
    document.body.classList.add("queue-dragging");
    drag.lifted = true;
    showMessage("");
  }

  function follow() {
    var y = drag.lastY;
    drag.row.style.top = y - drag.offsetY + "px";
    var before = null;
    var others = entriesOf(drag.list);
    for (var i = 0; i < others.length; i++) {
      if (others[i] === drag.row) {
        continue;
      }
      var box = others[i].getBoundingClientRect();
      if (y < box.top + box.height / 2) {
        before = others[i];
        break;
      }
    }
    if (nextEntry(drag.placeholder, drag.row) !== before) {
      if (before) {
        drag.list.insertBefore(drag.placeholder, before);
      } else {
        drag.list.appendChild(drag.placeholder);
      }
    }
  }

  // Near the top or bottom edge of the window the page scrolls on, also while the finger rests.
  function autoScroll() {
    var y = drag.lastY;
    drag.scrollStep = 0;
    if (y < EDGE_PX) {
      drag.scrollStep = -SCROLL_STEP_PX;
    } else if (y > window.innerHeight - EDGE_PX) {
      drag.scrollStep = SCROLL_STEP_PX;
    }
    if (drag.scrollStep === 0) {
      stopScroll(drag);
    } else if (drag.scrollTimer === null) {
      drag.scrollTimer = window.setInterval(function () {
        if (drag === null || drag.scrollStep === 0) {
          return;
        }
        window.scrollBy(0, drag.scrollStep);
        follow();
      }, SCROLL_EVERY_MS);
    }
  }

  function stopScroll(state) {
    if (state.scrollTimer !== null) {
      window.clearInterval(state.scrollTimer);
      state.scrollTimer = null;
    }
  }

  function onMove(event) {
    if (drag === null || event.pointerId !== drag.pointerId) {
      return;
    }
    drag.lastY = event.clientY;
    if (!drag.lifted) {
      if (Math.abs(event.clientY - drag.startY) < MIN_MOVE_PX) {
        return;
      }
      lift();
    }
    event.preventDefault();
    follow();
    autoScroll();
  }

  function onUp(event) {
    if (drag !== null && event.pointerId === drag.pointerId) {
      finish(true);
    }
  }

  function onCancel(event) {
    if (drag !== null && event.pointerId === drag.pointerId) {
      finish(false);
    }
  }

  function onKey(event) {
    if (drag !== null && event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  }

  function finish(commit) {
    var state = drag;
    drag = null;
    stopScroll(state);
    var handle = state.handle;
    handle.removeEventListener("pointermove", onMove);
    handle.removeEventListener("pointerup", onUp);
    handle.removeEventListener("pointercancel", onCancel);
    handle.removeEventListener("lostpointercapture", onCancel);
    document.removeEventListener("keydown", onKey);
    try {
      if (handle.hasPointerCapture(state.pointerId)) {
        handle.releasePointerCapture(state.pointerId);
      }
    } catch (error) {
      // nothing to release
    }
    document.body.classList.remove("queue-dragging");
    if (!state.lifted) {
      updateBusy(state.region);
      return;
    }
    var row = state.row;
    row.classList.remove("is-dragging");
    row.style.left = "";
    row.style.width = "";
    row.style.top = "";
    if (commit) {
      state.list.insertBefore(row, state.placeholder);
    }
    state.placeholder.remove();
    var next = nextEntry(row, null);
    if (!commit || next === state.next) {
      updateBusy(state.region);
      return;
    }
    renumber(state.list);
    send(state, row, next);
  }

  // --- saving ----------------------------------------------------------------

  function send(state, row, next) {
    var body = new URLSearchParams();
    body.set("to", next ? "before:" + next.getAttribute("data-entry-id") : "bottom");
    sending.push(state.region);
    fetch(row.getAttribute("data-move-url"), {
      method: "POST",
      body: body,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "X-Requested-With": "fetch",
        "X-CSRF-Token": state.list.getAttribute("data-csrf") || ""
      }
    })
      .then(function (response) {
        if (response.status === 204) {
          return null;
        }
        // 400 and 409 carry a short text; anything else (signed out, an error page) does not.
        var spoken = response.status === 400 || response.status === 409;
        return response.text().then(function (text) {
          fail(state, row, spoken ? text : "");
        });
      })
      .catch(function () {
        fail(state, row, "");
      })
      .then(function () {
        sending.splice(sending.indexOf(state.region), 1);
        updateBusy(state.region);
      });
  }

  // Refused or no answer: the entry goes back to where it was, then the region is fetched anew
  // (another device may have changed the queue; live.js fetches once the region is free).
  function fail(state, row, text) {
    if (row.parentElement === state.list) {
      if (state.next && state.next.parentElement === state.list) {
        state.list.insertBefore(row, state.next);
      } else {
        state.list.appendChild(row);
      }
      renumber(state.list);
    }
    showMessage(text);
    refresh(state.region);
  }

  // --- wiring ----------------------------------------------------------------

  document.addEventListener("pointerdown", function (event) {
    if (drag !== null || !event.isPrimary) {
      return;
    }
    if (event.pointerType === "mouse" && event.button !== 0) {
      return;
    }
    var handle = event.target && event.target.closest ? event.target.closest(".drag-handle") : null;
    if (!handle) {
      return;
    }
    event.preventDefault(); // no text selection, no long-press menu
    start(event, handle);
  });

  document.addEventListener("contextmenu", function (event) {
    if (event.target && event.target.closest && event.target.closest(".drag-handle")) {
      event.preventDefault();
    }
  });

  // One row menu open at a time; while it is open, its region is busy.
  document.addEventListener(
    "toggle",
    function (event) {
      var details = event.target;
      if (!details || !details.matches || !details.matches("details.row-menu")) {
        return;
      }
      if (details.open) {
        Array.prototype.forEach.call(document.querySelectorAll("details.row-menu[open]"), function (other) {
          if (other !== details) {
            other.open = false;
          }
        });
      }
      updateBusy(regionOf(details));
    },
    true
  );

  // A click outside an open row menu, or Escape, closes it.
  function closeMenus(except) {
    Array.prototype.forEach.call(document.querySelectorAll("details.row-menu[open]"), function (details) {
      if (!except || !details.contains(except)) {
        details.open = false;
      }
    });
  }

  document.addEventListener("click", function (event) {
    closeMenus(event.target);
  });

  document.addEventListener("keydown", function (event) {
    if (drag === null && event.key === "Escape") {
      closeMenus(null);
    }
  });
})();
