/* Pending Copy Orders metric helper (paper-only, no keys/signing).
 *
 * Include after the main dashboard script, or paste the install() call once
 * the portfolio metrics are on the page.
 */
(function () {
  function ensureMetric() {
    if (document.getElementById("pending-copy")) return;
    var grid = document.querySelector(".panel .grid");
    if (!grid) return;
    var el = document.createElement("div");
    el.className = "metric";
    el.innerHTML =
      '<div class="label">Pending Copy Orders</div>' +
      '<p id="pending-copy" class="value">—</p>';
    grid.appendChild(el);
  }

  function setPendingFromReport(r) {
    ensureMetric();
    var node = document.getElementById("pending-copy");
    if (!node) return;
    var count =
      r && r.pending_copy && r.pending_copy.pending_count != null
        ? r.pending_copy.pending_count
        : "—";
    node.textContent = String(count);
  }

  // Expose for the main dashboard script to call after /api/paper-simulation.
  window.EngenPendingCopy = { ensureMetric: ensureMetric, setPendingFromReport: setPendingFromReport };

  // Best-effort: when paper-simulation responses are handled via fetch hooks.
  if (typeof window.fetch === "function") {
    var orig = window.fetch.bind(window);
    window.fetch = function () {
      return orig.apply(null, arguments).then(function (res) {
        try {
          var url = String(arguments[0] || "");
          if (url.indexOf("/api/paper-simulation") !== -1 && res.ok) {
            res
              .clone()
              .json()
              .then(function (data) {
                var r = (data && data.report) || data || {};
                setPendingFromReport(r);
              })
              .catch(function () {});
          }
        } catch (e) {}
        return res;
      });
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ensureMetric);
  } else {
    ensureMetric();
  }
})();
