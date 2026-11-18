const assert = require("node:assert/strict");
const {duration, measure} = require("../frontend/static/timing.js");
const start = "2026-10-23T08:57:11.432898Z";
const finish = "2026-10-23T09:27:15.738962Z";
const job = {created_at:start, updated_at:finish, finished_at:finish, status:"partial"};

assert.equal(measure(job).text, "30 分 04 秒");
assert.equal(measure(job, Date.parse(finish) + 86400000).text, "30 分 04 秒");
for (const status of ["completed", "partial", "cancelled", "failed"]) {
  assert.equal(measure({...job, status}).approximate, false);
  assert.equal(measure({...job, status}).label, "总耗时");
}
for (const status of ["queued", "running"]) {
  assert.equal(measure({...job, status}, Date.parse(start) + 65000).text, "1 分 05 秒");
  assert.equal(measure({...job, status}, Date.parse(start) + 66000).text, "1 分 06 秒");
}
const interrupted = measure({...job, status:"interrupted", finished_at:undefined});
assert.equal(interrupted.approximate, true);
assert.equal(interrupted.text, "30 分 04 秒");
assert.equal(interrupted.label, "已记录耗时");
assert.equal(measure({status:"completed"}).text, "—");
assert.equal(measure({...job, created_at:"bad date"}).text, "—");
assert.equal(measure({...job, finished_at:"2026-10-20T00:00:00Z"}).text, "—");
assert.equal(duration(0), "0 秒");
assert.equal(duration(59999), "59 秒");
assert.equal(duration(3600000), "1 小时 00 分 00 秒");
assert.equal(duration(90061000), "25 小时 01 分 01 秒");
assert.equal(duration(null), "—");
console.log("Timing checks passed: historical, live, terminal, interrupted and invalid timestamps.");
