// Real Auth + Firestore + FastAPI + actual Yahoo/CNN preview data.
// A test-only dummy webhook config allows QUEUEING; no Discord network send occurs.
// One explicit emulator-only delivery failure below exercises the retry UI.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { execFileSync } from "node:child_process";
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.BROWSER_EXECUTABLE_PATH,
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
page.setDefaultTimeout(25000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const responseFor = (path, method) =>
  page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === path && r.request().method() === method,
  );
const idle = () =>
  page.waitForFunction(
    () =>
      !document.getElementById("briefing-save").disabled &&
      !document.getElementById("briefing-history-reload").disabled,
  );
try {
  await page.goto("http://localhost:5173#briefings");
  await page.locator("#email").fill("researcher@example.test");
  await page.locator("#password").fill("browser-test-password");
  const initialLoad = responseFor("/api/briefings", "GET");
  await page.locator("#login-submit").click();
  await page.locator("#authenticated").waitFor();
  await initialLoad;
  await idle();
  assert.equal(await page.locator("#briefings-view").isVisible(), true);
  // Market-only close schedule; no mandatory watchlist.
  await page.locator("#briefing-market").selectOption("US");
  let pending = responseFor("/api/briefing-schedules", "POST");
  await page.locator("#briefing-save").click();
  let response = await pending;
  assert.equal(response.status(), 201, await response.text());
  let schedule = await response.json();
  const scheduleId = schedule.id;
  assert.deepEqual(schedule.instrument_ids, []);
  assert.equal(schedule.timezone, "America/New_York");
  await idle();
  let card = page
    .locator(".briefing-schedule")
    .filter({ hasText: "US" })
    .last();
  await card.getByRole("button", { name: "수정", exact: true }).click();
  await page.locator("#briefing-kind").selectOption("fixed_time");
  await page.locator("#briefing-timezone").selectOption("Asia/Seoul");
  await page.locator("#briefing-local-time").fill("08:30");
  await page
    .locator('#briefing-instruments input[value="NASDAQ_AAPL"]')
    .check();
  pending = responseFor(`/api/briefing-schedules/${scheduleId}`, "PATCH");
  await page.locator("#briefing-save").click();
  response = await pending;
  assert.equal(response.status(), 200, await response.text());
  schedule = await response.json();
  assert.equal(schedule.local_time, "08:30");
  assert.deepEqual(schedule.instrument_ids, ["NASDAQ_AAPL"]);
  await idle();
  card = page.locator(".briefing-schedule").filter({ hasText: "Apple" }).last();
  await card.getByRole("button", { name: "일시정지", exact: true }).click();
  pending = responseFor(`/api/briefing-schedules/${scheduleId}`, "PATCH");
  await page.locator("#confirm-ok").click();
  response = await pending;
  assert.equal((await response.json()).active, false);
  await idle();
  pending = responseFor(`/api/briefing-schedules/${scheduleId}`, "PATCH");
  await card.getByRole("button", { name: "다시 시작", exact: true }).click();
  assert.equal((await (await pending).json()).active, true);
  await idle();
  await page.locator("#briefing-preview-market").selectOption("US");
  await page
    .locator('#briefing-preview-instruments input[value="NASDAQ_AAPL"]')
    .check();
  pending = responseFor("/api/briefings/preview", "POST");
  await page.locator("#briefing-preview-run").click();
  response = await pending;
  assert.equal(response.status(), 201, await response.text());
  const preview = await response.json();
  assert.ok(preview.sources.some((source) => source.url.includes("yahoo")));
  assert.ok(preview.sources.some((source) => source.url.includes("cnn")));
  assert.ok(preview.text.includes("뉴스"));
  assert.ok(preview.data_as_of.length > 0);
  await page.waitForFunction(
    () => !document.getElementById("briefing-preview-run").disabled,
  );
  assert.ok(
    !(await page.locator("#briefing-preview-result").textContent()).includes(
      "Invalid Date",
    ),
  );
  await page.locator("#toast").waitFor({ state: "hidden" });
  await mkdir("artifacts", { recursive: true });
  await page.screenshot({
    path: "artifacts/web-briefings-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth + 1,
    ),
  );
  await page.screenshot({
    path: "artifacts/web-briefings-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1050 });
  // Lost HTTP response after the real queue commit: retry uses the identical key.
  const sendPath = `/api/briefings/${preview.id}/send`;
  const keys = [];
  let lose = true;
  await page.route(`**${sendPath}`, async (route) => {
    keys.push(route.request().postDataJSON().idempotency_key);
    if (lose) {
      lose = false;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page.locator("#briefing-send").click();
  await page.locator("#briefing-send-retry").waitFor();
  pending = responseFor(sendPath, "POST");
  await page.locator("#briefing-send-retry").click();
  response = await pending;
  assert.equal((await response.json()).delivery_status, "queued");
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  await page.waitForFunction(
    () => !document.getElementById("briefing-preview-run").disabled,
  );
  // Explicitly mark this DISPOSABLE emulator row failed; no fake market data.
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      `
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
import os
assert os.environ['FIRESTORE_EMULATOR_HOST']=='127.0.0.1:8080'
db=Client(project='demo-stock-assistant',credentials=AnonymousCredentials())
ref=db.collection('briefings').document('${preview.id}')
row=ref.get().to_dict()
assert row['owner_uid']=='browser-test-owner'
parts=row['parts']; parts[0].update(status='failed',attempts=5,last_error={'code':'test_only','message':'브라우저 검사용 전송 실패'},uncertain=True)
ref.update({'delivery_status':'failed','parts':parts})
db.collection('outbox').document('${preview.id}').update({'status':'failed'})
`,
    ],
    { env: { ...process.env, FIRESTORE_EMULATOR_HOST: "127.0.0.1:8080" } },
  );
  await page.locator("#briefing-history-reload").click();
  await idle();
  const failed = page
    .locator(".briefing-history-item")
    .filter({ hasText: "브라우저 검사용 전송 실패" });
  assert.equal(await failed.count(), 1);
  assert.ok(!(await failed.textContent()).includes("[object Object]"));
  pending = responseFor(sendPath, "POST");
  await failed.getByRole("button", { name: "다시 시도", exact: true }).click();
  response = await pending;
  assert.equal((await response.json()).delivery_status, "queued");
  assert.notEqual(keys[2], keys[1]);
  await idle();
  await page
    .locator("#briefing-history-list .briefing-history-item")
    .first()
    .getByRole("button", { name: "상세 보기" })
    .click();
  await page.locator("#briefing-detail").waitFor();
  assert.ok(
    (await page.locator("#briefing-detail").textContent()).includes(
      "전송 대기",
    ),
  );
  pending = responseFor("/api/briefings", "GET");
  await page.reload();
  await page.locator("#authenticated").waitFor();
  await pending;
  await idle();
  assert.ok(
    await page
      .locator(".briefing-schedule")
      .filter({ hasText: "Apple" })
      .count(),
  );
  card = page.locator(".briefing-schedule").filter({ hasText: "Apple" }).last();
  await card.getByRole("button", { name: "삭제", exact: true }).click();
  pending = responseFor(`/api/briefing-schedules/${scheduleId}`, "DELETE");
  await page.locator("#confirm-ok").click();
  assert.equal((await pending).status(), 204);
  await idle();
  await page.locator("#logout").click();
  await page.locator("#login-view").waitFor();
  assert.equal(
    await page.locator("#briefing-preview-result").textContent(),
    "",
  );
  assert.equal(await page.locator(".briefing-history-item").count(), 0);
  await page.locator("#email").fill("other@example.test");
  await page.locator("#password").fill("browser-test-password");
  pending = responseFor("/api/briefings", "GET");
  await page.locator("#login-submit").click();
  await page.locator("#authenticated").waitFor();
  await pending;
  await idle();
  assert.equal(await page.locator(".briefing-history-item").count(), 0);
  assert.deepEqual(errors, []);
  console.log(
    "Briefing browser checks passed: CRUD, close/fixed schedule, real preview, queue lost-response retry, failed retry, history, mobile, account isolation.",
  );
} finally {
  await browser.close();
}
