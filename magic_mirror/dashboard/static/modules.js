(function () {
  "use strict";
  // Future integrations can update one region without changing the page layout.
  // Plain text avoids injecting markup from external data.
  window.MirrorDashboard = Object.freeze({
    setModule: function (name, data) {
      const region = document.getElementById("module-" + name);
      if (!region || !region.classList.contains("module")) return false;
      const update = data || {};
      region.querySelector('[data-role="status"]').textContent = update.status || "Unavailable";
      region.querySelector('[data-role="detail"]').textContent = update.detail || "No data available.";
      if (name === "assistant") region.dataset.state = update.state || "unavailable";
      return true;
    }
  });
}());
