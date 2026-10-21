// Real Firebase Auth + Firestore + API. History must contain genuine Yahoo
// observations collected by the live validation; no manufactured market prices.
// A disposable emulator-only failure state below tests retry behavior; no
// external Discord webhook is contacted by this browser test.
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
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
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
      !document.getElementById("alert-preview-run").disabled &&
      !document.getElementById("alert-events-reload").disabled,
  );
const preview = async () => {
  const pending = responseFor("/api/alerts/preview", "POST");
  await page.locator("#alert-preview-run").click();
  const response = await pending;
  assert.equal(response.status(), 200, await response.text());
  await idle();
  return response.json();
};
const markFailed = (id) =>
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      `
import sys
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
client=Client(project='demo-stock-assistant',credentials=AnonymousCredentials())
ref=client.collection('alert_events').document(sys.argv[1])
row=ref.get().to_dict()
assert row['owner_uid']=='browser-test-owner'
for part in row['parts']:
 part.update(status='failed',attempts=5,uncertain=True,last_error={'code':'test_only','message':'에뮬레이터 전용 실패 상태'})
ref.update({'delivery_status':'failed','parts':row['parts']})
client.collection('alert_outbox').document(sys.argv[1]).update({'status':'failed'})
`,
      id,
    ],
    { env: { ...process.env, FIRESTORE_EMULATOR_HOST: "127.0.0.1:8080" } },
  );
try {
  await page.goto("http://localhost:5173#alerts");
  assert.equal(await page.locator("#alerts-view").isVisible(), false);
  await page.locator("#email").fill("researcher@example.test");
  await page.locator("#password").fill("browser-test-password");
  const initial = responseFor("/api/alert-events", "GET");
  await page.locator("#login-submit").click();
  const actualEvents = (await (await initial).json()).items;
  assert.ok(
    actualEvents.some((e) => e.source_url?.includes("yahoo.com")),
    "Collect real Yahoo observations into emulator history before running this suite",
  );
  await idle();
  assert.equal(await page.locator("#alerts-view").isVisible(), true);
  assert.equal(await page.locator("#alert-save").isDisabled(), true);
  await page.locator("#alert-instrument").selectOption("KRX_005930");
  await page.locator(".alert-op").first().selectOption("cross_above");
  await page.locator(".alert-price").first().fill("35000");
  await page.locator("#alert-add-step").click();
  await page.locator(".alert-op").nth(1).selectOption("lte");
  await page.locator(".alert-price").nth(1).fill("30000");
  const result = await preview();
  assert.ok(
    result.description.includes("35000") &&
      result.description.includes("30000"),
  );
  assert.equal(result.observation.delay_seconds, 1200);
  let pending = responseFor("/api/alerts", "POST");
  await page.locator("#alert-save").click();
  let response = await pending;
  assert.equal(response.status(), 201, await response.text());
  let rule = await response.json();
  const ruleId = rule.id;
  let card = page.locator(`[data-rule-id="${ruleId}"]`);
  await idle();
  await card.waitFor();
  await card.getByRole("button", { name: "수정", exact: true }).click();
  await page.locator(".alert-price").nth(1).fill("29900");
  await page.locator("#alert-reset-state").check();
  await preview();
  pending = responseFor(`/api/alerts/${ruleId}`, "PATCH");
  await page.locator("#alert-save").click();
  response = await pending;
  assert.equal(response.status(), 200, await response.text());
  rule = await response.json();
  assert.equal(rule.steps[1].price, "29900");
  await idle();
  await card.getByRole("button", { name: "일시정지", exact: true }).click();
  pending = responseFor(`/api/alerts/${ruleId}`, "PATCH");
  await page.locator("#confirm-ok").click();
  assert.equal((await (await pending).json()).active, false);
  await idle();
  pending = responseFor(`/api/alerts/${ruleId}`, "PATCH");
  await card.getByRole("button", { name: "다시 시작", exact: true }).click();
  assert.equal((await (await pending).json()).active, true);
  await idle();
  await card.getByRole("button", { name: "처음부터 다시 감시", exact: true }).click();
  pending = responseFor(`/api/alerts/${ruleId}/reset`, "POST");
  await page.locator("#confirm-ok").click();
  response = await pending;
  assert.equal(response.status(), 200, await response.text());
  await idle();
  // Advanced AND is validated against the same server limit (3 per stage).
  await page.locator("#alert-instrument").selectOption("NASDAQ_AAPL");
  await page.locator(".alert-stage-mode").selectOption("and");
  await page.locator(".alert-price").first().fill("300.10");
  await page
    .locator(".alert-stage")
    .getByRole("button", { name: "＋ 조건 추가" })
    .click();
  await page.locator(".alert-op").nth(1).selectOption("lt");
  await page.locator(".alert-price").nth(1).fill("500.25");
  await page.locator("#alert-timeout").fill("720");
  const andPreview = await preview();
  assert.equal(andPreview.rule.steps[0].all.length, 2);
  pending = responseFor("/api/alerts", "POST");
  await page.locator("#alert-save").click();
  response = await pending;
  assert.equal(response.status(), 201, await response.text());
  const andId = (await response.json()).id;
  await idle();
  // Actual historical quote detail, preserved after unrelated rule deletion.
  const historyId = actualEvents[0].id;
  let event = page.locator(`[data-event-id="${historyId}"]`);
  await event.getByRole("button", { name: "상세 보기" }).click();
  await page.locator("#alert-event-detail").waitFor();
  assert.ok(
    (await page.locator("#alert-event-detail").textContent()).includes(
      "finance.yahoo.com",
    ),
  );
  // Explicit failed-delivery state is test-only; original actual price is untouched.
  markFailed(historyId);
  pending = responseFor("/api/alert-events", "GET");
  await page.locator("#alert-events-reload").click();
  await pending;
  await idle();
  const keys = [];
  const route = `**/api/alert-events/${historyId}/retry`;
  await page.route(route, async (route) => {
    keys.push(route.request().postDataJSON().idempotency_key);
    const upstream = await route.fetch();
    assert.equal(upstream.status(), 200);
    await route.abort("failed");
  });
  await event.getByRole("button", { name: "다시 시도" }).click();
  await page.waitForFunction(() =>
    document
      .getElementById("alert-events-status")
      .textContent.includes("불확실"),
  );
  await idle();
  await page.unroute(route);
  pending = responseFor(`/api/alert-events/${historyId}/retry`, "POST");
  await event.getByRole("button", { name: "다시 시도" }).click();
  response = await pending;
  assert.equal(response.status(), 200, await response.text());
  assert.equal(response.request().postDataJSON().idempotency_key, keys[0]);
  await idle();
  markFailed(historyId);
  pending = responseFor("/api/alert-events", "GET");
  await page.locator("#alert-events-reload").click();
  await pending;
  await idle();
  pending = responseFor(`/api/alert-events/${historyId}/retry`, "POST");
  await event.getByRole("button", { name: "다시 시도" }).click();
  response = await pending;
  assert.notEqual(response.request().postDataJSON().idempotency_key, keys[0]);
  await idle();
  await mkdir("artifacts", { recursive: true });
  await page.screenshot({
    path: "artifacts/web-alerts-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: "artifacts/web-alerts-mobile.png",
    fullPage: true,
  });
  // Reload persists rules/history through the API.
  pending = responseFor("/api/alert-events", "GET");
  await page.reload();
  await pending;
  await idle();
  assert.equal(await page.locator(`[data-rule-id="${ruleId}"]`).count(), 1);
  await page.locator("#mobile-logout").click();
  await page.locator("#login-view").waitFor();
  assert.equal(await page.locator("#alerts-view").isVisible(), false);
  assert.equal(await page.locator("#alert-rule-list .alert-rule").count(), 0);
  await page.locator("#email").fill("other@example.test");
  await page.locator("#password").fill("browser-test-password");
  pending = responseFor("/api/alert-events", "GET");
  await page.locator("#login-submit").click();
  assert.deepEqual((await (await pending).json()).items, []);
  await idle();
  assert.equal(await page.locator(".alert-rule").count(), 0);
  await page.locator("#mobile-logout").click();
  await page.locator("#login-view").waitFor();
  await page.locator("#email").fill("researcher@example.test");
  await page.locator("#password").fill("browser-test-password");
  pending = responseFor("/api/alert-events", "GET");
  await page.locator("#login-submit").click();
  await pending;
  await idle();
  for (const id of [ruleId, andId]) {
    const own = page.locator(`[data-rule-id="${id}"]`);
    await own.getByRole("button", { name: "삭제", exact: true }).click();
    pending = responseFor(`/api/alerts/${id}`, "DELETE");
    await page.locator("#confirm-ok").click();
    assert.equal((await pending).status(), 204);
    await idle();
  }
  assert.ok(await page.locator(".alert-history-item").count());
  assert.deepEqual(errors, []);
  console.log(
    "alerts browser passed: real auth/API, rule CRUD + sequence/AND, pause/resume/reset, real quote history, ambiguous retry keys, reload/account isolation, desktop/mobile",
  );
} finally {
  await browser.close();
}
