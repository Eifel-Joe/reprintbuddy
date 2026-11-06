/* Live pages (Spec 10.5, plan decisions 3 to 8): listen on the live channel of the page and
   fetch the regions of the page that a saved change concerns, without ever overwriting what
   somebody is typing. No texts and no addresses in here: both come from the page.

   Terms: a form is "changed" when one of its fields differs from the value the page came with;
   a region is "busy" while a form in it is changed or holds the focused field. A busy region is
   not swapped but remembered as stale and fetched once it is no longer busy. */
(function () {
  "use strict";

  var app = document.getElementById("app");
  if (!app || !app.hasAttribute("data-live") || !window.EventSource) {
    return;
  }

  var HEADER = "X-Live-Regions";
  var FIRST_RETRY_MS = 2000; // after the stream was closed for good; doubled up to the maximum
  var MAX_RETRY_MS = 60000;
  var SETTLE_MS = 250; // after focusout or input: wait until the focus has moved on

  var streamAddress = app.getAttribute("data-live");
  var source = app.getAttribute("data-live-src") || ""; // empty: the page answered a POST
  var known = app.getAttribute("data-live-state") || "";

  var stream = null;
  var stopped = false; // a new version runs: no more connecting
  var retryMs = FIRST_RETRY_MS;
  var retryTimer = null;
  var wanted = {}; // region names for the next request
  var stale = {}; // region names that changed while they were busy
  var fetching = false;
  var settleTimer = null;

  // --- forms ---------------------------------------------------------------

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
    // A single choice starts at the last option marked selected, or else at the first
    // option that is not disabled (what the browser shows when none is marked).
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
      return false; // buttons, fieldsets, outputs
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

  function formChanged(form) {
    for (var i = 0; i < form.elements.length; i++) {
      if (fieldChanged(form.elements[i])) {
        return true;
      }
    }
    return false;
  }

  function formBusy(form) {
    return formChanged(form) || form.contains(document.activeElement);
  }

  function anyFormChanged() {
    var forms = document.forms;
    for (var i = 0; i < forms.length; i++) {
      if (formChanged(forms[i])) {
        return true;
      }
    }
    return false;
  }

  // --- regions -------------------------------------------------------------

  function words(text) {
    return (text || "").split(/\s+/).filter(function (word) {
      return word !== "";
    });
  }

  function meets(text, topics) {
    var own = words(text);
    for (var i = 0; i < own.length; i++) {
      if (topics.indexOf(own[i]) >= 0) {
        return true;
      }
    }
    return false;
  }

  function allRegions() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-live-region]"));
  }

  function regionNamed(name) {
    var regions = allRegions();
    for (var i = 0; i < regions.length; i++) {
      if (regions[i].getAttribute("data-live-region") === name) {
        return regions[i];
      }
    }
    return null;
  }

  function busy(region) {
    var forms = region.querySelectorAll("form");
    for (var i = 0; i < forms.length; i++) {
      if (formBusy(forms[i])) {
        return true;
      }
    }
    return false;
  }

  function showNotice(element) {
    if (element) {
      element.hidden = false;
    }
  }

  // The page cannot bring itself up to date (signed out, an error page, a region that is
  // gone, a new version): load it again, unless that would throw away what somebody typed.
  function reloadOrNotice() {
    if (anyFormChanged()) {
      showNotice(document.getElementById("live-version"));
    } else {
      window.location.reload();
    }
  }

  function want(names) {
    if (!source) {
      return; // a page shown for a POST has an address that only answers POST
    }
    names.forEach(function (name) {
      wanted[name] = true;
    });
    pump();
  }

  // One request at a time; names that come in meanwhile go out with the next one.
  function pump() {
    if (fetching || !source) {
      return;
    }
    var names = [];
    Object.keys(wanted).forEach(function (name) {
      delete wanted[name];
      var region = regionNamed(name);
      if (!region) {
        return;
      }
      if (busy(region)) {
        stale[name] = true;
      } else {
        names.push(name);
      }
    });
    if (names.length === 0) {
      return;
    }
    fetching = true;
    var headers = {};
    headers[HEADER] = names.join(" ");
    fetch(source, { headers: headers, cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        // A redirect (to the sign-in page) or an error page carries no regions of this page;
        // inserting it would put the sign-in page into this page.
        var found = words(response.headers.get(HEADER));
        var complete = names.every(function (name) {
          return found.indexOf(name) >= 0;
        });
        if (response.status !== 200 || response.redirected || !complete) {
          reloadOrNotice();
          return null;
        }
        return response.text().then(function (html) {
          swap(names, html);
        });
      })
      .catch(function () {
        // No answer (the service restarts): the next hello fetches every region.
        known = "";
      })
      .then(function () {
        fetching = false;
        pump();
      });
  }

  function swap(names, html) {
    var template = document.createElement("template");
    template.innerHTML = html;
    var fresh = {};
    Array.prototype.forEach.call(template.content.querySelectorAll("[data-live-region]"), function (node) {
      fresh[node.getAttribute("data-live-region")] = node;
    });
    var missing = names.some(function (name) {
      return !fresh[name];
    });
    if (missing) {
      reloadOrNotice();
      return;
    }
    names.forEach(function (name) {
      var region = regionNamed(name);
      if (!region) {
        return;
      }
      if (busy(region)) {
        stale[name] = true; // somebody started typing while the answer was on its way
        return;
      }
      delete stale[name];
      region.replaceWith(fresh[name]);
    });
  }

  function fetchStaleSoon() {
    if (settleTimer !== null) {
      window.clearTimeout(settleTimer);
    }
    settleTimer = window.setTimeout(function () {
      settleTimer = null;
      var free = Object.keys(stale).filter(function (name) {
        var region = regionNamed(name);
        if (!region) {
          delete stale[name];
          return false;
        }
        return !busy(region);
      });
      free.forEach(function (name) {
        delete stale[name];
      });
      if (free.length > 0) {
        want(free);
      }
    }, SETTLE_MS);
  }

  // --- the live channel ----------------------------------------------------

  function payload(event) {
    try {
      return JSON.parse(event.data) || {};
    } catch (error) {
      return {};
    }
  }

  function onHello(event) {
    if (event.target !== stream) {
      return;
    }
    var data = payload(event);
    retryMs = FIRST_RETRY_MS;
    if (data.state !== known) {
      // Something was saved since the page was drawn, or while the stream was away.
      known = data.state;
      want(
        allRegions().map(function (region) {
          return region.getAttribute("data-live-region");
        })
      );
    }
  }

  function onChange(event) {
    if (event.target !== stream) {
      return;
    }
    var data = payload(event);
    var topics = Array.isArray(data.topics) ? data.topics : [];
    if (typeof data.state === "string") {
      known = data.state;
    }
    var names = [];
    var swapping = [];
    allRegions().forEach(function (region) {
      if (meets(region.getAttribute("data-live-topics"), topics)) {
        names.push(region.getAttribute("data-live-region"));
        if (source && !busy(region)) {
          swapping.push(region);
        }
      }
    });
    // A form about a thing that changed and that is not swapped now says so.
    Array.prototype.forEach.call(document.querySelectorAll("form[data-live-watch]"), function (form) {
      if (!meets(form.getAttribute("data-live-watch"), topics)) {
        return;
      }
      var swapped = swapping.some(function (region) {
        return region.contains(form);
      });
      if (!swapped) {
        showNotice(staleNoticeOf(form));
      }
    });
    want(names);
  }

  function staleNoticeOf(form) {
    for (var i = 0; i < form.children.length; i++) {
      if (form.children[i].classList.contains("live-stale")) {
        return form.children[i];
      }
    }
    return form.querySelector(".live-stale");
  }

  function onNewVersion(event) {
    if (event.target !== stream) {
      return;
    }
    stopped = true;
    disconnect();
    reloadOrNotice();
  }

  function onError(event) {
    if (event.target !== stream || stream.readyState !== EventSource.CLOSED) {
      return; // CONNECTING: the browser tries again by itself
    }
    stream = null;
    retryTimer = window.setTimeout(function () {
      retryTimer = null;
      connect();
    }, retryMs);
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
  }

  function connect() {
    if (stopped || stream !== null || document.visibilityState === "hidden") {
      return;
    }
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
    stream = new EventSource(streamAddress);
    stream.addEventListener("hello", onHello);
    stream.addEventListener("change", onChange);
    stream.addEventListener("reload", onNewVersion);
    stream.addEventListener("error", onError);
  }

  function disconnect() {
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (stream !== null) {
      stream.close();
      stream = null;
    }
  }

  // A hidden tab gives its connection back: browsers open only about six per server.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      disconnect();
    } else {
      connect();
    }
  });
  window.addEventListener("pagehide", disconnect);
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      connect(); // back from the browser's page cache
    }
  });

  document.addEventListener("focusout", fetchStaleSoon);
  document.addEventListener("input", fetchStaleSoon);
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (target && target.closest && target.closest("[data-live-reload]")) {
      window.location.reload();
    }
  });

  connect();
})();
