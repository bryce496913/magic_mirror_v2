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

  function show(snapshot) {
    const state = Object.hasOwn(labels, snapshot.state) ? snapshot.state : "unavailable";
    region.dataset.state = state;
    region.querySelector('[data-role="status"]').textContent = labels[state];
    region.querySelector('[data-role="detail"]').textContent = state === "error" ?
      (snapshot.error || "Assistant recovering") : state === "idle" ?
      "Say the configured wake phrase to begin." : "";
    transcript("user", snapshot.user_text, snapshot.user_final);
    transcript("assistant", snapshot.assistant_text, snapshot.assistant_final);
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
