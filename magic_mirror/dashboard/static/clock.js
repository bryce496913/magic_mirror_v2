(function () {
  "use strict";

  function formatTime(date, mode, locale) {
    return new Intl.DateTimeFormat(locale || undefined, {
      hour: "2-digit", minute: "2-digit", hourCycle: mode === "12" ? "h12" : "h23"
    }).format(date);
  }
  function formatDate(date, locale) {
    return new Intl.DateTimeFormat(locale || undefined, {
      weekday: "long", day: "numeric", month: "long"
    }).format(date);
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { formatTime, formatDate };
  if (typeof document === "undefined") return;

  const region = document.querySelector(".clock-region");
  const timeNode = document.getElementById("clock-time");
  const dateNode = document.getElementById("clock-date");
  if (!region || !timeNode || !dateNode) return;

  let timer;
  function render() {
    const now = new Date(); // Browser local timezone, including DST changes.
    timeNode.textContent = formatTime(now, region.dataset.clockFormat);
    dateNode.textContent = formatDate(now);
    timeNode.dateTime = now.toISOString();
    clearTimeout(timer);
    timer = setTimeout(render, (60 - now.getSeconds()) * 1000 - now.getMilliseconds() + 50);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) render(); });
  window.addEventListener("pageshow", render);
  render();
}());
