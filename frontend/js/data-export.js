const columns = [
  "id",
  "instrument_id",
  "date",
  "value",
  "currency",
  "source",
  "price_basis",
  "source_url",
  "memo",
  "user_modified",
];

export async function loadExportRows(request, filters) {
  const params = new URLSearchParams({ ...filters, limit: "200" });
  const rows = [];
  const cursors = new Set();
  const ids = new Set();
  while (true) {
    const page = await request(`/api/data?${params}`);
    for (const row of page.items) {
      if (ids.has(row.id)) {
        throw new Error("내보내는 중 데이터가 변경됐습니다. 다시 시도하세요.");
      }
      ids.add(row.id);
      rows.push(row);
    }
    if (!page.next_cursor) return rows;
    if (cursors.has(page.next_cursor)) {
      throw new Error("다음 데이터 페이지를 확인하지 못했습니다. 다시 시도하세요.");
    }
    cursors.add(page.next_cursor);
    params.set("cursor", page.next_cursor);
  }
}

function csvCell(value) {
  if (value == null) return "";
  if (typeof value !== "string") return String(value);
  // Treat potentially formula-like text as text in spreadsheet applications.
  const text = /^[\t\r\n]|^\s*[=+\-@＝＋－＠]/u.test(value) ? `'${value}` : value;
  return `"${text.replaceAll('"', '""')}"`;
}

export function formatDataExport(rows, instrument, format) {
  const records = rows.map((row) =>
    Object.fromEntries(
      columns.map((column) => [
        column,
        row[column] ?? instrument[column] ?? null,
      ]),
    ),
  );
  if (format === "json") return JSON.stringify(records, null, 2) + "\n";

  const lines = records.map((record) =>
    columns.map((column) => csvCell(record[column])).join(","),
  );
  return "\ufeff" + [columns.join(","), ...lines, ""].join("\r\n");
}
