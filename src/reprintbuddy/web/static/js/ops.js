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
})();
