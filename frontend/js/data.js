import {
  $,
  el,
  number,
  unit,
  dateTime,
  localDate,
  status,
  errorMessage,
  toast,
  confirmAction,
} from "./ui.js";
import { formatDataExport, loadExportRows } from "./data-export.js";

export function initializeData({ request, getInstrument, onInstrumentChange }) {
  let rows = [],
    cursor = null,
    summary = null,
    version = 0,
    busy = false,
    editing = null,
    createKey = null,
    formVersion = 0;
  const controls = [
    "filter-submit",
    "import-data",
    "add-data",
    "export-csv",
    "export-json",
    "load-data-more",
    "instrument",
    "start-date",
    "end-date",
  ];
  const filter = () => ({
    instrument_id: $("instrument").value,
    start_date: $("start-date").value,
    end_date: $("end-date").value,
  });
  function lock(value) {
    busy = value;
    controls.forEach((id) => {
      $(id).disabled = value;
    });
  }
  function renderSummary() {
    const s = summary || {},
      currency = unit(s.currency || getInstrument()?.currency);
    const trend =
      {
        rising: "상승",
        falling: "하락",
        flat: "보합",
        insufficient_data: "관측치 부족",
      }[s.trend] || "데이터 대기";
    const metrics = [
      [
        "관측 데이터",
        summary ? number(s.count) : "—",
        "건",
        "선택 기간 전체 기록",
      ],
      ["평균값", number(s.average), currency, "전체 관측치의 산술 평균"],
      ["최고값", number(s.maximum), currency, "선택 기간 내 최고 기록"],
      ["최저값", number(s.minimum), currency, "선택 기간 내 최저 기록"],
      [
        "최근 추세",
        trend,
        "",
        s.trend_return_pct == null
          ? "최소 21개 관측치 필요"
          : `20개 관측 간격 · ${s.trend_return_pct >= 0 ? "+" : ""}${number(s.trend_return_pct)}%`,
      ],
    ];
    $("summary-cards").replaceChildren(
      ...metrics.map(([label, value, suffix, caption]) => {
        const card = el("div", "metric");
        const val = el("div", "metric-value", value);
        val.append(el("span", "metric-unit", suffix));
        card.append(
          el("div", "metric-label", label),
          val,
          el("div", "metric-caption", caption),
        );
        return card;
      }),
    );
    $("record-count").textContent = number(s.count || 0);
    $("summary-instrument").textContent =
      s.instrument_name || getInstrument()?.name || "";
    $("summary-asof").textContent = s.data_as_of
      ? `데이터 기준 ${dateTime(s.data_as_of)}`
      : "선택 기간 전체 기준";
    $("summary-note").textContent = summary
      ? `Yahoo ${number(s.source_counts?.yahoo || 0)}건 · 직접 입력 ${number(s.source_counts?.manual || 0)}건\n사용자 수정 ${number(s.user_modified_count || 0)}건 · 중복 날짜 ${number(s.duplicate_date_count || 0)}건\nYahoo 값은 수정 종가 기준입니다. 추세는 거래일 대신 관측치 간격을 사용합니다.`
      : "데이터를 불러오면 출처와 분석 기준을 확인할 수 있습니다.";
  }
  function renderChart() {
    const holder = $("price-chart");
    holder.replaceChildren();
    $("chart-unit").textContent =
      `${unit(getInstrument()?.currency)} · 현재 목록 ${rows.length}건`;
    if (!rows.length) {
      holder.textContent = "데이터를 모으면 흐름이 보입니다.";
      return;
    }
    const ns = "http://www.w3.org/2000/svg",
      svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 600 180");
    svg.setAttribute("preserveAspectRatio", "none");
    const values = rows.map((r) => r.value),
      low = Math.min(...values),
      high = Math.max(...values),
      range = high - low || Math.max(high * 0.02, 1);
    const xy = rows.map((r, i) => [
      48 + (i / Math.max(rows.length - 1, 1)) * 540,
      145 - ((r.value - low) / range) * 118,
    ]);
    for (let i = 0; i < 3; i++) {
      const y = 27 + i * 59,
        line = document.createElementNS(ns, "line");
      for (const [key, val] of Object.entries({
        x1: 48,
        x2: 588,
        y1: y,
        y2: y,
        stroke: "#e9eee3",
        "stroke-dasharray": "4 5",
      }))
        line.setAttribute(key, val);
      const text = document.createElementNS(ns, "text");
      text.setAttribute("x", "0");
      text.setAttribute("y", y + 3);
      text.setAttribute("fill", "#9aa58f");
      text.setAttribute("font-size", "8");
      text.textContent = number(high - (i * (high - low)) / 2);
      svg.append(line, text);
    }
    const area = document.createElementNS(ns, "path");
    area.setAttribute(
      "d",
      `M${xy[0][0]},150 L${xy.map((p) => p.join(",")).join(" L")} L${xy.at(-1)[0]},150 Z`,
    );
    area.setAttribute("fill", "#e8f0de");
    area.setAttribute("opacity", ".55");
    svg.append(area);
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", xy.map((p) => p.join(",")).join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "#729855");
    line.setAttribute("stroke-width", "2");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
    [0, rows.length - 1].forEach((i, index) => {
      const text = document.createElementNS(ns, "text");
      text.setAttribute("x", index ? 588 : 48);
      text.setAttribute("y", "173");
      text.setAttribute("text-anchor", index ? "end" : "start");
      text.setAttribute("fill", "#9aa58f");
      text.setAttribute("font-size", "8");
      text.textContent = rows[i].date;
      svg.append(text);
    });
    holder.append(svg);
  }
  function renderRows() {
    $("records").replaceChildren(
      ...rows.map((row) => {
        const tr = el("tr");
        tr.append(
          el("td", "", row.date),
          el("td", "number", number(row.value)),
        );
        const source = el("td");
        source.append(
          el(
            "span",
            "source-chip",
            `${row.source === "yahoo" ? "Yahoo" : "직접 입력"}${row.user_modified ? " · 수정" : ""}`,
          ),
        );
        const actions = el("td"),
          wrap = el("div", "row-actions");
        for (const [label, action, className] of [
          ["수정", () => openRecord(row), ""],
          ["삭제", () => removeRecord(row), "delete"],
        ]) {
          const button = el("button", className, label);
          button.type = "button";
          button.disabled = busy;
          button.setAttribute("aria-label", `${row.date} 데이터 ${label}`);
          button.addEventListener("click", action);
          wrap.append(button);
        }
        actions.append(wrap);
        tr.append(source, el("td", "memo-cell", row.memo || "—"), actions);
        return tr;
      }),
    );
    $("data-empty").hidden = rows.length > 0;
    $("load-data-more").hidden = !cursor;
    $("records-label").textContent =
      `날짜 오름차순 · ${number(rows.length)}건 표시${summary ? ` / 전체 ${number(summary.count)}건` : ""}`;
    renderChart();
  }
  async function load(more = false) {
    if (busy) return;
    const query = filter();
    if (!query.instrument_id) return;
    if (query.start_date > query.end_date) {
      status("data-status", "시작일은 종료일보다 늦을 수 없습니다.", true);
      return;
    }
    const ownVersion = ++version;
    lock(true);
    status(
      "data-status",
      more ? "다음 데이터를 불러오는 중…" : "데이터와 전체 요약을 불러오는 중…",
    );
    if (!more) {
      rows = [];
      cursor = null;
      summary = null;
      renderRows();
      renderSummary();
    }
    try {
      const params = new URLSearchParams({ ...query, limit: "50" });
      if (more && cursor) params.set("cursor", cursor);
      const [page, s] = await Promise.all([
        request(`/api/data?${params}`),
        more
          ? Promise.resolve(summary)
          : request(`/api/data/summary?${new URLSearchParams(query)}`),
      ]);
      if (ownVersion !== version) return;
      rows = more ? [...rows, ...page.items] : page.items;
      cursor = page.next_cursor;
      summary = s;
      renderRows();
      renderSummary();
      status("data-status");
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError")
        status("data-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) {
        lock(false);
        renderRows();
      }
    }
  }
  function openRecord(row = null) {
    if (busy) return;
    editing = row;
    $("reload-record").hidden = true;
    createKey = crypto.randomUUID();
    $("record-dialog-title").textContent = row
      ? "분석 데이터 수정"
      : "분석 데이터 추가";
    $("record-instrument").textContent = getInstrument()?.name || "";
    $("record-currency").textContent = `(${unit(getInstrument()?.currency)})`;
    $("record-date").value = row?.date || localDate();
    $("record-value").value = row?.value ?? "";
    $("record-memo").value = row?.memo || "";
    status("record-status");
    $("record-dialog").showModal();
    $("record-date").focus();
  }
  async function saveRecord(event) {
    event.preventDefault();
    if ($("save-record").disabled) return;
    const ownVersion = version,
      ownFormVersion = ++formVersion,
      instrumentId = $("instrument").value;
    const body = {
      date: $("record-date").value,
      value: Number($("record-value").value),
      memo: $("record-memo").value,
    };
    const changed = editing;
    if (changed) body.expected_revision = changed.revision;
    else {
      body.instrument_id = instrumentId;
      body.idempotency_key = createKey;
    }
    if (!Number.isFinite(body.value) || body.value <= 0) {
      status("record-status", "0보다 큰 유효한 값을 입력하세요.", true);
      return;
    }
    [
      "save-record",
      "cancel-record",
      "close-record",
      "record-date",
      "record-value",
      "record-memo",
    ].forEach((id) => {
      $(id).disabled = true;
    });
    status("record-status", "저장하는 중…");
    try {
      await request(
        changed ? `/api/data/${encodeURIComponent(changed.id)}` : "/api/data",
        { method: changed ? "PUT" : "POST", body },
      );
      if (ownVersion !== version) return;
      $("record-dialog").close();
      toast(changed ? "데이터를 수정했습니다." : "데이터를 추가했습니다.");
      await load();
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError") {
        status("record-status", errorMessage(error), true);
        if (error.status === 409) {
          $("reload-record").hidden = false;
          status(
            "record-status",
            "다른 곳에서 기록이 변경되었습니다. 입력 내용을 복사한 뒤 최신 목록을 불러와 다시 수정하세요.",
            true,
          );
        }
      }
    } finally {
      if (ownFormVersion !== formVersion) return;
      [
        "save-record",
        "cancel-record",
        "close-record",
        "record-date",
        "record-value",
        "record-memo",
      ].forEach((id) => {
        $(id).disabled = false;
      });
    }
  }
  async function removeRecord(row) {
    if (busy) return;
    const ownVersion = version;
    if (
      !(await confirmAction(
        "이 기록을 삭제하시겠어요?",
        `${row.date} · ${number(row.value)} ${unit(getInstrument()?.currency)}\n삭제한 기록은 목록과 요약에서 제외됩니다.`,
      )) ||
      ownVersion !== version
    )
      return;
    lock(true);
    let deleted = false;
    try {
      await request(`/api/data/${encodeURIComponent(row.id)}`, {
        method: "DELETE",
      });
      if (ownVersion !== version) return;
      deleted = true;
      toast("데이터를 삭제했습니다.");
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError")
        status("data-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) {
        lock(false);
        if (deleted) await load();
      }
    }
  }
  async function importData() {
    if (busy) return;
    const ownVersion = version,
      body = filter();
    if (body.start_date > body.end_date) {
      status("data-status", "날짜 범위를 확인하세요.", true);
      return;
    }
    lock(true);
    status(
      "data-status",
      "Yahoo에서 실제 일봉을 수집하고 저장하는 중입니다. 잠시 기다려주세요…",
    );
    try {
      const result = await request("/api/data/import", {
        method: "POST",
        body,
      });
      if (ownVersion !== version) return;
      toast(
        `실제 데이터 수집 완료 · ${result.start_date} ~ ${result.end_date}`,
      );
      lock(false);
      await load();
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError")
        status("data-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lock(false);
    }
  }
  async function exportData(format) {
    if (busy || !$("filter-form").reportValidity()) return;
    const query = filter();
    if (query.start_date > query.end_date) {
      status("data-status", "시작일은 종료일보다 늦을 수 없습니다.", true);
      return;
    }
    const ownVersion = version;
    const label = format.toUpperCase();
    const instrument = {
      instrument_id: query.instrument_id,
      currency: getInstrument()?.currency,
    };
    lock(true);
    status("data-status", `${label} 파일을 준비하는 중…`);
    try {
      const records = await loadExportRows(request, query);
      if (ownVersion !== version) return;
      const content = formatDataExport(records, instrument, format);
      const type = format === "csv" ? "text/csv" : "application/json";
      const url = URL.createObjectURL(
        new Blob([content], { type: `${type};charset=utf-8` }),
      );
      const link = el("a");
      link.href = url;
      link.download = `marketnote_${query.instrument_id}_${query.start_date}_${query.end_date}.${format}`;
      link.hidden = true;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      status("data-status", `${label} 다운로드 · ${number(records.length)}건`);
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError") {
        status("data-status", errorMessage(error), true);
      }
    } finally {
      if (ownVersion === version) lock(false);
    }
  }
  $("filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    load();
  });
  $("instrument").addEventListener("change", () => {
    onInstrumentChange($("instrument").value);
    load();
  });
  $("load-data-more").addEventListener("click", () => load(true));
  $("import-data").addEventListener("click", importData);
  $("add-data").addEventListener("click", () => openRecord());
  $("export-csv").addEventListener("click", () => exportData("csv"));
  $("export-json").addEventListener("click", () => exportData("json"));
  $("record-form").addEventListener("submit", saveRecord);
  $("reload-record").addEventListener("click", () => {
    $("record-dialog").close();
    load();
  });
  ["close-record", "cancel-record"].forEach((id) =>
    $(id).addEventListener("click", () => $("record-dialog").close()),
  );
  $("record-dialog").addEventListener("cancel", (event) => {
    if ($("save-record").disabled) event.preventDefault();
  });
  // Editing a failed create is a new operation; unchanged retries retain the same key.
  $("record-form").addEventListener("input", () => {
    if (!editing) createKey = crypto.randomUUID();
  });
  return {
    load,
    reset() {
      version++;
      formVersion++;
      [
        "save-record",
        "cancel-record",
        "close-record",
        "record-date",
        "record-value",
        "record-memo",
      ].forEach((id) => {
        $(id).disabled = false;
      });
      rows = [];
      summary = null;
      cursor = null;
      lock(false);
      renderSummary();
      renderRows();
      status("data-status");
      $("record-dialog").close();
    },
  };
}
