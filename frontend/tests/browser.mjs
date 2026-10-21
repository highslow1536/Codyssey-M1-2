// End-to-end checks against the real Auth + Firestore emulators and serve_api.py.
// Only that test server's model output is replaced. No auth or data routes are mocked.
import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const fixture = JSON.parse(
  await readFile(
    path.join(root, "tests/fixtures/yahoo_aapl_daily.json"),
    "utf8",
  ),
);
const browser = await chromium.launch({
  headless: true,
  ...(process.env.BROWSER_EXECUTABLE_PATH
    ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH }
    : {}),
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 1050 },
});
const page = await context.newPage();
page.setDefaultTimeout(20000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const artifacts = path.join(root, "artifacts");
await mkdir(artifacts, { recursive: true });
async function login(email) {
  await page.locator("#email").fill(email);
  await page.locator("#password").fill("browser-test-password");
  await page.locator("#login-submit").click();
  await page.locator("#authenticated").waitFor({ state: "visible" });
}
async function waitForCount(count) {
  await page.waitForFunction(
    (n) => document.getElementById("record-count").textContent === String(n),
    count,
  );
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
}
try {
  await page.goto(process.env.FRONTEND_TEST_URL || "http://localhost:5173");
  await page.waitForFunction(
    () => !document.getElementById("login-submit").disabled,
  );
  await page.screenshot({
    path: path.join(artifacts, "web-login-desktop.png"),
    fullPage: true,
  });
  await page.locator("#email").fill("researcher@example.test");
  await page.locator("#password").fill("incorrect-password");
  await page.locator("#login-submit").click();
  await page.locator("#login-status.error").waitFor();
  await login("researcher@example.test");
  await page
    .locator('#instrument option[value="NASDAQ_AAPL"]')
    .waitFor({ state: "attached" });
  await page.locator("#instrument").selectOption("NASDAQ_AAPL");
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
  await page.locator("#start-date").fill("2025-10-21");
  await page.locator("#end-date").fill("2025-10-23");
  await page.locator("#filter-submit").click();
  await waitForCount(3);
  assert.equal(await page.locator("#records tr").count(), 3);
  // Add a real observed value as a user record; it is explicitly marked manual.
  const memo = '<img src=x onerror="window.xss=true"> 브라우저 CRUD 점검';
  await page.locator("#add-data").click();
  await page.locator("#record-date").fill("2025-10-23");
  await page
    .locator("#record-value")
    .fill(String(fixture.bars[0].adjusted_close));
  await page.locator("#record-memo").fill(memo);
  await page.locator("#save-record").click();
  await waitForCount(4);
  const record = page.locator("#records tr").filter({ hasText: memo });
  assert.equal(await record.count(), 1);
  assert.equal(await record.locator("img").count(), 0);
  assert.equal(await page.evaluate(() => window.xss), undefined);
  await record.getByRole("button", { name: /수정/ }).click();
  // A second client edits the same record before this dialog is saved.
  const conflictToken = await page.evaluate(async () =>
    (await import("/js/auth.js")).getToken(),
  );
  const conflictHeaders = { Authorization: `Bearer ${conflictToken}` };
  const currentRows = await context.request.get(
    "http://localhost:8000/api/data?instrument_id=NASDAQ_AAPL&start_date=2025-10-21&end_date=2025-10-23",
    { headers: conflictHeaders },
  );
  const manual = (await currentRows.json()).items.find(
    (row) => row.memo === memo,
  );
  const concurrentEdit = await context.request.put(
    `http://localhost:8000/api/data/${manual.id}`,
    {
      headers: conflictHeaders,
      data: {
        date: manual.date,
        value: manual.value,
        memo: "다른 탭의 수정",
        expected_revision: manual.revision,
      },
    },
  );
  assert.equal(concurrentEdit.status(), 200);
  await page.locator("#save-record").click();
  await page.locator("#reload-record").waitFor({ state: "visible" });
  await page.locator("#reload-record").click();
  await page
    .locator("#records tr")
    .filter({ hasText: "다른 탭의 수정" })
    .waitFor();
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
  await page
    .locator("#records tr")
    .filter({ hasText: "다른 탭의 수정" })
    .getByRole("button", { name: /수정/ })
    .click();
  await page.locator("#record-memo").fill("수정된 메모");
  await page.locator("#save-record").click();
  await page
    .locator("#records tr")
    .filter({ hasText: "수정된 메모" })
    .waitFor();
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
  await page
    .locator("#records tr")
    .filter({ hasText: "수정된 메모" })
    .getByRole("button", { name: /삭제/ })
    .click();
  await page.locator("#confirm-ok").click();
  // Remaining deletion and reload assertions below use the actual API.
  await waitForCount(3);
  const collected = page.waitForResponse(
    (r) =>
      r.url().endsWith("/api/data/import") && r.request().method() === "POST",
    { timeout: 90000 },
  );
  await page.locator("#import-data").click();
  const imported = await collected;
  assert.equal(imported.status(), 200, await imported.text());
  await waitForCount(3);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(artifacts, "web-data-desktop.png"),
    fullPage: true,
  });
  await page.locator('[data-view="chat"]').click();
  await page.locator("#chat-input").fill("저장된 데이터의 추세를 설명해줘");
  // Simulate loss of the first completed HTTP response to verify same-ID replay.
  let lost = true;
  const chatPayloads = [];
  await page.route("**/api/chat", async (route) => {
    chatPayloads.push(route.request().postDataJSON());
    if (lost) {
      lost = false;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page.locator("#send-chat").click();
  await page.locator("#chat-error").waitFor({ state: "visible" });
  await page.locator("#retry-chat").click();
  await page.locator(".message.assistant").waitFor();
  assert.equal(chatPayloads.length, 2);
  assert.deepEqual(chatPayloads[0], chatPayloads[1]);
  assert.match(
    await page.locator(".message.assistant").textContent(),
    /실제 모델 호출이 아닙니다/,
  );
  await page.waitForFunction(
    () => !document.getElementById("send-chat").disabled,
  );
  await page.locator("#chat-input").fill("평균도 설명해줘");
  await page.locator("#send-chat").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".message.assistant").length === 2,
  );
  await page.waitForFunction(
    () => !document.getElementById("new-chat").disabled,
  );
  await page.locator("#new-chat").click();
  await page.locator(".conversation-open").first().click();
  await page.waitForFunction(
    () => document.querySelectorAll(".message").length === 4,
  );
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(artifacts, "web-chat-desktop.png"),
    fullPage: true,
  });
  await page.reload();
  await page.locator("#authenticated").waitFor({ state: "visible" });
  await page.locator(".conversation-open").first().click();
  await page.waitForFunction(
    () => document.querySelectorAll(".message").length === 4,
  );
  // Exercise actual cursor pagination with >100 saved messages.
  const token = await page.evaluate(async () =>
    (await import("/js/auth.js")).getToken(),
  );
  const apiHeaders = { Authorization: `Bearer ${token}` };
  const created = await context.request.post(
    "http://localhost:8000/api/conversations",
    {
      headers: apiHeaders,
      data: {
        title: "페이지 복원 점검",
        messages: Array.from({ length: 100 }, (_, i) => ({
          role: i % 2 ? "assistant" : "user",
          content: `페이지 검증 메시지 ${i + 1}`,
        })),
      },
    },
  );
  assert.equal(created.status(), 201);
  const longId = (await created.json()).id;
  const extraTurn = await context.request.post(
    "http://localhost:8000/api/chat",
    {
      headers: apiHeaders,
      data: {
        conversation_id: longId,
        message: "마지막 페이지 질문",
        client_request_id: crypto.randomUUID(),
        instrument_id: "NASDAQ_AAPL",
      },
    },
  );
  assert.equal(extraTurn.status(), 200);
  await page.waitForFunction(
    () => !document.getElementById("new-chat").disabled,
  );
  await page.locator("#new-chat").click();
  await page
    .locator(".conversation-open")
    .filter({ hasText: "페이지 복원 점검" })
    .first()
    .click();
  await page.waitForFunction(
    () => document.querySelectorAll(".message").length === 102,
  );
  assert.match(
    await page.locator(".message").first().textContent(),
    /페이지 검증 메시지 1$/,
  );
  assert.match(
    await page.locator(".message").nth(100).textContent(),
    /마지막 페이지 질문/,
  );
  await page.waitForFunction(
    () => !document.getElementById("new-chat").disabled,
  );
  const beforeDelete = await page.locator(".conversation-item").count();
  await page
    .getByRole("button", { name: "페이지 복원 점검 대화 삭제", exact: true })
    .first()
    .click();
  await page.locator("#confirm-ok").click();
  await page.waitForFunction(
    (n) => document.querySelectorAll(".conversation-item").length === n,
    beforeDelete - 1,
  );
  await page.waitForFunction(
    () => !document.getElementById("new-chat").disabled,
  );
  await page
    .locator(".conversation-open")
    .filter({ hasText: "저장된 데이터의 추세를 설명해줘" })
    .first()
    .click();
  await page.waitForFunction(
    () => document.querySelectorAll(".message").length === 4,
  );
  // Mobile navigation, composer and table stay within the viewport.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(artifacts, "web-chat-mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.locator('[data-view="data"]').click();
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
  await page.locator("#instrument").selectOption("NASDAQ_AAPL");
  await page.waitForFunction(
    () => !document.getElementById("filter-submit").disabled,
  );
  await page.locator("#start-date").fill("2025-10-21");
  await page.locator("#end-date").fill("2025-10-23");
  await page.locator("#filter-submit").click();
  await waitForCount(3);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(artifacts, "web-data-mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.locator("#mobile-logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(await page.locator(".message").count(), 0);
  assert.equal(await page.locator("#records tr").count(), 0);
  await login("other@example.test");
  await page
    .locator("#instrument option")
    .first()
    .waitFor({ state: "attached" });
  await page.locator('[data-view="chat"]').click();
  await page.waitForFunction(
    () => !document.getElementById("history-status").textContent,
  );
  assert.equal(await page.locator(".conversation-item").count(), 0);
  await page.locator("#mobile-logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  await page.screenshot({
    path: path.join(artifacts, "web-login-mobile.png"),
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  console.log(
    "PASS: real Firebase emulator login, real Yahoo import, CRUD + summary, safe text, chat lost-response replay, saved history reload, account isolation, desktop/mobile. Model output is a test double.",
  );
} finally {
  await browser.close();
}
