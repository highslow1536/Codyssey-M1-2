import test from "node:test";
import assert from "node:assert/strict";
import {
  buildScheduleCreateBody,
  buildScheduleUpdateBody,
} from "../js/briefings.js";

test("pausing a stored API schedule preserves its timezone and watchlist", () => {
  const schedule = {
    market: "US",
    instrument_ids: ["NASDAQ_AAPL"],
    schedule_kind: "fixed_time",
    timezone: "Europe/London",
    local_time: "09:00",
    weekdays: [0, 2, 4],
    close_offset_minutes: 40,
    active: true,
    revision: 3,
  };
  const body = buildScheduleUpdateBody(schedule, { active: false });
  assert.equal(body.timezone, "Europe/London");
  assert.deepEqual(body.instrument_ids, schedule.instrument_ids);
  assert.deepEqual(body.weekdays, schedule.weekdays);
  assert.equal(body.local_time, "09:00");
  assert.equal(body.expected_revision, 3);
  assert.equal(body.active, false);
  assert.ok(!Object.hasOwn(body, "idempotency_key"));
});

test("market-only close schedules have a server-valid weekday field and market timezone", () => {
  const body = buildScheduleCreateBody({
    market: "US",
    instrument_ids: [],
    schedule_kind: "market_close",
    timezone: "Asia/Seoul",
    weekdays: [],
    idempotency_key: "test-intent",
  });
  assert.equal(body.timezone, "America/New_York");
  assert.ok(body.weekdays.length > 0);
  assert.deepEqual(body.instrument_ids, []);
  assert.equal(body.destination_alias, "default");
});
