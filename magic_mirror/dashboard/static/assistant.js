(function () {
  "use strict";
  const region = document.getElementById("module-assistant");
  if (!region) return;
  const labels = {
    idle: "Ready for wake word", wake_detected: "Wake word detected",
    listening: "Listening", processing: "Processing",
    responding: "Responding", returning_idle: "Returning to idle",
    error: "Recovering", stopped: "Assistant stopped",
    disabled: "Assistant disabled", unavailable: "Assistant unavailable"
  };
  let timer;

  function transcript(role, value, final) {
    const row = region.querySelector('[data-role="' + role + '-transcript"]');
    row.hidden = !value;
    row.textContent = value ? (role === "user" ? "You: " : "Mirror: ") +
      value + (final ? "" : " …") : "";
  }

  function toolResult(tool) {
    const panel = region.querySelector('[data-role="assistant-tool"]');
    const links = panel.querySelector('[data-role="tool-sources"]');
    links.replaceChildren();
    const valid = tool && tool.version === 1 &&
      ["weather_current", "weather_forecast", "web_search"].includes(tool.kind) &&
      ["loading", "completed", "unavailable"].includes(tool.status);
    panel.hidden = !valid;
    if (!valid) return;
    panel.dataset.kind = tool.kind;
    panel.dataset.status = tool.status;
    panel.querySelector('[data-role="tool-title"]').textContent =
      typeof tool.title === "string" ? tool.title.slice(0, 80) : "";
    panel.querySelector('[data-role="tool-summary"]').textContent =
      typeof tool.summary === "string" ? tool.summary.slice(0, 200) : "";
    const detail = [tool.detail, tool.freshness === "stale" ? "Stale data" : ""];
    panel.querySelector('[data-role="tool-detail"]').textContent =
      detail.filter(value => typeof value === "string" && value).join(" · ").slice(0, 110);
    if (tool.kind !== "web_search" || !Array.isArray(tool.sources)) return;
    tool.sources.slice(0, 3).forEach(function (source) {
      if (!source || typeof source.url !== "string") return;
      try {
        const url = new URL(source.url);
        if (!["https:", "http:"].includes(url.protocol) || !url.hostname.includes(".") ||
            url.hostname === "localhost" || url.hostname.endsWith(".local")) return;
        const link = document.createElement("a");
        link.href = url.href;
        const label = typeof source.title === "string" && source.title ? source.title : url.hostname;
        const domain = typeof source.source_name === "string" && source.source_name ?
          source.source_name : url.hostname;
        const published = typeof source.publication_date === "string" ?
          source.publication_date.slice(0, 10) : "";
        const title = document.createElement("span");
        title.textContent = label.slice(0, 70);
        const meta = document.createElement("small");
        meta.textContent = (domain + (published ? " · " + published : "")).slice(0, 90);
        link.append(title, meta);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        links.append(link);
      } catch (_error) { /* Ignore malformed citation URLs. */ }
    });
  }

  function show(snapshot) {
    const state = Object.hasOwn(labels, snapshot.state) ? snapshot.state : "unavailable";
    region.dataset.state = state;
    region.querySelector('[data-role="status"]').textContent = labels[state];
    region.querySelector('[data-role="detail"]').textContent = state === "error" ?
      (snapshot.error || "Assistant recovering") : state === "idle" ?
      "Say the configured wake phrase to begin." : "";
    transcript("user", snapshot.user_text, snapshot.user_final);
    transcript("assistant", snapshot.assistant_text, snapshot.assistant_final);
    toolResult(snapshot.tool);
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch("/api/assistant", {cache: "no-store", signal: AbortSignal.timeout(1500)});
      if (!response.ok) throw new Error("Assistant state unavailable");
      show(await response.json());
    } catch (_error) {
      show({state: "unavailable"});
    } finally {
      clearTimeout(timer);
      if (!document.hidden) timer = setTimeout(refresh, 1000);
    }
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) clearTimeout(timer);
    else refresh();
  });
  refresh();
}());
