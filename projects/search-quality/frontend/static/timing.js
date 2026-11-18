"use strict";

// Wall-clock time for a whole run; parallel request latencies must not be added.
const SearchBenchTiming = (() => {
  function timestamp(value) {
    return typeof value === "string" && value.trim() ? Date.parse(value) : NaN;
  }

  function duration(milliseconds) {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "—";
    const seconds = Math.floor(milliseconds / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds / 60) % 60;
    const pad = value => String(value).padStart(2, "0");
    if (hours) return `${hours} 小时 ${pad(minutes)} 分 ${pad(seconds % 60)} 秒`;
    if (minutes) return `${minutes} 分 ${pad(seconds % 60)} 秒`;
    return `${seconds} 秒`;
  }

  function measure(job, now = Date.now()) {
    const running = ["queued", "running"].includes(job.status);
    const start = timestamp(job.created_at);
    const finished = timestamp(job.finished_at);
    const approximate = !running && !Number.isFinite(finished);
    const end = running ? now : approximate ? timestamp(job.updated_at) : finished;
    const elapsed = Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
    return {running, approximate, start, end, elapsed,
      label: running ? "已用时间" : approximate ? "已记录耗时" : "总耗时",
      text: duration(elapsed)};
  }

  return {duration, measure};
})();

if (typeof module !== "undefined" && module.exports) module.exports = SearchBenchTiming;
