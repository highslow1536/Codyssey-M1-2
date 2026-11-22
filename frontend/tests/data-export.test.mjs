import assert from "node:assert/strict";
import test from "node:test";
import { formatDataExport, loadExportRows } from "../js/data-export.js";

const filters = {
  instrument_id: "NASDAQ_AAPL",
  start_date: "2025-10-01",
  end_date: "2025-10-31",
};

test("downloads every page with the same filters and preserves records on the same date", async () => {
  const pages = [
    { items: [{ id: "first", date: "2025-10-01" }], next_cursor: "a+/=" },
    { items: [{ id: "second", date: "2025-10-01" }], next_cursor: null },
  ];
  const requests = [];
  const rows = await loadExportRows((url) => {
    const request = new URL(url, "http://localhost");
    assert.equal(request.pathname, "/api/data");
    requests.push(Object.fromEntries(request.searchParams));
    return pages.shift();
  }, filters);
  assert.deepEqual(requests, [
    { ...filters, limit: "200" },
    { ...filters, limit: "200", cursor: "a+/=" },
  ]);
  assert.deepEqual(rows.map((row) => row.id), ["first", "second"]);
});

test("rejects a repeated cursor or record instead of returning an incomplete export", async () => {
  await assert.rejects(
    loadExportRows(() => ({ items: [], next_cursor: "same" }), filters),
    /다음 데이터 페이지/,
  );
  await assert.rejects(
    loadExportRows(() => ({ items: [{ id: "same" }], next_cursor: "next" }), filters),
    /데이터가 변경/,
  );
});

test("CSV quotes punctuation and line breaks, marks formula-like text, and writes a BOM", () => {
  const rows = [
    { id: "1", value: 123.45, memo: '메모, "인용"\n다음 줄', user_modified: false },
    { id: "2", value: 1, memo: "  ＝1+2", user_modified: true },
  ];
  const csv = formatDataExport(rows, { currency: "USD" }, "csv");
  assert.match(csv, /^\ufeffid,instrument_id,date,value,currency,source,price_basis,source_url,memo,user_modified\r\n/);
  assert.ok(csv.includes('"1",,,123.45,"USD",,,,"메모, ""인용""\n다음 줄",false\r\n'));
  assert.ok(csv.includes('"2",,,1,"USD",,,,"\'  ＝1+2",true\r\n'));
  assert.equal(formatDataExport([], {}, "csv").trim().split("\n").length, 1);
});

test("JSON preserves numeric and text values and excludes owner metadata", () => {
  const json = formatDataExport(
    [{ id: "1", memo: "=1+2", value: 10.5, user_modified: false, owner_uid: "private" }],
    { instrument_id: "NASDAQ_AAPL", currency: "USD" },
    "json",
  );
  assert.deepEqual(JSON.parse(json), [{
    id: "1",
    instrument_id: "NASDAQ_AAPL",
    date: null,
    value: 10.5,
    currency: "USD",
    source: null,
    price_basis: null,
    source_url: null,
    memo: "=1+2",
    user_modified: false,
  }]);
  assert.deepEqual(JSON.parse(formatDataExport([], {}, "json")), []);
});
