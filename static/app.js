/* decision-chain CHAIN DASHBOARD - minimal client script, offline only.
   The dashboard is fully server-rendered from the run artifact; the only
   client behavior is the light/dark theme toggle (persisted locally). */
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "chain-theme";

  function apply(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode: ignore */ }
  }

  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* ignore */ }
  if (saved === "dark" || saved === "light") { apply(saved); }

  var toggle = document.getElementById("themeToggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      apply(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });
  }
})();
