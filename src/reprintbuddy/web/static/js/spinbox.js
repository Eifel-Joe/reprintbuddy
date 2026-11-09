/* Small arrows to count up and down on every number field (Spec 10.1, p155).

   One mechanism for all pages: when the page is loaded, and for every field that appears later
   (a row a script clones, a region live.js swaps in), a field of type `number` or with
   `inputmode="numeric"` / `"decimal"` gets a wrapper with two arrow buttons on its right side.
   No page lists its fields; a field opts out with `data-no-spin`. Without this script the
   fields are what they were: plain fields.

   Rules, the same for every field:
   - the step is the `step` of the field (1 without it); the limits are its `min` and `max`.
     A text field (`inputmode`) without `min` counts from 0: no number field of the program takes a
     negative number. A field that does gets a `min` of its own.
   - an empty or unreadable field counts from 0; a value beyond a limit is drawn to the limit;
     the arrow at a limit is switched off.
   - comma and point are read; the value is written back in the writing of the interface
     (`<html lang>`: a comma in German), a `type=number` field always with a point, which is
     what the browser expects there and shows in the writing of the person's system.
   - the key arrows up and down count in the field itself (a `type=number` field does that anyway).
   - every change fires `input` and `change`, so live.js sees a changed form and does not swap the
     region while somebody is counting.
   Counting never saves anything: a form is sent with its own button or the enter key. */
(function () {
  "use strict";

  var REPEAT_FIRST_MS = 450; // holding an arrow: the second step comes after this
  var REPEAT_MS = 80;

  // --- the numbers (pure; tested with node) -----------------------------------

  function readNumber(text) {
    var clean = String(text === null || text === undefined ? "" : text).replace(/\s+/g, "");
    if (!/^[+-]?\d+(?:[.,]\d+)?$/.test(clean)) {
      return null;
    }
    return parseFloat(clean.replace(",", "."));
  }

  function decimalsOf(step) {
    var text = String(step);
    var at = text.indexOf(".");
    return at < 0 ? 0 : text.length - at - 1;
  }

  function limit(attribute) {
    var value = attribute === null || attribute === undefined ? null : readNumber(attribute);
    return value;
  }

  /* The next value one step up (direction 1) or down (-1), as a number.
     `options`: step (> 0), min and max (null for none). Rounded to the decimals of the step, so
     that 0.1 + 0.2 stays 0.3. */
  function stepped(current, direction, options) {
    var step = options.step > 0 ? options.step : 1;
    var decimals = decimalsOf(step);
    var scale = Math.pow(10, decimals);
    var base = current === null ? 0 : current;
    var next = Math.round(base * scale + direction * Math.round(step * scale)) / scale;
    if (options.min !== null && options.min !== undefined && next < options.min) {
      next = options.min;
    }
    if (options.max !== null && options.max !== undefined && next > options.max) {
      next = options.max;
    }
    return next;
  }

  function writeNumber(value, decimals, language, plain) {
    var text = value.toFixed(decimals);
    if (plain || !/^(de|fr|es|it|nl|pt|pl|cs|da|sv|nb|fi|ru|tr)\b/i.test(language || "")) {
      return text;
    }
    return text.replace(".", ",");
  }

  var api = {
    readNumber: readNumber,
    decimalsOf: decimalsOf,
    stepped: stepped,
    writeNumber: writeNumber,
    limit: limit,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof document === "undefined") {
    return;
  }

  // --- the fields -----------------------------------------------------------

  var SELECTOR = 'input[type="number"], input[inputmode="numeric"], input[inputmode="decimal"]';

  function optionsOf(input) {
    var stepAttr = input.getAttribute("step");
    var step = stepAttr === null || stepAttr === "any" ? 1 : readNumber(stepAttr);
    var min = limit(input.getAttribute("min"));
    if (min === null && input.type !== "number") {
      min = 0;
    }
    return { step: step === null || step <= 0 ? 1 : step, min: min, max: limit(input.getAttribute("max")) };
  }

  function currentOf(input) {
    return readNumber(input.value);
  }

  function setValue(input, value, options) {
    var text = writeNumber(value, decimalsOf(options.step), document.documentElement.lang, input.type === "number");
    if (input.value === text) {
      return false;
    }
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // true when the field changed; a held arrow stops as soon as it does not.
  function count(input, direction) {
    if (input.disabled || input.readOnly) {
      return false;
    }
    var options = optionsOf(input);
    return setValue(input, stepped(currentOf(input), direction, options), options);
  }

  // The repeat of every held arrow of the page. A browser may not send the end of a press to an arrow
  // that has just been switched off (it reached its limit): so a press elsewhere, the end of any press
  // on the page, or a count that changes nothing stops them all.
  var holding = [];

  function stopAll() {
    holding.slice().forEach(function (stop) {
      stop();
    });
  }

  function refresh(input, up, down) {
    var options = optionsOf(input);
    var current = currentOf(input);
    var locked = input.disabled || input.readOnly;
    up.disabled = locked || (options.max !== null && current !== null && current >= options.max);
    down.disabled = locked || (options.min !== null && current !== null && current <= options.min);
  }

  function arrow(direction) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "spin-btn " + (direction > 0 ? "spin-up" : "spin-down");
    button.tabIndex = -1; // the keyboard has the arrow keys in the field
    button.setAttribute("aria-hidden", "true");
    return button;
  }

  function hold(button, input, direction) {
    var first = null;
    var again = null;

    function stop() {
      if (first === null && again === null) {
        return;
      }
      if (first !== null) {
        window.clearTimeout(first);
        first = null;
      }
      if (again !== null) {
        window.clearInterval(again);
        again = null;
      }
      holding.splice(holding.indexOf(stop), 1);
    }

    button.addEventListener("mousedown", function (event) {
      event.preventDefault(); // older browsers move the focus on mousedown even when pointerdown was cancelled
    });
    button.addEventListener("pointerdown", function (event) {
      if (event.button !== undefined && event.button !== 0) {
        return;
      }
      event.preventDefault(); // the field keeps the focus
      stopAll();
      count(input, direction);
      holding.push(stop);
      first = window.setTimeout(function () {
        first = null;
        again = window.setInterval(function () {
          if (!count(input, direction)) {
            stop();
          }
        }, REPEAT_MS);
      }, REPEAT_FIRST_MS);
    });
    ["pointerup", "pointerleave", "pointercancel", "blur"].forEach(function (name) {
      button.addEventListener(name, stop);
    });
    button.addEventListener("click", function (event) {
      event.preventDefault(); // counted on pointerdown; a keyboard never reaches the button
    });
  }

  function enhance(input) {
    if (input.hasAttribute("data-spin-ready") || input.hasAttribute("data-no-spin") || !input.parentNode) {
      return;
    }
    input.setAttribute("data-spin-ready", "");
    var wrap = document.createElement("span");
    wrap.className = "spin";
    var buttons = document.createElement("span");
    buttons.className = "spin-btns";
    var up = arrow(1);
    var down = arrow(-1);
    buttons.appendChild(up);
    buttons.appendChild(down);
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    wrap.appendChild(buttons);
    hold(up, input, 1);
    hold(down, input, -1);
    var update = function () {
      refresh(input, up, down);
    };
    input.addEventListener("input", update);
    input.addEventListener("change", update);
    if (input.type !== "number") {
      input.addEventListener("keydown", function (event) {
        if ((event.key !== "ArrowUp" && event.key !== "ArrowDown") || event.altKey || event.ctrlKey || event.metaKey) {
          return;
        }
        event.preventDefault();
        count(input, event.key === "ArrowUp" ? 1 : -1);
      });
    }
    update();
  }

  function enhanceWithin(node) {
    if (node.nodeType !== 1) {
      return;
    }
    if (node.matches(SELECTOR)) {
      enhance(node);
    }
    Array.prototype.forEach.call(node.querySelectorAll(SELECTOR), enhance);
  }

  function start() {
    document.addEventListener("pointerup", stopAll);
    document.addEventListener("pointercancel", stopAll);
    enhanceWithin(document.body);
    if (window.MutationObserver) {
      new window.MutationObserver(function (records) {
        records.forEach(function (record) {
          Array.prototype.forEach.call(record.addedNodes, enhanceWithin);
        });
      }).observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
