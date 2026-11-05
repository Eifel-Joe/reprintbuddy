/* Restart and update pages. No texts and no addresses in here: both come from the page. */
(function () {
  "use strict";

  // --- restart: wait until the new process answers, then go back -------------
  var wait = document.getElementById("restart-wait");
  if (wait) {
    var healthUrl = wait.getAttribute("data-health-url");
    var returnUrl = wait.getAttribute("data-return-url");
    var started = Date.now();
    var sawFailure = false;
    var checkHealth = function () {
      fetch(healthUrl, { cache: "no-store" })
        .then(function (response) {
          if (response.ok) {
            // An answer straight away may still come from the old process.
            if (sawFailure || Date.now() - started > 20000) {
              window.location.href = returnUrl;
              return;
            }
          } else {
            sawFailure = true;
          }
          window.setTimeout(checkHealth, 2000);
        })
        .catch(function () {
          sawFailure = true;
          window.setTimeout(checkHealth, 2000);
        });
    };
    window.setTimeout(checkHealth, 3000);
  }

  // --- update: reload the page when the state or the step changes -------------
  var update = document.getElementById("update-status");
  if (update) {
    var statusUrl = update.getAttribute("data-status-url");
    var knownState = update.getAttribute("data-state");
    var knownStep = update.getAttribute("data-step");
    var poll = function () {
      fetch(statusUrl, { cache: "no-store", headers: { Accept: "application/json" } })
        .then(function (response) {
          return response.json();
        })
        .then(function (status) {
          if (status.state !== knownState || status.step !== knownStep) {
            window.location.reload();
            return;
          }
          window.setTimeout(poll, 3000);
        })
        .catch(function () {
          // The service restarts during an update, or the sign-in page answers: keep asking.
          window.setTimeout(poll, 3000);
        });
    };
    window.setTimeout(poll, 3000);
  }
})();
