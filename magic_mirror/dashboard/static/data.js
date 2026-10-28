(function () {
  "use strict";
  const INTERVAL_MS = 15000;
  const names = ["system", "weather", "news", "pihole"];
  let timer;

  function number(value, suffix) {
    return typeof value === "number" && Number.isFinite(value) ? value + (suffix || "") : "—";
  }

  function formatTemperature(value, unit) {
    return typeof value === "number" && Number.isFinite(value) ?
      String(Number(value.toFixed(0))) + "°" + unit : "—";
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {formatTemperature};
  if (typeof document === "undefined") return;

  function setWeatherReading(region, name, data) {
    region.querySelector('[data-role="' + name + '-c"]').textContent = formatTemperature(data[name + "_c"], "C");
    region.querySelector('[data-role="' + name + '-f"]').textContent = formatTemperature(data[name + "_f"], "F");
  }

  function uptime(seconds) {
    if (typeof seconds !== "number") return "—";
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor(seconds % 86400 / 3600);
    return days ? days + "d " + hours + "h" : hours + "h " + Math.floor(seconds % 3600 / 60) + "m";
  }

  function present(name, snapshot) {
    const data = snapshot && snapshot.data;
    const stale = snapshot && snapshot.status === "stale";
    if (!data || snapshot.status === "unavailable") {
      if (name === "weather") {
        const region = document.getElementById("module-weather");
        region.querySelector('[data-role="weather-current"]').hidden = true;
        region.querySelector('[data-role="weather-metrics"]').hidden = true;
      }
      const list = document.querySelector("#module-news [data-role=headlines]");
      if (name === "news" && list) list.replaceChildren();
      window.MirrorDashboard.setModule(name, {
        status: "Unavailable", detail: snapshot && snapshot.message || "No data available."
      });
      return;
    }
    let status, detail;
    if (name === "system") {
      status = "CPU " + number(data.cpu_percent, "%") + " · " + number(data.cpu_temperature_c, "°C");
      detail = "RAM " + number(data.ram_used_gb) + "/" + number(data.ram_total_gb, " GB") + " (" + number(data.ram_percent, "%") + ") · Disk " + number(data.disk_percent, "%") + " · Up " + uptime(data.uptime_seconds);
    } else if (name === "weather") {
      const region = document.getElementById("module-weather");
      ["temperature", "high", "low", "feels_like"].forEach(function (field) {
        setWeatherReading(region, field, data);
      });
      region.querySelector('[data-role="weather-current"]').hidden = false;
      region.querySelector('[data-role="weather-metrics"]').hidden = false;
      region.querySelector('[data-role="feels-row"]').hidden = data.feels_like_c == null || data.feels_like_f == null;
      status = (data.icon || "") + " " + (data.description || "Conditions unavailable");
      detail = "Tomorrow: " + (data.forecast || "Unavailable");
    } else if (name === "pihole") {
      status = data.blocking_enabled ? "Blocking enabled" : "Blocking disabled";
      detail = number(data.queries_total) + " queries · " + number(data.queries_blocked) + " blocked (" + number(data.blocked_percent, "%") + ")";
    } else if (name === "news") {
      const items = Array.isArray(data.headlines) ? data.headlines : [];
      const first = items[0];
      status = first ? first.title : "No headlines";
      const date = first && first.published_at ? new Date(first.published_at) : null;
      detail = first ? (first.publisher || "Unknown publisher") + (date && !Number.isNaN(date.valueOf()) ? " · " + date.toLocaleString() : "") : "No data available.";
      const list = document.querySelector("#module-news [data-role=headlines]");
      if (list) {
        list.replaceChildren();
        items.slice(1, 3).forEach(function (item) {
          const row = document.createElement("p");
          const published = item.published_at ? new Date(item.published_at) : null;
          row.textContent = item.title + " — " + (item.publisher || "Unknown publisher") +
            (published && !Number.isNaN(published.valueOf()) ? " · " + published.toLocaleString() : "");
          list.appendChild(row);
        });
      }
    }
    window.MirrorDashboard.setModule(name, {status: (stale ? "Stale · " : "") + status, detail: detail});
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch("/api/dashboard", {cache: "no-store", signal: AbortSignal.timeout(6000)});
      if (!response.ok) throw new Error("Dashboard endpoint unavailable");
      const payload = await response.json();
      names.forEach(function (name) {
        if (document.getElementById("module-" + name)) present(name, payload.modules && payload.modules[name]);
      });
    } catch (_error) {
      names.forEach(function (name) {
        const region = document.getElementById("module-" + name);
        if (region) {
          const status = region.querySelector('[data-role="status"]');
          if (status.textContent === "Unavailable" || status.textContent === "Not connected") {
            status.textContent = "Unavailable";
          } else if (!status.textContent.startsWith("Stale · ")) {
            status.textContent = "Stale · " + status.textContent;
          }
        }
      });
    } finally {
      clearTimeout(timer);
      if (!document.hidden) timer = setTimeout(refresh, INTERVAL_MS);
    }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(timer);
    else refresh();
  });
  refresh();
}());
