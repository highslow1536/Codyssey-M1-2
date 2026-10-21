// Research-view browser checks against the real Auth emulators and serve_api.py.
// Real auth, real instruments and a first REAL /api/market/overview read come first
// (localhost:5173 + localhost:8000, researcher@example.test / browser-test-password).
// Only the later fixture phase replaces /api/market/overview responses with
// TEST-ONLY synthetic payloads defined below; nothing else is mocked.
// Servers are started by the main agent with a seeded Yahoo INDEX_SP500/VIX cache.
// This file starts no servers, modifies no other files and commits nothing.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const frontendUrl =
  (process.env.FRONTEND_TEST_URL || "http://localhost:5173") + "#research";

// ---------------------------------------------------------------------------
// TEST-ONLY synthetic overview payloads. Used only by the fixture phase through
// page.route for /api/market/overview. They exercise edge states without ever
// touching real Yahoo/CNN/news providers. Auth and instruments stay real.
// ---------------------------------------------------------------------------
const SP500_INSTRUMENT = {
  id: "INDEX_SP500",
  name: "S&P 500",
  yahoo_symbol: "^GSPC",
  market: "US",
  currency: "POINT",
  timezone: "America/New_York",
  calendar: "XNYS",
  delay_seconds: 0,
};
const AAPL_INSTRUMENT = {
  id: "NASDAQ_AAPL",
  name: "Apple",
  yahoo_symbol: "AAPL",
  market: "US",
  currency: "USD",
  timezone: "America/New_York",
  calendar: "XNAS",
  delay_seconds: 0,
};
const VIX_INSTRUMENT = {
  id: "INDEX_VIX",
  name: "VIX",
  yahoo_symbol: "^VIX",
  market: "US",
  currency: "POINT",
  timezone: "America/New_York",
  calendar: "XCBF",
  delay_seconds: 0,
};
function testAnalysis(instrumentId, quality, close, error) {
  return {
    instrument_id: instrumentId,
    quality_status: quality,
    error: error,
    fetched_at: "2026-10-21T12:00:00.000Z",
    event_time: "2026-10-20T20:00:00.000Z",
    data_as_of: "2026-10-20T20:00:00.000Z",
    cache_expires_at: "2026-10-21T12:30:00.000Z",
    last_attempt_at: "2026-10-21T12:00:00.000Z",
    observation_count: 120,
    latest_close: close,
    returns_pct: { 1: 0.5, 5: 1.2, 20: 3.4, 60: 8.9 },
    realized_volatility_20d_pct: 15.5,
    rsi_14: 55.5,
    sma_20: 5020.1,
    sma_60: 4880.4,
    sma_20_distance_pct: 2.0,
    sma_60_distance_pct: 5.0,
    volume_ratio_20d: 1.1,
    trend: "rising",
    calculation_notes: ["TEST-ONLY synthetic calculation note"],
    source_urls: ["https://example.test/safe-source"],
    provider: "yahoo",
    dataset: "market_bars",
    price_basis: "adjusted_close",
  };
}
function testCnn(value, rating, quality, error) {
  return {
    quality_status: quality,
    error: error,
    value: value,
    rating: rating,
    previous_close: 12,
    previous_1_week: 15,
    previous_1_month: 20,
    history: [
      {
        event_time: "2026-10-20T12:00:00.000Z",
        value: 11,
        rating: "Extreme Fear",
      },
    ],
    event_time: "2026-10-21T11:00:00.000Z",
    fetched_at: "2026-10-21T12:00:00.000Z",
    provider: "cnn",
    market: "US",
    source_url: "https://www.cnn.com/markets/fear-and-greed",
  };
}
function testNews(instrumentId, windowValue, quality, error, items, summary) {
  return {
    quality_status: quality,
    error: error,
    fetched_at: "2026-10-21T12:00:00.000Z",
    event_time: "2026-10-21T10:00:00.000Z",
    market: "US",
    instrument_id: instrumentId,
    window: windowValue,
    provider: "finnhub",
    items: items,
    summary: summary,
    truncated: false,
    coverage_note: "TEST-ONLY synthetic coverage note",
  };
}
const SAFE_ARTICLE = {
  id: "test-article-1",
  title: "TEST-ONLY Known headline for citation",
  summary: "TEST-ONLY Known summary body",
  source: "TEST-ONLY Wire",
  source_url: "https://example.test/article-1",
  published_at: "2026-10-21T10:00:00.000Z",
  content_basis: "provider_title_and_summary",
};
const XSS_ARTICLE = {
  id: "test-article-xss",
  title: "<img src=x onerror=window.xssResearch=true> TEST-ONLY markup probe",
  summary: "<script>alert(1)</script>TEST-ONLY summary probe",
  source: "TEST-ONLY Wire",
  source_url: "https://example.test/xss-probe",
  published_at: "2026-10-21T09:00:00.000Z",
  content_basis: "provider_title_and_summary",
};
const JS_LINK_ARTICLE = {
  id: "test-article-js",
  title: "TEST-ONLY javascript link probe",
  summary: "TEST-ONLY link safety probe",
  source: "TEST-ONLY Wire",
  source_url: "javascript:alert(1)",
  published_at: "2026-10-21T08:00:00.000Z",
  content_basis: "provider_title_and_summary",
};
const STALE_ITEM = {
  id: "test-article-stale",
  title: "TEST-ONLY stale headline",
  summary: "TEST-ONLY stale summary",
  source: "TEST-ONLY Wire",
  source_url: "https://example.test/stale-article",
  published_at: "2026-10-21T07:00:00.000Z",
  content_basis: "provider_title_and_summary",
};
function testOverview(instrument, analysis, cnn, vix, news) {
  return {
    instrument: instrument,
    generated_at: "2026-10-21T12:00:00.000Z",
    analysis: analysis,
    sentiment: { cnn: cnn, vix: vix },
    news: news,
  };
}
const FIXTURE_SP500_FRESH = testOverview(
  SP500_INSTRUMENT,
  testAnalysis("INDEX_SP500", "ok", 5123.45, null),
  testCnn(0, "Extreme Fear", "ok", null),
  testAnalysis("INDEX_VIX", "ok", 18.5, null),
  testNews(
    "INDEX_SP500",
    "24h",
    "ok",
    null,
    [SAFE_ARTICLE, XSS_ARTICLE, JS_LINK_ARTICLE],
    {
      method: "extractive",
      text: "TEST-ONLY synthetic digest citing [test-article-1]",
      highlights: [
        {
          article_id: "test-article-1",
          title: "TEST-ONLY Known headline for citation",
          summary: "TEST-ONLY Known summary body",
          source_url: "https://example.test/article-1",
          published_at: "2026-10-21T10:00:00.000Z",
        },
      ],
      error: {
        code: "summary_model_not_configured",
        message: "TEST-ONLY synthetic summary unavailable",
      },
    },
  ),
);
const FIXTURE_SP500_STALE = testOverview(
  SP500_INSTRUMENT,
  testAnalysis("INDEX_SP500", "stale", 5099.99, {
    code: "research_unavailable",
    message: "TEST-ONLY synthetic Yahoo delay",
  }),
  testCnn(44, "Fear", "ok", null),
  testAnalysis("INDEX_VIX", "ok", 18.5, null),
  testNews(
    "INDEX_SP500",
    "7d",
    "stale",
    {
      code: "research_unavailable",
      message: "TEST-ONLY synthetic provider failure",
    },
    [STALE_ITEM],
    {
      method: "extractive",
      text: "TEST-ONLY synthetic stale summary",
      highlights: [],
      error: null,
    },
  ),
);
const FIXTURE_AAPL_FRESH = testOverview(
  AAPL_INSTRUMENT,
  testAnalysis("NASDAQ_AAPL", "ok", 234.56, null),
  testCnn(55, "Greed", "ok", null),
  testAnalysis("INDEX_VIX", "ok", 18.5, null),
  testNews("NASDAQ_AAPL", "24h", "ok", null, [SAFE_ARTICLE], {
    method: "extractive",
    text: "TEST-ONLY synthetic AAPL digest",
    highlights: [],
    error: null,
  }),
);
const FIXTURE_AAPL_WEEK = testOverview(
  AAPL_INSTRUMENT,
  testAnalysis("NASDAQ_AAPL", "ok", 231.11, null),
  testCnn(55, "Greed", "ok", null),
  testAnalysis("INDEX_VIX", "ok", 18.5, null),
  testNews("NASDAQ_AAPL", "7d", "ok", null, [SAFE_ARTICLE], {
    method: "extractive",
    text: "TEST-ONLY synthetic AAPL week digest",
    highlights: [],
    error: null,
  }),
);
function pickFixture(instrumentId, windowValue) {
  if (instrumentId === "NASDAQ_AAPL" && windowValue === "7d")
    return FIXTURE_AAPL_WEEK;
  if (instrumentId === "NASDAQ_AAPL") return FIXTURE_AAPL_FRESH;
  if (instrumentId === "INDEX_SP500" && windowValue === "7d")
    return FIXTURE_SP500_STALE;
  return FIXTURE_SP500_FRESH;
}
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
function overviewWaiter() {
  return page.waitForResponse(
    (r) =>
      r.url().indexOf("/api/market/overview") !== -1 &&
      r.request().method() === "GET",
    { timeout: 20000 },
  );
}
async function researchIdle() {
  await page.waitForFunction(
    () => !document.getElementById("research-refresh").disabled,
  );
}
async function assertBusyResearch() {
  assert.equal(await page.locator("#research-refresh").isDisabled(), true);
  assert.equal(await page.locator("#research-instrument").isDisabled(), true);
  assert.equal(await page.locator("#research-window").isDisabled(), true);
}
async function assertNoXOverflow() {
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
}
try {
  await page.goto(frontendUrl);
  await page.waitForFunction(
    () => !document.getElementById("login-submit").disabled,
  );
  const realPending = overviewWaiter();
  await login("researcher@example.test");
  await page.locator("[data-view='research']").click();
  await page
    .locator("#research-instrument option[value='INDEX_SP500']")
    .waitFor({ state: "attached" });
  const realResp = await realPending;
  assert.equal(realResp.status(), 200);
  const realPayload = await realResp.json();
  assert.ok(realPayload.analysis);
  assert.equal(realPayload.analysis.instrument_id, "INDEX_SP500");
  assert.ok(realPayload.analysis.observation_count > 0);
  assert.equal(typeof realPayload.analysis.latest_close, "number");
  assert.ok(
    realPayload.analysis.quality_status === "ok" ||
      realPayload.analysis.quality_status === "stale",
  );
  assert.ok(realPayload.sentiment);
  assert.ok(realPayload.sentiment.vix);
  assert.equal(typeof realPayload.sentiment.vix.latest_close, "number");
  assert.equal(typeof realPayload.sentiment.cnn.value, "number");
  assert.ok(realPayload.sentiment.cnn.history.length > 0);
  assert.equal(realPayload.news.quality_status, "unavailable");
  assert.equal(realPayload.news.error.code, "news_not_configured");
  assert.equal(realPayload.news.items.length, 0);
  await researchIdle();
  await page.locator("#research-cnn .research-value").waitFor();
  assert.equal(
    await page.locator("#research-instrument").inputValue(),
    "INDEX_SP500",
  );
  assert.ok(
    (await page.locator("#research-instrument-name").textContent()).indexOf(
      "S&P 500",
    ) !== -1,
  );
  assert.ok(
    (await page.locator("#research-asof").textContent()).indexOf(
      "데이터 기준",
    ) !== -1,
  );
  assert.ok(
    (
      await page
        .locator("#research-metrics .metric-value")
        .first()
        .textContent()
    ).indexOf("—") === -1,
  );
  assert.equal(await page.locator("#research-news-count").textContent(), "0");
  assert.ok(
    (await page.locator("#research-news-items").textContent()).indexOf(
      "뉴스가 설정되지 않았습니다",
    ) !== -1,
  );
  assert.equal(await page.locator("#research-status").textContent(), "");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(artifacts, "web-research-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(artifacts, "web-research-mobile.png"),
    fullPage: true,
  });
  await assertNoXOverflow();
  await page.setViewportSize({ width: 1440, height: 1050 });
  // Actual cached research sources pass through the isolated test model and
  // survive conversation reload. Only the model text is a test double here.
  await page.locator("[data-view='chat']").click();
  await page.locator("#chat-instrument").selectOption("INDEX_SP500");
  const question = `브리핑 출처 복원 검증 ${Date.now()}`;
  await page.locator("#chat-input").fill(question);
  const chatPending = page.waitForResponse(
    (r) => r.url().endsWith("/api/chat") && r.request().method() === "POST",
  );
  await page.locator("#send-chat").click();
  const chatResponse = await chatPending;
  assert.equal(chatResponse.status(), 200, await chatResponse.text());
  const chatPayload = await chatResponse.json();
  assert.ok(chatPayload.sources.some((source) => source.provider === "cnn"));
  await page.waitForFunction(
    () => !document.getElementById("send-chat").disabled,
  );
  assert.ok((await page.locator("#messages .message-sources a").count()) >= 2);
  await page.reload();
  await page.locator("#authenticated").waitFor();
  await page
    .locator("#conversations .conversation-open")
    .filter({ hasText: question })
    .click();
  await page.locator("#messages .message-sources a").first().waitFor();
  assert.ok((await page.locator("#messages .message-sources a").count()) >= 2);
  await page.locator("[data-view='research']").click();
  await researchIdle();
  const requestedUrls = [];
  let overviewMode = "fixture";
  await page.route("**/api/market/overview*", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    requestedUrls.push(route.request().url());
    if (overviewMode === "fail") {
      await route.abort("failed");
      return;
    }
    if (overviewMode === "hang") {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try {
        await route.abort("failed");
      } catch (ignored) {
        void ignored;
      }
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
    const parsed = new URL(route.request().url());
    const picked = pickFixture(
      parsed.searchParams.get("instrument_id"),
      parsed.searchParams.get("window"),
    );
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(picked),
    });
  });
  requestedUrls.length = 0;
  const aaplPending = overviewWaiter();
  await page.locator("#research-instrument").selectOption("NASDAQ_AAPL");
  await assertBusyResearch();
  const aaplResp = await aaplPending;
  assert.equal(aaplResp.status(), 200);
  await researchIdle();
  const aaplUrl = new URL(requestedUrls[requestedUrls.length - 1]);
  assert.equal(aaplUrl.searchParams.get("instrument_id"), "NASDAQ_AAPL");
  assert.equal(aaplUrl.searchParams.get("window"), "24h");
  assert.ok(
    (await page.locator("#research-instrument-name").textContent()).indexOf(
      "Apple",
    ) !== -1,
  );
  requestedUrls.length = 0;
  const weekPending = overviewWaiter();
  await page.locator("#research-window").selectOption("7d");
  await assertBusyResearch();
  const weekResp = await weekPending;
  assert.equal(weekResp.status(), 200);
  await researchIdle();
  const weekUrl = new URL(requestedUrls[requestedUrls.length - 1]);
  assert.equal(weekUrl.searchParams.get("instrument_id"), "NASDAQ_AAPL");
  assert.equal(weekUrl.searchParams.get("window"), "7d");
  assert.ok(
    (await page.locator("#research-news-note").textContent()).indexOf(
      "기간 7d",
    ) !== -1,
  );
  requestedUrls.length = 0;
  const stalePending = overviewWaiter();
  await page.locator("#research-instrument").selectOption("INDEX_SP500");
  await assertBusyResearch();
  const staleResp = await stalePending;
  assert.equal(staleResp.status(), 200);
  const stalePayload = await staleResp.json();
  assert.equal(stalePayload.analysis.quality_status, "stale");
  assert.equal(stalePayload.analysis.error.code, "research_unavailable");
  assert.equal(stalePayload.news.quality_status, "stale");
  assert.equal(stalePayload.news.error.code, "research_unavailable");
  await researchIdle();
  assert.ok(
    (await page.locator("#research-analysis-note").textContent()).indexOf(
      "일부 지연",
    ) !== -1,
  );
  assert.ok(
    (await page.locator("#research-analysis-note").textContent()).indexOf(
      "TEST-ONLY synthetic Yahoo delay",
    ) !== -1,
  );
  requestedUrls.length = 0;
  const zeroPending = overviewWaiter();
  await page.locator("#research-window").selectOption("24h");
  await assertBusyResearch();
  const zeroResp = await zeroPending;
  assert.equal(zeroResp.status(), 200);
  const zeroPayload = await zeroResp.json();
  assert.equal(zeroPayload.sentiment.cnn.value, 0);
  await researchIdle();
  assert.equal(
    (await page.locator("#research-cnn .research-value").textContent()).trim(),
    "0",
  );
  assert.ok(
    (await page.locator("#research-news-highlights").textContent()).indexOf(
      "TEST-ONLY Known headline for citation",
    ) !== -1,
  );
  await page.locator("[data-summary-error]").waitFor({ state: "visible" });
  assert.ok(
    (await page.locator("[data-summary-error]").textContent()).indexOf(
      "summary_model_not_configured",
    ) !== -1,
  );
  assert.equal(await page.locator("#research-news-items img").count(), 0);
  assert.equal(await page.locator("#research-news-highlights img").count(), 0);
  assert.equal(await page.evaluate(() => window.xssResearch), undefined);
  assert.ok(
    (await page.locator("#research-news-items").textContent()).indexOf(
      "TEST-ONLY markup probe",
    ) !== -1,
  );
  assert.equal(
    await page.locator("#research-view a[href^='javascript:']").count(),
    0,
  );
  assert.equal(
    await page
      .locator("#research-news-items a[href='https://example.test/article-1']")
      .count(),
    1,
  );
  overviewMode = "fail";
  await page.locator("#research-instrument").selectOption("NASDAQ_AAPL");
  await researchIdle();
  await page.locator("#research-status.error").waitFor({ state: "visible" });
  assert.ok((await page.locator("#research-status").textContent()).length > 0);
  assert.equal(
    (await page.locator("#research-instrument-name").textContent()).trim(),
    "",
  );
  const failedMetrics = await page.locator("#research-metrics").textContent();
  assert.ok(failedMetrics.indexOf("5123") === -1);
  assert.ok(failedMetrics.indexOf("5,123") === -1);
  assert.ok(failedMetrics.indexOf("—") !== -1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(300);
  await assertNoXOverflow();
  await page.setViewportSize({ width: 1440, height: 1050 });
  overviewMode = "hang";
  await page.locator("#research-refresh").click();
  await page.waitForFunction(
    () => document.getElementById("research-refresh").disabled,
  );
  await page.locator("#logout").click();
  await page.locator("#login-view").waitFor({ state: "visible" });
  assert.equal(await page.locator("#research-instrument option").count(), 0);
  assert.equal(
    (await page.locator("#research-instrument-name").textContent()).trim(),
    "",
  );
  assert.equal(await page.locator("#research-news-count").textContent(), "0");
  assert.equal(await page.locator("#research-status").textContent(), "");
  assert.equal(await page.locator("#research-status.error").count(), 0);
  assert.deepEqual(errors, []);
  console.log(
    "PASS: real Yahoo SP500/VIX analysis plus missing news-key state, then test-only overview fixtures for CNN 0, stale provider errors, cited summary error, markup safety, javascript-link removal, query-param plus busy states, failed-fetch clearing, mobile overflow, logout clearing research and pending errors.",
  );
} finally {
  await browser.close();
}
