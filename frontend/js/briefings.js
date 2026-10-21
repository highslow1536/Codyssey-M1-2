// Discord 예약 브리핑 + 수동 미리보기 + 전송 이력 (vanilla, 의존성 없음).
// Backend contract:
//   GET  /api/briefing-schedules -> { items: [schedule], discord_configured: bool }
//   POST /api/briefing-schedules (idempotency_key 포함) -> schedule
//   PATCH /api/briefing-schedules/{id} (전체 편집 필드 + expected_revision) -> schedule
//   DELETE /api/briefing-schedules/{id} -> 204
//   POST /api/briefings/preview { market, instrument_ids } -> briefing (저장됨, 발송 없음)
//   POST /api/briefings/{id}/send { idempotency_key } -> briefing (워커 대기열 등록)
//   GET  /api/briefings?limit=20&cursor=... -> { items: [briefing], next_cursor }
//   GET  /api/briefings/{id} -> briefing 상세
// initializeBriefings({ request }) -> { setInstruments, reset, load }.
import {
  $,
  el,
  dateTime,
  status,
  errorMessage,
  safeLink,
  confirmAction,
  toast,
} from "./ui.js";
export const BRIEFING_TIMEZONES = ["Asia/Seoul", "America/New_York", "UTC"];
export const BRIEFING_WEEKDAY_LABELS = [
  "\uC6D4",
  "\uD654",
  "\uC218",
  "\uBAA9",
  "\uAE08",
  "\uD1A0",
  "\uC77C",
];
export const BRIEFING_MARKETS = ["KR", "US"];
const STATUS_LABEL = {
  preview: "미리보기",
  building: "생성 중",
  queued: "전송 대기",
  sending: "전송 중",
  sent: "전송 완료",
  failed: "전송 실패",
  cancelled: "취소됨",
};
export function normalizeWeekdays(value) {
  if (!Array.isArray(value)) return [];
  const out = [];
  for (const day of value) {
    const n = typeof day === "string" && day.trim() !== "" ? Number(day) : day;
    if (!Number.isInteger(n) || n < 0 || n > 6) continue;
    if (out.indexOf(n) === -1) out.push(n);
  }
  return out.sort(function (a, b) {
    return a - b;
  });
}
export function isValidLocalTime(value) {
  return typeof value === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}
export function defaultTimezoneForMarket(market) {
  if (market === "US") return "America/New_York";
  return "Asia/Seoul";
}
export function statusLabel(value) {
  if (Object.prototype.hasOwnProperty.call(STATUS_LABEL, value))
    return STATUS_LABEL[value];
  return String(value == null ? "알 수 없음" : value);
}
function uniqIds(ids) {
  const out = [];
  for (const id of Array.isArray(ids) ? ids : []) {
    if (out.indexOf(id) === -1) out.push(id);
  }
  return out;
}
export function buildScheduleCreateBody(input) {
  const src = input || {};
  const market = src.market === "US" ? "US" : "KR";
  const schedule_kind =
    src.schedule_kind === "fixed_time" ? "fixed_time" : "market_close";
  const close_offset_minutes = Number.isInteger(src.close_offset_minutes)
    ? src.close_offset_minutes
    : 40;
  const active = src.active !== false;
  return {
    market: market,
    instrument_ids: uniqIds(src.instrument_ids),
    schedule_kind: schedule_kind,
    timezone:
      schedule_kind === "market_close"
        ? defaultTimezoneForMarket(market)
        : src.timezone || "Asia/Seoul",
    local_time:
      schedule_kind === "market_close" ? null : src.local_time || "08:00",
    weekdays:
      schedule_kind === "market_close"
        ? [0, 1, 2, 3, 4]
        : normalizeWeekdays(src.weekdays),
    close_offset_minutes: close_offset_minutes,
    active: active,
    destination_alias: "default",
    idempotency_key: src.idempotency_key,
  };
}
export function buildScheduleUpdateBody(schedule, edits) {
  const base = schedule || {};
  const next = edits || {};
  const kind =
    next.schedule_kind !== undefined ? next.schedule_kind : base.schedule_kind;
  const merged = {
    market: next.market !== undefined ? next.market : base.market,
    instrument_ids:
      next.instrument_ids !== undefined
        ? next.instrument_ids
        : base.instrument_ids,
    schedule_kind: kind,
    timezone: next.timezone !== undefined ? next.timezone : base.timezone,
    local_time:
      next.local_time !== undefined
        ? next.local_time
        : base.local_time !== undefined
          ? base.local_time
          : null,
    weekdays:
      kind === "market_close"
        ? [0, 1, 2, 3, 4]
        : next.weekdays !== undefined
          ? next.weekdays
          : base.weekdays,
    close_offset_minutes:
      next.close_offset_minutes !== undefined
        ? next.close_offset_minutes
        : base.close_offset_minutes,
    active: next.active !== undefined ? next.active : base.active,
  };
  const body = buildScheduleCreateBody(merged);
  delete body.idempotency_key;
  body.destination_alias = "default";
  body.expected_revision =
    next.expected_revision !== undefined
      ? next.expected_revision
      : base.revision;
  return body;
}
export function describeSchedule(schedule, nameOf) {
  const s = schedule || {};
  const resolve =
    typeof nameOf === "function"
      ? nameOf
      : function (id) {
          return String(id);
        };
  const ids = Array.isArray(s.instrument_ids) ? s.instrument_ids : [];
  const targets = ids.length ? ids.map(resolve).join(", ") : "시장 전체";
  const head = (s.market || "KR") + " \u00B7 " + targets;
  if (s.schedule_kind === "fixed_time") {
    const days = normalizeWeekdays(s.weekdays)
      .map(function (d) {
        return BRIEFING_WEEKDAY_LABELS[d] || String(d);
      })
      .join("\u00B7");
    return (
      head +
      " \u00B7 지정 " +
      (s.local_time || "08:00") +
      " (" +
      (s.timezone || "-") +
      ")" +
      (days ? " \u00B7 " + days : "")
    );
  }
  return (
    head +
    " \u00B7 장 마감 +" +
    String(s.close_offset_minutes == null ? 40 : s.close_offset_minutes) +
    "분"
  );
}
let bound = false;
export function initializeBriefings(options) {
  const request = options && options.request;
  if (typeof request !== "function")
    throw new Error("request 함수가 필요합니다.");
  let instruments = [];
  let schedules = [];
  let discordConfigured = null;
  let editing = null;
  let createKey = null;
  const sendKeys = new Map();
  let previewBriefing = null;
  let previewKey = null;
  let historyItems = [];
  let historyCursor = null;
  let version = 0;
  let schedBusy = false;
  let previewBusy = false;
  let historyBusy = false;
  function nameOf(id) {
    const found = instruments.find(function (item) {
      return item.id === id;
    });
    return found ? found.name + " \u00B7 " + found.market : String(id);
  }
  function marketInstruments(market) {
    return instruments.filter(function (item) {
      return item.market === market;
    });
  }
  function lockSched(value) {
    schedBusy = value;
    document
      .querySelectorAll(
        "#briefing-schedule-form input, #briefing-schedule-form select",
      )
      .forEach((node) => {
        node.disabled = value;
      });
    if (!value) syncKindFields();
    ["briefing-save", "briefing-cancel-edit", "briefing-reload"].forEach(
      function (id) {
        const node = $(id);
        if (node) node.disabled = value;
      },
    );
    document
      .querySelectorAll("#briefing-schedule-list button")
      .forEach(function (button) {
        button.disabled = value;
      });
  }
  function lockPreview(value) {
    previewBusy = value;
    ["briefing-preview-run", "briefing-send", "briefing-send-retry"].forEach(
      function (id) {
        const node = $(id);
        if (node) node.disabled = value;
      },
    );
    const pm = $("briefing-preview-market");
    if (pm) pm.disabled = value;
    document
      .querySelectorAll("#briefing-preview-instruments input")
      .forEach(function (b) {
        b.disabled = value;
      });
    if (
      !value &&
      (!previewBriefing ||
        discordConfigured !== true ||
        !["preview", "failed"].includes(previewBriefing.delivery_status))
    ) {
      const send = $("briefing-send");
      if (send) send.disabled = true;
      const retry = $("briefing-send-retry");
      if (retry) retry.disabled = true;
    }
  }
  function lockHistory(value) {
    historyBusy = value;
    const reload = $("briefing-history-reload");
    if (reload) reload.disabled = value;
    const more = $("briefing-history-more");
    if (more) more.disabled = value;
    document
      .querySelectorAll("#briefing-history-list button")
      .forEach(function (b) {
        b.disabled = value;
      });
  }
  function resetCreateKey() {
    createKey = null;
  }
  function getCreateKey() {
    if (!createKey) createKey = crypto.randomUUID();
    return createKey;
  }
  function getSendKey(briefingId) {
    if (sendKeys.has(briefingId)) return sendKeys.get(briefingId);
    const key = crypto.randomUUID();
    sendKeys.set(briefingId, key);
    return key;
  }
  function checkedIds(fieldsetId) {
    const root = $(fieldsetId);
    if (!root) return [];
    return Array.from(
      root.querySelectorAll('input[type="checkbox"]:checked'),
    ).map(function (node) {
      return node.value;
    });
  }
  function renderCheckboxList(fieldsetId, market, selected) {
    const root = $(fieldsetId);
    if (!root) return;
    const keep = new Set(Array.isArray(selected) ? selected : []);
    root.replaceChildren(
      el("legend", "", "관심 종목 (최대 4개 · 비워두면 시장 전체)"),
    );
    const list = marketInstruments(market);
    if (!list.length) {
      root.append(el("p", "muted", "해당 시장의 종목이 없습니다."));
      return;
    }
    list.forEach(function (item) {
      const label = el("label", "check-item");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = item.id;
      box.checked = keep.has(item.id);
      box.addEventListener("change", function () {
        if (checkedIds(fieldsetId).length > 4) {
          box.checked = false;
          status(
            "briefing-schedule-status",
            "종목은 최대 4개까지 선택할 수 있습니다.",
            true,
          );
          return;
        }
        if (fieldsetId === "briefing-instruments") resetCreateKey();
      });
      label.append(box, el("span", "", item.name + " \u00B7 " + item.market));
      root.append(label);
    });
  }
  function syncKindFields() {
    const kind =
      ($("briefing-kind") && $("briefing-kind").value) || "market_close";
    const market = ($("briefing-market") && $("briefing-market").value) || "KR";
    const isClose = kind === "market_close";
    const tz = $("briefing-timezone");
    const time = $("briefing-local-time");
    const offset = $("briefing-offset");
    if (tz) {
      tz.disabled = isClose;
      if (isClose) tz.value = defaultTimezoneForMarket(market);
    }
    if (time) {
      time.disabled = isClose;
      if (!isClose && !isValidLocalTime(time.value)) time.value = "08:00";
    }
    if (offset) offset.disabled = !isClose;
    document.querySelectorAll(".briefing-weekday").forEach(function (node) {
      node.disabled = isClose;
    });
  }
  function readForm() {
    const market =
      $("briefing-market") && $("briefing-market").value === "US" ? "US" : "KR";
    const kind =
      $("briefing-kind") && $("briefing-kind").value === "fixed_time"
        ? "fixed_time"
        : "market_close";
    const timezone =
      kind === "market_close"
        ? defaultTimezoneForMarket(market)
        : ($("briefing-timezone") && $("briefing-timezone").value) ||
          "Asia/Seoul";
    const local_time =
      kind === "market_close"
        ? null
        : ($("briefing-local-time") && $("briefing-local-time").value) ||
          "08:00";
    const weekdays =
      kind === "market_close"
        ? [0, 1, 2, 3, 4]
        : normalizeWeekdays(
            Array.from(
              document.querySelectorAll(".briefing-weekday:checked"),
            ).map(function (n) {
              return Number(n.value);
            }),
          );
    const rawOffset = $("briefing-offset")
      ? Number($("briefing-offset").value)
      : 40;
    const active = $("briefing-active") ? $("briefing-active").checked : true;
    return {
      market: market,
      instrument_ids: checkedIds("briefing-instruments"),
      schedule_kind: kind,
      timezone: timezone,
      local_time: local_time,
      weekdays: weekdays,
      close_offset_minutes: Number.isInteger(rawOffset) ? rawOffset : 40,
      active: active,
    };
  }
  function validateForm(values) {
    if (values.instrument_ids.length > 4)
      return "종목은 최대 4개까지 선택할 수 있습니다.";
    for (const id of values.instrument_ids) {
      const found = instruments.find(function (item) {
        return item.id === id;
      });
      if (!found)
        return "선택한 종목을 찾을 수 없습니다. 목록을 다시 불러오세요.";
      if (found.market !== values.market)
        return "관심 종목과 시장이 일치해야 합니다.";
    }
    if (values.schedule_kind === "fixed_time") {
      if (!values.weekdays.length) return "요일을 1개 이상 선택하세요.";
      try {
        new Intl.DateTimeFormat("ko-KR", { timeZone: values.timezone });
      } catch {
        return "시간대를 확인하세요.";
      }
      if (!isValidLocalTime(values.local_time))
        return "지정 시각(HH:MM)을 확인하세요.";
    } else if (
      !Number.isInteger(values.close_offset_minutes) ||
      values.close_offset_minutes < 0 ||
      values.close_offset_minutes > 180
    ) {
      return "마감 후 전송 시점(0~180분)을 확인하세요.";
    }
    return null;
  }
  function renderSchedules() {
    const list = $("briefing-schedule-list");
    if (!list) return;
    list.replaceChildren();
    const chip = $("briefing-discord-chip");
    if (chip) {
      chip.textContent =
        discordConfigured == null
          ? ""
          : discordConfigured
            ? "Discord 설정됨"
            : "Discord 미설정";
      chip.dataset.quality = discordConfigured ? "ok" : "unavailable";
    }
    if (!schedules.length) {
      list.append(
        el(
          "p",
          "muted",
          "예약이 없습니다. 아래 양식으로 첫 예약을 만들어보세요.",
        ),
      );
      return;
    }
    schedules.forEach(function (item) {
      const card = el("article", "briefing-schedule");
      const head = el("div", "briefing-schedule-head");
      head.append(
        el("strong", "", describeSchedule(item, nameOf)),
        el("span", "chip", item.active ? "활성" : "일시정지"),
      );
      card.append(head);
      const meta = el("p", "muted");
      meta.textContent =
        "다음 실행 " +
        dateTime(item.next_run_at) +
        " \u00B7 수정버전 " +
        String(item.revision == null ? "-" : item.revision);
      card.append(meta);
      const actions = el("div", "row-actions briefing-actions");
      const edit = el("button", "", "수정");
      edit.type = "button";
      edit.addEventListener("click", function () {
        startEdit(item.id);
      });
      const toggle = el("button", "", item.active ? "일시정지" : "다시 시작");
      toggle.type = "button";
      toggle.addEventListener("click", function () {
        toggleActive(item);
      });
      const remove = el("button", "delete", "삭제");
      remove.type = "button";
      remove.addEventListener("click", function () {
        removeSchedule(item);
      });
      actions.append(edit, toggle, remove);
      card.append(actions);
      list.append(card);
    });
  }
  function fillForm(item) {
    $("briefing-market").value = item.market;
    renderCheckboxList(
      "briefing-instruments",
      item.market,
      item.instrument_ids,
    );
    $("briefing-kind").value = item.schedule_kind;
    const timezone =
      item.schedule_kind === "market_close"
        ? defaultTimezoneForMarket(item.market)
        : item.timezone;
    if (
      !Array.from($("briefing-timezone").options).some(
        (option) => option.value === timezone,
      )
    ) {
      const option = el("option", "", timezone);
      option.value = timezone;
      $("briefing-timezone").append(option);
    }
    $("briefing-timezone").value = timezone;
    $("briefing-local-time").value = item.local_time || "08:00";
    $("briefing-offset").value = String(
      item.close_offset_minutes == null ? 40 : item.close_offset_minutes,
    );
    $("briefing-active").checked = item.active !== false;
    document.querySelectorAll(".briefing-weekday").forEach(function (node) {
      node.checked =
        normalizeWeekdays(item.weekdays).indexOf(Number(node.value)) !== -1;
    });
    syncKindFields();
  }
  function startEdit(id) {
    const item = schedules.find(function (s) {
      return s.id === id;
    });
    if (!item) return;
    editing = { id: item.id, revision: item.revision };
    fillForm(item);
    resetCreateKey();
    if ($("briefing-form-title"))
      $("briefing-form-title").textContent = "예약 수정";
    if ($("briefing-cancel-edit")) $("briefing-cancel-edit").hidden = false;
    if ($("briefing-save")) $("briefing-save").textContent = "수정 저장";
    status(
      "briefing-schedule-status",
      "예약을 수정하는 중입니다. 저장하면 수정버전이 확인됩니다.",
    );
  }
  function cancelEditSilent() {
    editing = null;
    if ($("briefing-form-title"))
      $("briefing-form-title").textContent = "새 예약";
    if ($("briefing-cancel-edit")) $("briefing-cancel-edit").hidden = true;
    if ($("briefing-save")) $("briefing-save").textContent = "예약 만들기";
  }
  function cancelEdit() {
    cancelEditSilent();
    resetCreateKey();
    status("briefing-schedule-status", "");
  }
  async function loadSchedules(ownVersion) {
    lockSched(true);
    status("briefing-schedule-status", "예약을 불러오는 중…");
    try {
      const payload = await request("/api/briefing-schedules");
      if (ownVersion !== version) return;
      schedules = Array.isArray(payload && payload.items) ? payload.items : [];
      discordConfigured =
        payload && payload.discord_configured !== undefined
          ? payload.discord_configured
          : null;
      renderSchedules();
      status(
        "briefing-schedule-status",
        editing ? "예약을 수정하는 중입니다." : "",
      );
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("briefing-schedule-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockSched(false);
    }
  }
  async function saveSchedule(event) {
    if (event) event.preventDefault();
    if (schedBusy) return;
    const values = readForm();
    const invalid = validateForm(values);
    if (invalid) {
      status("briefing-schedule-status", invalid, true);
      return;
    }
    const ownVersion = version;
    lockSched(true);
    status(
      "briefing-schedule-status",
      editing ? "예약을 수정하는 중…" : "예약을 만드는 중…",
    );
    try {
      if (editing) {
        const current =
          schedules.find(function (s) {
            return s.id === editing.id;
          }) || {};
        const body = buildScheduleUpdateBody(current, {
          market: values.market,
          instrument_ids: values.instrument_ids,
          schedule_kind: values.schedule_kind,
          timezone: values.timezone,
          local_time: values.local_time,
          weekdays: values.weekdays,
          close_offset_minutes: values.close_offset_minutes,
          active: values.active,
          expected_revision: editing.revision,
        });
        const updated = await request(
          "/api/briefing-schedules/" + encodeURIComponent(editing.id),
          { method: "PATCH", body: body },
        );
        if (ownVersion !== version) return;
        schedules = schedules.map(function (s) {
          return s.id === editing.id ? updated : s;
        });
        cancelEditSilent();
        renderSchedules();
        status("briefing-schedule-status", "");
        toast("예약을 수정했습니다.");
      } else {
        const body = buildScheduleCreateBody({
          market: values.market,
          instrument_ids: values.instrument_ids,
          schedule_kind: values.schedule_kind,
          timezone: values.timezone,
          local_time: values.local_time,
          weekdays: values.weekdays,
          close_offset_minutes: values.close_offset_minutes,
          active: values.active,
          idempotency_key: getCreateKey(),
        });
        const created = await request("/api/briefing-schedules", {
          method: "POST",
          body: body,
        });
        if (ownVersion !== version) return;
        schedules = schedules.concat([created]);
        createKey = null;
        renderSchedules();
        status("briefing-schedule-status", "");
        toast("예약을 만들었습니다.");
      }
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError") {
        if (error.status === 409)
          status(
            "briefing-schedule-status",
            errorMessage(error) + " 최신 내용을 다시 불러온 뒤 확인하세요.",
            true,
          );
        else status("briefing-schedule-status", errorMessage(error), true);
      }
    } finally {
      if (ownVersion === version) lockSched(false);
    }
  }
  async function toggleActive(item) {
    if (schedBusy) return;
    if (item.active) {
      const ok = await confirmAction(
        "예약을 일시정지할까요?",
        "이후 실행만 멈추고, 이미 전송 대기열에 들어간 브리핑은 전송될 수 있습니다.",
        "일시정지",
      );
      if (!ok) return;
    }
    const ownVersion = version;
    lockSched(true);
    status(
      "briefing-schedule-status",
      item.active ? "예약을 일시정지하는 중…" : "예약을 다시 시작하는 중…",
    );
    try {
      const body = buildScheduleUpdateBody(item, {
        active: !item.active,
        expected_revision: item.revision,
      });
      const updated = await request(
        "/api/briefing-schedules/" + encodeURIComponent(item.id),
        { method: "PATCH", body: body },
      );
      if (ownVersion !== version) return;
      schedules = schedules.map(function (s) {
        return s.id === item.id ? updated : s;
      });
      if (editing && editing.id === item.id) cancelEditSilent();
      renderSchedules();
      status("briefing-schedule-status", "");
      toast(
        item.active ? "예약을 일시정지했습니다." : "예약을 다시 시작했습니다.",
      );
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("briefing-schedule-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockSched(false);
    }
  }
  async function removeSchedule(item) {
    if (schedBusy) return;
    const ok = await confirmAction(
      "예약을 삭제하시겠어요?",
      "\u201C" +
        describeSchedule(item, nameOf) +
        "\u201D 예약을 삭제합니다. 이후 실행은 멈추지만 이미 대기열에 들어간 브리핑은 전송될 수 있습니다.",
    );
    if (!ok) return;
    const ownVersion = version;
    lockSched(true);
    status("briefing-schedule-status", "예약을 삭제하는 중…");
    try {
      await request("/api/briefing-schedules/" + encodeURIComponent(item.id), {
        method: "DELETE",
      });
      if (ownVersion !== version) return;
      schedules = schedules.filter(function (s) {
        return s.id !== item.id;
      });
      if (editing && editing.id === item.id) cancelEditSilent();
      renderSchedules();
      status("briefing-schedule-status", "");
      toast("예약을 삭제했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("briefing-schedule-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockSched(false);
    }
  }
  function renderSources(holder, sources) {
    holder.replaceChildren();
    const list = Array.isArray(sources) ? sources : [];
    if (!list.length) {
      holder.append(el("p", "muted", "출처가 없습니다."));
      return;
    }
    const ul = el("ul", "briefing-sources");
    list.forEach(function (source) {
      const li = el("li", "");
      const label = source && source.label ? String(source.label) : "출처";
      const link = source && source.url ? safeLink(String(source.url)) : null;
      if (link) {
        const anchor = el("a", "", label + " \u2197");
        anchor.href = link;
        anchor.target = "_blank";
        anchor.rel = "noopener noreferrer";
        li.append(anchor);
      } else {
        li.append(el("span", "", label));
      }
      ul.append(li);
    });
    holder.append(ul);
  }
  function renderWarnings(holder, warnings) {
    holder.replaceChildren();
    const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    if (!list.length) return;
    const box = el("div", "briefing-warnings");
    box.append(el("strong", "", "참고 "));
    box.append(el("span", "", list.map(String).join(" \u00B7 ")));
    holder.append(box);
  }
  function errorDetail(err) {
    if (err == null) return "";
    if (typeof err === "object")
      return String(err.message || err.code || "오류");
    return String(err);
  }
  function formatAsOf(value) {
    const list = Array.isArray(value) ? value : value == null ? [] : [value];
    if (!list.length) return "기준 없음";
    return list
      .map(function (v) {
        return dateTime(v);
      })
      .join(" \u00B7 ");
  }
  function renderPreview(briefing) {
    previewBriefing = briefing || null;
    const result = $("briefing-preview-result");
    const send = $("briefing-send");
    const retry = $("briefing-send-retry");
    if (!result) return;
    result.replaceChildren();
    if (retry) retry.hidden = true;
    if (!briefing) {
      if (send) send.hidden = true;
      return;
    }
    const meta = el("p", "muted");
    meta.textContent =
      "생성 " +
      dateTime(briefing.generated_at) +
      " \u00B7 기준 " +
      formatAsOf(briefing.data_as_of) +
      " \u00B7 상태 " +
      statusLabel(briefing.delivery_status);
    const body = el(
      "div",
      "briefing-text",
      String(briefing.text == null ? "" : briefing.text),
    );
    const sources = el("div", "");
    renderSources(sources, briefing.sources);
    const warnings = el("div", "");
    renderWarnings(warnings, briefing.warnings);
    result.append(meta, body, sources, warnings);
    if (send) {
      send.hidden = !["preview", "failed"].includes(briefing.delivery_status);
      send.textContent = "Discord 전송 예약";
    }
  }
  async function runPreview(event) {
    if (event) event.preventDefault();
    if (previewBusy) return;
    const market =
      $("briefing-preview-market") &&
      $("briefing-preview-market").value === "US"
        ? "US"
        : "KR";
    const instrument_ids = checkedIds("briefing-preview-instruments");
    if (instrument_ids.length > 4) {
      status(
        "briefing-preview-status",
        "종목은 최대 4개까지 선택할 수 있습니다.",
        true,
      );
      return;
    }
    for (const id of instrument_ids) {
      const found = instruments.find(function (item) {
        return item.id === id;
      });
      if (!found || found.market !== market) {
        status(
          "briefing-preview-status",
          "관심 종목과 시장이 일치해야 합니다.",
          true,
        );
        return;
      }
    }
    const ownVersion = version;
    lockPreview(true);
    renderPreview(null);
    status("briefing-preview-status", "실제 캐시 데이터로 브리핑을 만드는 중…");
    try {
      const briefing = await request("/api/briefings/preview", {
        method: "POST",
        body: { market: market, instrument_ids: instrument_ids },
      });
      if (ownVersion !== version) return;
      previewKey = null;
      if (briefing && briefing.id) sendKeys.delete(briefing.id);
      renderPreview(briefing);
      status(
        "briefing-preview-status",
        "미리보기를 저장했습니다. Discord로는 보내지 않았습니다.",
      );
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("briefing-preview-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockPreview(false);
    }
  }
  async function sendPreview(isRetry) {
    if (previewBusy || !previewBriefing || !previewBriefing.id) return;
    const briefingId = previewBriefing.id;
    if (!previewKey) previewKey = getSendKey(briefingId);
    const key = previewKey;
    void isRetry;
    const ownVersion = version;
    lockPreview(true);
    status("briefing-preview-status", "전송 대기열에 등록하는 중…");
    try {
      const briefing = await request(
        "/api/briefings/" + encodeURIComponent(briefingId) + "/send",
        { method: "POST", body: { idempotency_key: key } },
      );
      if (ownVersion !== version) return;
      previewBriefing = briefing;
      sendKeys.delete(briefingId);
      previewKey = null;
      renderPreview(briefing);
      status(
        "briefing-preview-status",
        "전송 대기열에 등록했습니다. 별도 워커가 Discord로 보냅니다.",
      );
      toast("Discord 전송을 예약했습니다.");
      await loadHistoryPage(false, ownVersion);
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError") {
        if (error.status === 503)
          status(
            "briefing-preview-status",
            "Discord 웹훅이 설정되지 않았습니다. 관리자에게 전송 설정을 확인해 주세요.",
            true,
          );
        else {
          status(
            "briefing-preview-status",
            errorMessage(error) +
              " 네트워크가 애매하면 같은 키로 다시 시도해 중복 전송을 막으세요.",
            true,
          );
          const retry = $("briefing-send-retry");
          if (retry) retry.hidden = false;
        }
      }
    } finally {
      if (ownVersion === version) lockPreview(false);
    }
  }
  function partLine(part) {
    const p = part || {};
    const bits = ["상태 " + statusLabel(p.status)];
    if (Number.isInteger(p.attempts)) bits.push("시도 " + String(p.attempts));
    if (p.last_error) bits.push("사유 " + errorDetail(p.last_error));
    if (p.message_id) bits.push("메시지 " + String(p.message_id));
    if (p.uncertain) bits.push("전송 여부 불확실");
    return bits.join(" \u00B7 ");
  }
  function renderHistory() {
    const list = $("briefing-history-list");
    if (!list) return;
    list.replaceChildren();
    if (!historyItems.length) {
      list.append(
        el(
          "p",
          "muted",
          "아직 브리핑 이력이 없습니다. 미리보기나 예약 실행이 쌓이면 여기에 표시됩니다.",
        ),
      );
    }
    historyItems.forEach(function (item) {
      const card = el("article", "briefing-history-item");
      const head = el("div", "briefing-schedule-head");
      head.append(
        el(
          "strong",
          "",
          (item.market || "-") + " \u00B7 " + dateTime(item.generated_at),
        ),
        el("span", "chip", statusLabel(item.delivery_status)),
      );
      card.append(head);
      card.append(
        el(
          "p",
          "briefing-history-text",
          String(item.text == null ? "" : item.text).slice(0, 220),
        ),
      );
      const parts = Array.isArray(item.parts) ? item.parts : [];
      if (parts.length) {
        const ul = el("ul", "briefing-parts");
        parts.forEach(function (part) {
          ul.append(el("li", "muted", partLine(part)));
        });
        card.append(ul);
      }
      const actions = el("div", "row-actions briefing-actions");
      const detail = el("button", "", "상세 보기");
      detail.type = "button";
      detail.addEventListener("click", function () {
        openDetail(item.id);
      });
      actions.append(detail);
      if (item.delivery_status === "failed") {
        const retry = el("button", "", "다시 시도");
        retry.type = "button";
        retry.addEventListener("click", function () {
          retryFailed(item.id);
        });
        actions.append(retry);
      }
      card.append(actions);
      list.append(card);
    });
    const more = $("briefing-history-more");
    if (more) more.hidden = !historyCursor;
  }
  async function loadHistoryPage(more, ownVersion) {
    if (historyBusy) return;
    const useVersion = ownVersion === undefined ? version : ownVersion;
    lockHistory(true);
    if (!more) status("briefing-history-status", "이력을 불러오는 중…");
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (more && historyCursor) params.set("cursor", historyCursor);
      const page = await request("/api/briefings?" + params.toString());
      if (useVersion !== version) return;
      const items = Array.isArray(page && page.items) ? page.items : [];
      historyItems = more ? historyItems.concat(items) : items;
      historyCursor =
        page && page.next_cursor !== undefined ? page.next_cursor : null;
      renderHistory();
      status("briefing-history-status", "");
    } catch (error) {
      if (useVersion === version && error && error.name !== "AbortError")
        status("briefing-history-status", errorMessage(error), true);
    } finally {
      if (useVersion === version) lockHistory(false);
    }
  }
  async function openDetail(id) {
    const ownVersion = version;
    status("briefing-history-status", "상세를 불러오는 중…");
    try {
      const detail = await request("/api/briefings/" + encodeURIComponent(id));
      if (ownVersion !== version) return;
      const panel = $("briefing-detail");
      if (!panel) return;
      panel.hidden = false;
      panel.replaceChildren();
      const head = el("div", "section-heading");
      head.append(
        el(
          "h2",
          "",
          (detail.market || "-") + " \u00B7 " + dateTime(detail.generated_at),
        ),
      );
      const close = el("button", "icon-button", "\u00D7");
      close.type = "button";
      close.setAttribute("aria-label", "상세 닫기");
      close.addEventListener("click", function () {
        panel.hidden = true;
        panel.replaceChildren();
      });
      head.append(close);
      const meta = el("p", "muted");
      const slotBits = [];
      if (detail.schedule_id)
        slotBits.push("예약 " + String(detail.schedule_id).slice(0, 8));
      if (detail.slot) slotBits.push("슬롯 " + String(detail.slot));
      meta.textContent =
        "상태 " +
        statusLabel(detail.delivery_status) +
        " \u00B7 기준 " +
        formatAsOf(detail.data_as_of) +
        (slotBits.length ? " \u00B7 " + slotBits.join(" \u00B7 ") : "");
      const body = el(
        "div",
        "briefing-text",
        String(detail.text == null ? "" : detail.text),
      );
      const sources = el("div", "");
      renderSources(sources, detail.sources);
      const warnings = el("div", "");
      renderWarnings(warnings, detail.warnings);
      const parts = el("ul", "briefing-parts");
      (Array.isArray(detail.parts) ? detail.parts : []).forEach(
        function (part) {
          parts.append(el("li", "muted", partLine(part)));
        },
      );
      panel.append(head, meta, body, sources, warnings, parts);
      status("briefing-history-status", "");
      if (panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("briefing-history-status", errorMessage(error), true);
    }
  }
  async function retryFailed(id) {
    const target = historyItems.find(function (item) {
      return item.id === id;
    });
    if (!target || target.delivery_status !== "failed") return;
    const key = getSendKey(id);
    const ownVersion = version;
    lockHistory(true);
    status(
      "briefing-history-status",
      "실패한 전송을 같은 키로 다시 시도하는 중…",
    );
    try {
      const briefing = await request(
        "/api/briefings/" + encodeURIComponent(id) + "/send",
        { method: "POST", body: { idempotency_key: key } },
      );
      if (ownVersion !== version) return;
      historyItems = historyItems.map(function (item) {
        return item.id === id ? briefing : item;
      });
      sendKeys.delete(id);
      renderHistory();
      status("briefing-history-status", "전송 대기열에 다시 등록했습니다.");
      toast("전송을 다시 예약했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError") {
        if (error.status === 503)
          status(
            "briefing-history-status",
            "Discord 웹훅이 설정되지 않았습니다. 관리자에게 전송 설정을 확인해 주세요.",
            true,
          );
        else status("briefing-history-status", errorMessage(error), true);
      }
    } finally {
      if (ownVersion === version) lockHistory(false);
    }
  }
  async function load() {
    if (schedBusy || previewBusy || historyBusy) return;
    const ownVersion = ++version;
    renderSchedules();
    renderHistory();
    await loadSchedules(ownVersion);
    if (ownVersion !== version) return;
    await loadHistoryPage(false, ownVersion);
  }
  function setInstruments(items) {
    instruments = Array.isArray(items) ? items.slice() : [];
    const market = ($("briefing-market") && $("briefing-market").value) || "KR";
    renderCheckboxList(
      "briefing-instruments",
      market,
      checkedIds("briefing-instruments"),
    );
    const previewMarket =
      ($("briefing-preview-market") && $("briefing-preview-market").value) ||
      market;
    renderCheckboxList(
      "briefing-preview-instruments",
      previewMarket,
      checkedIds("briefing-preview-instruments"),
    );
    syncKindFields();
  }
  function reset() {
    version++;
    instruments = [];
    $("briefing-schedule-form").reset();
    $("briefing-preview-form").reset();
    renderCheckboxList("briefing-instruments", "KR", []);
    renderCheckboxList("briefing-preview-instruments", "KR", []);
    schedules = [];
    discordConfigured = null;
    createKey = null;
    sendKeys.clear();
    previewBriefing = null;
    previewKey = null;
    historyItems = [];
    historyCursor = null;
    cancelEditSilent();
    lockSched(false);
    lockPreview(false);
    lockHistory(false);
    renderSchedules();
    renderHistory();
    renderPreview(null);
    const detail = $("briefing-detail");
    if (detail) {
      detail.hidden = true;
      detail.replaceChildren();
    }
    status("briefing-schedule-status", "");
    status("briefing-preview-status", "");
    status("briefing-history-status", "");
  }
  if (!bound) {
    bound = true;
    const scheduleForm = $("briefing-schedule-form");
    if (scheduleForm) scheduleForm.addEventListener("submit", saveSchedule);
    const market = $("briefing-market");
    if (market)
      market.addEventListener("change", function () {
        resetCreateKey();
        renderCheckboxList("briefing-instruments", market.value, []);
        syncKindFields();
        if (editing)
          status(
            "briefing-schedule-status",
            "시장을 변경했습니다. 관심 종목을 다시 선택한 뒤 저장하세요.",
          );
      });
    const kind = $("briefing-kind");
    if (kind)
      kind.addEventListener("change", function () {
        resetCreateKey();
        syncKindFields();
      });
    [
      "briefing-timezone",
      "briefing-local-time",
      "briefing-offset",
      "briefing-active",
    ].forEach(function (id) {
      const node = $(id);
      if (node) node.addEventListener("change", resetCreateKey);
    });
    document.querySelectorAll(".briefing-weekday").forEach(function (node) {
      node.addEventListener("change", resetCreateKey);
    });
    const cancel = $("briefing-cancel-edit");
    if (cancel) cancel.addEventListener("click", cancelEdit);
    const reload = $("briefing-reload");
    if (reload)
      reload.addEventListener("click", function () {
        load();
      });
    const previewMarket = $("briefing-preview-market");
    if (previewMarket)
      previewMarket.addEventListener("change", function () {
        renderPreview(null);
        status("briefing-preview-status", "");
        renderCheckboxList(
          "briefing-preview-instruments",
          previewMarket.value,
          [],
        );
      });
    const previewBoxes = $("briefing-preview-instruments");
    if (previewBoxes)
      previewBoxes.addEventListener("change", function () {
        if (!previewBusy) {
          renderPreview(null);
          status("briefing-preview-status", "");
        }
      });
    const previewForm = $("briefing-preview-form");
    if (previewForm) previewForm.addEventListener("submit", runPreview);
    const send = $("briefing-send");
    if (send)
      send.addEventListener("click", function () {
        sendPreview(false);
      });
    const sendRetry = $("briefing-send-retry");
    if (sendRetry)
      sendRetry.addEventListener("click", function () {
        sendPreview(true);
      });
    const historyReload = $("briefing-history-reload");
    if (historyReload)
      historyReload.addEventListener("click", function () {
        loadHistoryPage(false, version);
      });
    const historyMore = $("briefing-history-more");
    if (historyMore)
      historyMore.addEventListener("click", function () {
        loadHistoryPage(true, version);
      });
  }
  return { setInstruments: setInstruments, reset: reset, load: load };
}
