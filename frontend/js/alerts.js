// 가격 알림 규칙 + 전송 이력 (vanilla, 의존성 없음).
// GET /api/alerts, POST /api/alerts/preview, POST /api/alerts,
// PATCH/DELETE /api/alerts/{id}, POST /api/alerts/{id}/reset,
// GET /api/alert-events, GET /api/alert-events/{id}, POST /api/alert-events/{id}/retry.
// initializeAlerts({ request }) -> { setInstruments, reset, load }.
import {
  $,
  el,
  unit,
  dateTime,
  status,
  errorMessage,
  confirmAction,
  toast,
} from "./ui.js";

export const ALERT_OPS = [
  "gt",
  "gte",
  "lt",
  "lte",
  "cross_above",
  "cross_below",
];
const OP_LABEL = {
  gt: "초과",
  gte: "이상",
  lt: "미만",
  lte: "이하",
  cross_above: "상향 돌파",
  cross_below: "하향 돌파",
};
const DELIVERY_LABEL = {
  queued: "전송 대기",
  sending: "전송 중",
  sent: "전송 완료",
  failed: "전송 실패",
  cancelled: "취소됨",
};
const MONITOR_LABEL = {
  watching: "감시 중",
  paused: "일시정지",
  completed: "완료",
  expired: "만료",
  stale: "관측 지연",
  unavailable: "관측 불가",
  waiting: "첫 관측 대기",
  armed: "다음 단계 감시",
  market_closed: "정규장 대기",
  corporate_action: "기업행위 확인 필요",
};
const PRICE_RE = /^(0|[1-9][0-9]*)([.][0-9]+)?$/;

export function opLabel(op) {
  if (Object.prototype.hasOwnProperty.call(OP_LABEL, op)) return OP_LABEL[op];
  return String(op == null ? "알 수 없음" : op);
}
export function deliveryLabel(value) {
  if (Object.prototype.hasOwnProperty.call(DELIVERY_LABEL, value))
    return DELIVERY_LABEL[value];
  return String(value == null ? "알 수 없음" : value);
}
export function monitorLabel(value) {
  if (Object.prototype.hasOwnProperty.call(MONITOR_LABEL, value))
    return MONITOR_LABEL[value];
  return String(value == null ? "알 수 없음" : value);
}
export function isValidPriceDecimal(value) {
  if (typeof value !== "string") return false;
  const t = value.trim();
  if (!PRICE_RE.test(t)) return false;
  const n = Number(t);
  return Number.isFinite(n) && n > 0;
}
export function formatPrice(priceDecimal, currency) {
  const price = priceDecimal == null ? "-" : String(priceDecimal);
  const suffix = unit(currency);
  return suffix ? price + " " + suffix : price;
}
function normalizeCondition(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    op: typeof s.op === "string" ? s.op : "",
    price: typeof s.price === "string" ? s.price.trim() : "",
  };
}
function normalizeStep(raw) {
  const s = raw && typeof raw === "object" ? raw : {};
  if (Array.isArray(s.all)) return { all: s.all.map(normalizeCondition) };
  return normalizeCondition(s);
}
export function normalizeSteps(rawSteps) {
  if (!Array.isArray(rawSteps)) return [];
  return rawSteps.map(normalizeStep);
}
export function buildRuleValues(input) {
  const src = input && typeof input === "object" ? input : {};
  const rawExpires =
    typeof src.expires_at === "string" ? src.expires_at.trim() : "";
  const rawTimeout = src.step_timeout_hours;
  let timeout = null;
  if (
    rawTimeout !== null &&
    rawTimeout !== undefined &&
    String(rawTimeout).trim() !== ""
  ) {
    const n = Number(String(rawTimeout).trim());
    timeout = Number.isInteger(n) ? n : NaN;
  }
  return {
    instrument_id:
      typeof src.instrument_id === "string" ? src.instrument_id : "",
    currency: typeof src.currency === "string" ? src.currency : "",
    steps: normalizeSteps(src.steps),
    session_scope: "regular",
    repeat_mode: src.repeat_mode === "repeat" ? "repeat" : "once",
    expires_at: rawExpires === "" ? null : rawExpires,
    step_timeout_hours: timeout,
    active: src.active !== false,
  };
}
export function validateRuleValues(values, instruments) {
  const v = values && typeof values === "object" ? values : {};
  const list = Array.isArray(instruments) ? instruments : [];
  if (!v.instrument_id) return "종목을 선택하세요.";
  if (
    list.length &&
    !list.some(function (it) {
      return it.id === v.instrument_id;
    })
  )
    return "선택한 종목을 찾을 수 없습니다. 목록을 다시 불러오세요.";
  if (!Array.isArray(v.steps) || v.steps.length < 1 || v.steps.length > 3)
    return "조건 단계는 1개 이상 3개 이하로 입력하세요.";
  for (let i = 0; i < v.steps.length; i++) {
    const st = v.steps[i] || {};
    const conds = Array.isArray(st.all) ? st.all : [st];
    if (Array.isArray(st.all) && st.all.length < 2)
      return i + 1 + "단계의 AND 묶음은 조건을 2개 이상 입력하세요.";
    if (conds.length > 3) return i + 1 + "단계의 조건은 3개 이하로 입력하세요.";
    for (const c of conds) {
      if (ALERT_OPS.indexOf(c.op) === -1)
        return i + 1 + "단계의 연산자를 선택하세요.";
      if (!isValidPriceDecimal(c.price || ""))
        return i + 1 + "단계의 가격을 0보다 큰 숫자로 입력하세요.";
    }
  }
  if (v.repeat_mode !== "once" && v.repeat_mode !== "repeat")
    return "반복 방식을 선택하세요.";
  if (v.expires_at !== null && v.expires_at !== undefined) {
    const t = new Date(v.expires_at).valueOf();
    if (Number.isNaN(t)) return "만료 시각을 확인하세요.";
    if (t <= Date.now()) return "만료 시각은 현재 이후로 입력하세요.";
  }
  if (v.step_timeout_hours !== null && v.step_timeout_hours !== undefined) {
    if (
      !Number.isInteger(v.step_timeout_hours) ||
      v.step_timeout_hours < 1 ||
      v.step_timeout_hours > 720
    )
      return "단계 제한 시간은 1~720시간 사이의 정수로 입력하세요.";
  }
  return null;
}
export function buildRuleCreateBody(values, idempotencyKey) {
  return {
    instrument_id: values.instrument_id,
    currency: values.currency,
    steps: values.steps,
    session_scope: "regular",
    repeat_mode: values.repeat_mode,
    expires_at: values.expires_at,
    step_timeout_hours: values.step_timeout_hours,
    active: values.active,
    idempotency_key: idempotencyKey,
  };
}
export function buildRuleUpdateBody(rule, values, options) {
  const o = options && typeof options === "object" ? options : {};
  return {
    instrument_id: values.instrument_id,
    currency: values.currency,
    steps: values.steps,
    session_scope: "regular",
    repeat_mode: values.repeat_mode,
    expires_at: values.expires_at,
    step_timeout_hours: values.step_timeout_hours,
    active: values.active,
    expected_revision:
      o.expected_revision !== undefined
        ? o.expected_revision
        : rule && rule.revision,
    reset_state: o.reset_state === true,
  };
}
function sameCondition(a, b) {
  return (
    (a && a.op) === (b && b.op) &&
    String(a && a.price != null ? a.price : "").trim() ===
      String(b && b.price != null ? b.price : "").trim()
  );
}
function sameStep(a, b) {
  const aa = a && typeof a === "object" ? a : {};
  const bb = b && typeof b === "object" ? b : {};
  const aAll = Array.isArray(aa.all) ? aa.all : null;
  const bAll = Array.isArray(bb.all) ? bb.all : null;
  if ((aAll == null) !== (bAll == null)) return false;
  if (aAll && bAll) {
    if (aAll.length !== bAll.length) return false;
    return aAll.every(function (c, i) {
      return sameCondition(c, bAll[i]);
    });
  }
  return sameCondition(aa, bb);
}
export function ruleConditionsChanged(rule, values) {
  const r = rule && typeof rule === "object" ? rule : {};
  const v = values && typeof values === "object" ? values : {};
  if ((r.instrument_id || "") !== (v.instrument_id || "")) return true;
  if ((r.repeat_mode || "once") !== (v.repeat_mode || "once")) return true;
  if (
    (r.step_timeout_hours == null ? null : r.step_timeout_hours) !==
    (v.step_timeout_hours == null ? null : v.step_timeout_hours)
  )
    return true;
  const rs = Array.isArray(r.steps) ? r.steps : [];
  const vs = Array.isArray(v.steps) ? v.steps : [];
  if (rs.length !== vs.length) return true;
  return rs.some(function (s, i) {
    return !sameStep(s, vs[i]);
  });
}
export function fingerprintValues(values) {
  return JSON.stringify(values != null ? values : null);
}
export function describeStep(step, currency) {
  const s = step && typeof step === "object" ? step : {};
  const suffix = unit(currency);
  const tail = suffix ? " " + suffix : "";
  const conds = Array.isArray(s.all) ? s.all : [s];
  const parts = conds.map(function (c) {
    const cc = c && typeof c === "object" ? c : {};
    return (
      "가격 " +
      String(cc.price != null ? cc.price : "-") +
      tail +
      " " +
      opLabel(cc.op)
    );
  });
  if (parts.length > 1) return "(" + parts.join(" 그리고 ") + ")";
  return parts[0] || "-";
}
export function describeRule(rule, nameOf) {
  const r = rule && typeof rule === "object" ? rule : {};
  const resolve =
    typeof nameOf === "function"
      ? nameOf
      : function (id) {
          return String(id);
        };
  const steps = Array.isArray(r.steps) ? r.steps : [];
  const seq = steps
    .map(function (s, i) {
      return String(i + 1) + "단계 " + describeStep(s, r.currency);
    })
    .join(" → ");
  return (
    resolve(r.instrument_id) +
    " · " +
    seq +
    " · " +
    (r.repeat_mode === "repeat" ? "반복" : "1회")
  );
}
export function stageSummary(rule) {
  const r = rule && typeof rule === "object" ? rule : {};
  const st = r.state && typeof r.state === "object" ? r.state : null;
  if (!st) return "대기 중";
  if (st.completed === true) return "완료";
  const total = Array.isArray(r.steps) ? r.steps.length : 0;
  const stage = Number.isInteger(st.stage) ? st.stage : 0;
  const cycle = Number.isInteger(st.cycle) ? st.cycle : 0;
  return (
    "단계 " +
    String(Math.min(stage + 1, Math.max(total, 1))) +
    "/" +
    String(Math.max(total, 1)) +
    " · 주기 " +
    String(cycle + 1)
  );
}
export function isAmbiguousError(error) {
  if (!error || typeof error !== "object") return true;
  if (error.code === "network_error" || error.code === "response_error")
    return true;
  return !(
    Number.isInteger(error.status) &&
    error.status >= 400 &&
    error.status < 500
  );
}
let bound = false;
export function initializeAlerts(options) {
  const request = options && options.request;
  if (typeof request !== "function")
    throw new Error("request 함수가 필요합니다.");
  let instruments = [];
  let rules = [];
  let discordConfigured = null;
  let editing = null;
  let createKey = null;
  let createFingerprint = null;
  let detailSequence = 0;
  let previewFingerprint = null;
  let events = [];
  let eventsCursor = null;
  let version = 0;
  let listBusy = false;
  let formBusy = false;
  let previewBusy = false;
  let historyBusy = false;
  const retryKeys = new Map();
  function nameOf(id) {
    const found = instruments.find(function (it) {
      return it.id === id;
    });
    return found ? found.name + " · " + found.market : String(id);
  }
  function instrumentOf(id) {
    return (
      instruments.find(function (it) {
        return it.id === id;
      }) || null
    );
  }
  function getCreateKey() {
    if (!createKey) createKey = crypto.randomUUID();
    return createKey;
  }
  function getRetryKey(id) {
    if (!retryKeys.has(id)) retryKeys.set(id, crypto.randomUUID());
    return retryKeys.get(id);
  }
  function updateCurrencyNote() {
    const note = $("alert-currency");
    const sel = $("alert-instrument");
    if (!note || !sel) return;
    const inst = instrumentOf(sel.value);
    note.textContent =
      inst && inst.currency ? inst.currency + " · " + unit(inst.currency) : "-";
  }
  function toLocalInputValue(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.valueOf())) return "";
    const p = function (n) {
      return String(n).padStart(2, "0");
    };
    return (
      d.getFullYear() +
      "-" +
      p(d.getMonth() + 1) +
      "-" +
      p(d.getDate()) +
      "T" +
      p(d.getHours()) +
      ":" +
      p(d.getMinutes())
    );
  }
  function syncControls() {
    const busy = listBusy || formBusy || previewBusy;
    const form = $("alert-form");
    if (form)
      form.querySelectorAll("input, select, button").forEach((n) => {
        n.disabled = busy;
      });
    const list = $("alert-rule-list");
    if (list)
      list.querySelectorAll("button").forEach((n) => {
        n.disabled = busy;
      });
    const save = $("alert-save");
    if (save) save.disabled = busy || previewFingerprint == null;
    const add = $("alert-add-step");
    if (add) add.disabled = busy || $("alert-steps").children.length >= 3;
  }
  function lockList(value) {
    listBusy = value;
    syncControls();
  }
  function lockForm(value) {
    formBusy = value;
    syncControls();
  }
  function lockPreview(value) {
    previewBusy = value;
    syncControls();
  }
  function syncSave() {
    syncControls();
  }
  function lockHistory(value) {
    historyBusy = value;
    for (const id of ["alert-events-reload", "alert-events-more"]) {
      const node = $(id);
      if (node) node.disabled = value;
    }
    const list = $("alert-events-list");
    if (list)
      list.querySelectorAll("button").forEach((n) => {
        n.disabled = value;
      });
  }
  function markPreviewStale() {
    if (previewFingerprint == null || formBusy) return;
    previewFingerprint = null;
    renderPreviewResult(null);
    status(
      "alert-form-status",
      "조건이 바뀌었습니다. 저장 전에 미리보기를 다시 실행하세요.",
    );
    syncSave();
  }
  function conditionRow(cond, removable) {
    const c = cond && typeof cond === "object" ? cond : {};
    const row = el("div", "alert-cond-row");
    const op = document.createElement("select");
    op.className = "alert-op";
    op.setAttribute("aria-label", "연산자");
    ALERT_OPS.forEach(function (o) {
      const opt = el("option", "", opLabel(o));
      opt.value = o;
      op.append(opt);
    });
    op.value = ALERT_OPS.indexOf(c.op) === -1 ? "gte" : c.op;
    const price = document.createElement("input");
    price.type = "text";
    price.className = "alert-price";
    price.inputMode = "decimal";
    price.placeholder = "예: 35000.50";
    price.setAttribute("aria-label", "가격");
    price.value = typeof c.price === "string" ? c.price : "";
    row.append(op, price);
    if (removable) {
      const del = el("button", "icon-button", "×");
      del.type = "button";
      del.setAttribute("aria-label", "조건 삭제");
      del.addEventListener("click", function () {
        row.remove();
        markPreviewStale();
      });
      row.append(del);
    }
    return row;
  }
  function stageCard(step, index, total) {
    const s = step && typeof step === "object" ? step : {};
    const isAnd = Array.isArray(s.all);
    const card = el("div", "alert-stage");
    const head = el("div", "alert-stage-head");
    head.append(el("strong", "", "단계 " + (index + 1)));
    const mode = document.createElement("select");
    mode.className = "alert-stage-mode";
    mode.setAttribute("aria-label", "단계 " + (index + 1) + " 방식");
    [
      ["simple", "단순 조건"],
      ["and", "AND 묶음(고급)"],
    ].forEach(function (pair) {
      const opt = el("option", "", pair[1]);
      opt.value = pair[0];
      mode.append(opt);
    });
    mode.value = isAnd ? "and" : "simple";
    mode.addEventListener("change", function () {
      renderStepsEditor(readSteps());
      markPreviewStale();
    });
    head.append(mode);
    if (total > 1) {
      const del = el("button", "icon-button", "×");
      del.type = "button";
      del.setAttribute("aria-label", "단계 " + (index + 1) + " 삭제");
      del.addEventListener("click", function () {
        const cur = readSteps();
        cur.splice(index, 1);
        renderStepsEditor(cur);
        markPreviewStale();
      });
      head.append(del);
    }
    card.append(head);
    if (isAnd) {
      const conds = s.all.length
        ? s.all
        : [
            { op: "gte", price: "" },
            { op: "gte", price: "" },
          ];
      const box = el("div", "alert-conds");
      conds.forEach(function (c) {
        box.append(conditionRow(c, true));
      });
      const add = el("button", "button secondary", "＋ 조건 추가");
      add.type = "button";
      add.addEventListener("click", function () {
        if (box.children.length >= 3) return;
        box.append(conditionRow({ op: "gte", price: "" }, true));
        markPreviewStale();
      });
      card.append(box, add);
    } else {
      card.append(conditionRow(normalizeCondition(s), false));
    }
    return card;
  }
  function renderStepsEditor(steps) {
    const holder = $("alert-steps");
    if (!holder) return;
    const list =
      Array.isArray(steps) && steps.length ? steps : [{ op: "gte", price: "" }];
    holder.replaceChildren();
    list.forEach(function (s, i) {
      holder.append(stageCard(s, i, list.length));
    });
    const add = $("alert-add-step");
    if (add) add.disabled = list.length >= 3 || formBusy;
  }
  function readSteps() {
    const holder = $("alert-steps");
    if (!holder) return [];
    return Array.from(holder.children)
      .filter(function (n) {
        return n.classList && n.classList.contains("alert-stage");
      })
      .map(function (card) {
        const mode = card.querySelector(".alert-stage-mode");
        if (mode && mode.value === "and") {
          return {
            all: Array.from(card.querySelectorAll(".alert-cond-row")).map(
              function (row) {
                return {
                  op: row.querySelector(".alert-op").value,
                  price: row.querySelector(".alert-price").value.trim(),
                };
              },
            ),
          };
        }
        return {
          op: card.querySelector(".alert-op").value,
          price: card.querySelector(".alert-price").value.trim(),
        };
      });
  }
  function readForm() {
    const sel = $("alert-instrument");
    const instrumentId = sel ? sel.value : "";
    const inst = instrumentOf(instrumentId);
    const exp = $("alert-expires");
    let expires = null;
    if (exp && String(exp.value).trim() !== "") {
      const t = new Date(exp.value).valueOf();
      expires = Number.isNaN(t)
        ? String(exp.value).trim()
        : new Date(exp.value).toISOString();
    }
    const to = $("alert-timeout");
    return buildRuleValues({
      instrument_id: instrumentId,
      currency: inst ? inst.currency || "" : "",
      steps: readSteps(),
      repeat_mode: $("alert-repeat") ? $("alert-repeat").value : "once",
      expires_at: expires,
      step_timeout_hours:
        to && String(to.value).trim() !== "" ? String(to.value).trim() : null,
      active: $("alert-active") ? $("alert-active").checked : true,
    });
  }
  function fillForm(rule) {
    const form = $("alert-form");
    if (!form) return;
    form.reset();
    const sel = $("alert-instrument");
    if (sel && rule.instrument_id) sel.value = rule.instrument_id;
    updateCurrencyNote();
    const rep = $("alert-repeat");
    if (rep) rep.value = rule.repeat_mode === "repeat" ? "repeat" : "once";
    const exp = $("alert-expires");
    if (exp)
      exp.value = rule.expires_at ? toLocalInputValue(rule.expires_at) : "";
    const to = $("alert-timeout");
    if (to)
      to.value =
        rule.step_timeout_hours == null ? "" : String(rule.step_timeout_hours);
    const act = $("alert-active");
    if (act) act.checked = rule.active !== false;
    renderStepsEditor(rule.steps);
    editing = { id: rule.id, revision: rule.revision };
    const title = $("alert-form-title");
    if (title) title.textContent = "규칙 수정";
    const cancel = $("alert-cancel-edit");
    if (cancel) cancel.hidden = false;
    const save = $("alert-save");
    if (save) save.textContent = "수정 저장";
    const resetRow = $("alert-reset-state-row");
    if (resetRow) resetRow.hidden = false;
    const resetBox = $("alert-reset-state");
    if (resetBox) resetBox.checked = false;
    previewFingerprint = null;
    renderPreviewResult(null);
    syncSave();
    status(
      "alert-form-status",
      "규칙을 수정하는 중입니다. 미리보기를 다시 실행한 뒤 저장하세요.",
    );
  }
  function clearCreateForm() {
    const form = $("alert-form");
    if (form) form.reset();
    updateCurrencyNote();
    renderStepsEditor([{ op: "cross_above", price: "" }]);
    editing = null;
    createKey = null;
    const title = $("alert-form-title");
    if (title) title.textContent = "새 규칙";
    const cancel = $("alert-cancel-edit");
    if (cancel) cancel.hidden = true;
    const save = $("alert-save");
    if (save) save.textContent = "미리보기 후 저장";
    const resetRow = $("alert-reset-state-row");
    if (resetRow) resetRow.hidden = true;
    const resetBox = $("alert-reset-state");
    if (resetBox) resetBox.checked = false;
    previewFingerprint = null;
    renderPreviewResult(null);
    syncSave();
  }
  function cancelEditSilent() {
    editing = null;
    const title = $("alert-form-title");
    if (title) title.textContent = "새 규칙";
    const cancel = $("alert-cancel-edit");
    if (cancel) cancel.hidden = true;
    const save = $("alert-save");
    if (save) save.textContent = "미리보기 후 저장";
    const resetRow = $("alert-reset-state-row");
    if (resetRow) resetRow.hidden = true;
  }
  function errorText(err) {
    if (err == null) return "";
    if (typeof err === "object")
      return String(err.message || err.code || "오류");
    return String(err);
  }
  function renderRules() {
    const list = $("alert-rule-list");
    if (!list) return;
    list.replaceChildren();
    const chip = $("alert-discord-chip");
    if (chip) {
      chip.textContent =
        discordConfigured == null
          ? ""
          : discordConfigured
            ? "Discord 설정됨"
            : "Discord 미설정";
      chip.dataset.quality = discordConfigured ? "ok" : "unavailable";
    }
    if (!rules.length) {
      list.append(
        el(
          "p",
          "muted",
          "규칙이 없습니다. 아래 양식으로 첫 규칙을 만들어보세요.",
        ),
      );
      return;
    }
    rules.forEach(function (rule) {
      const card = el("article", "alert-rule");
      card.dataset.ruleId = rule.id;
      const head = el("div", "alert-rule-head");
      head.append(el("strong", "", describeRule(rule, nameOf)));
      head.append(el("span", "chip", rule.active ? "활성" : "일시정지"));
      head.append(el("span", "chip", monitorLabel(rule.monitor_status)));
      card.append(head);
      const meta = el("p", "muted");
      let metaText =
        "수정버전 " +
        String(rule.revision == null ? "-" : rule.revision) +
        " · 갱신 " +
        dateTime(rule.updated_at);
      if (rule.expires_at) metaText += " · 만료 " + dateTime(rule.expires_at);
      if (rule.step_timeout_hours != null)
        metaText += " · 단계 제한 " + String(rule.step_timeout_hours) + "시간";
      meta.textContent = metaText;
      card.append(meta);
      const st =
        rule.state && typeof rule.state === "object" ? rule.state : null;
      const prog = el("p", "alert-progress");
      let progText = stageSummary(rule);
      if (st && st.last_price_decimal != null)
        progText +=
          " · 마지막 " + formatPrice(st.last_price_decimal, rule.currency);
      if (st && st.last_event_time)
        progText += " · " + dateTime(st.last_event_time);
      prog.textContent = progText;
      card.append(prog);
      if (st && st.armed_at)
        card.append(
          el("p", "muted", "현재 단계 시작 " + dateTime(st.armed_at)),
        );
      if (st && Array.isArray(st.evidence) && st.evidence.length) {
        const evidence = el("ul", "alert-parts");
        st.evidence.forEach((item, index) => {
          evidence.append(
            el(
              "li",
              "muted",
              `${index + 1}단계 확인 · ${formatPrice(item.price_decimal, rule.currency)} · ${dateTime(item.event_time)}`,
            ),
          );
        });
        card.append(evidence);
      }
      if (rule.last_error) {
        const err = el("p", "alert-error");
        err.textContent = "오류 " + errorText(rule.last_error);
        card.append(err);
      }
      const actions = el("div", "row-actions alert-actions");
      const editBtn = el("button", "", "수정");
      editBtn.type = "button";
      editBtn.addEventListener("click", function () {
        fillForm(rule);
      });
      const toggleBtn = el(
        "button",
        "",
        rule.active ? "일시정지" : "다시 시작",
      );
      toggleBtn.type = "button";
      toggleBtn.addEventListener("click", function () {
        toggleActive(rule);
      });
      const resetBtn = el("button", "", "처음부터 다시 감시");
      resetBtn.type = "button";
      resetBtn.addEventListener("click", function () {
        resetRule(rule);
      });
      const delBtn = el("button", "delete", "삭제");
      delBtn.type = "button";
      delBtn.addEventListener("click", function () {
        removeRule(rule);
      });
      actions.append(editBtn, toggleBtn, resetBtn, delBtn);
      card.append(actions);
      list.append(card);
    });
  }
  function valuesFromRule(rule) {
    return {
      instrument_id: rule.instrument_id || "",
      currency: rule.currency || "",
      steps: normalizeSteps(rule.steps),
      session_scope: "regular",
      repeat_mode: rule.repeat_mode === "repeat" ? "repeat" : "once",
      expires_at: rule.expires_at == null ? null : String(rule.expires_at),
      step_timeout_hours:
        rule.step_timeout_hours == null ? null : rule.step_timeout_hours,
      active: rule.active !== false,
    };
  }
  function renderPreviewResult(result) {
    const holder = $("alert-preview-result");
    if (!holder) return;
    holder.replaceChildren();
    if (!result) return;
    const desc = el(
      "div",
      "alert-preview-text",
      String(result.description == null ? "" : result.description),
    );
    holder.append(desc);
    const obs =
      result.observation && typeof result.observation === "object"
        ? result.observation
        : null;
    if (obs) {
      const meta = el("p", "muted");
      let bits =
        "관측 주기 " +
        String(obs.interval_minutes == null ? 5 : obs.interval_minutes) +
        "분 완성 봉 종가";
      bits +=
        " · " +
        (obs.session_scope === "regular"
          ? "정규장"
          : String(obs.session_scope || "정규장"));
      if (obs.delay_seconds != null)
        bits += " · 지연 약 " + String(obs.delay_seconds) + "초";
      meta.textContent = bits;
      holder.append(meta);
    }
    const warnings = Array.isArray(result.warnings)
      ? result.warnings.filter(Boolean)
      : [];
    if (warnings.length) {
      const box = el("div", "alert-warnings");
      box.append(el("strong", "", "참고 "));
      box.append(el("span", "", warnings.map(String).join(" · ")));
      holder.append(box);
    }
  }
  async function runPreview() {
    if (listBusy || previewBusy || formBusy) return;
    const values = readForm();
    const invalid = validateRuleValues(values, instruments);
    if (invalid) {
      status("alert-form-status", invalid, true);
      return;
    }
    const ownVersion = version;
    lockPreview(true);
    renderPreviewResult(null);
    status("alert-form-status", "조건을 확인하는 중…");
    try {
      const result = await request("/api/alerts/preview", {
        method: "POST",
        body: values,
      });
      if (ownVersion !== version) return;
      previewFingerprint = fingerprintValues(values);
      renderPreviewResult(result);
      status(
        "alert-form-status",
        "미리보기입니다. 저장하면 감시가 시작됩니다.",
      );
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-form-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockPreview(false);
    }
  }
  async function saveRule(event) {
    if (event) event.preventDefault();
    if (listBusy || formBusy || previewBusy) return;
    const values = readForm();
    const invalid = validateRuleValues(values, instruments);
    if (invalid) {
      status("alert-form-status", invalid, true);
      return;
    }
    if (
      previewFingerprint == null ||
      previewFingerprint !== fingerprintValues(values)
    ) {
      status(
        "alert-form-status",
        "조건을 확인한 뒤 저장하세요. 미리보기를 먼저 실행하세요.",
        true,
      );
      return;
    }
    if (editing) {
      const current = rules.find(function (r) {
        return r.id === editing.id;
      });
      if (!current) {
        status(
          "alert-form-status",
          "수정 중인 규칙을 찾을 수 없습니다. 다시 불러오세요.",
          true,
        );
        return;
      }
      const changed = ruleConditionsChanged(current, values);
      const resetBox = $("alert-reset-state");
      const wantReset = resetBox ? resetBox.checked : false;
      if (changed && !wantReset) {
        status(
          "alert-form-status",
          "조건이 바뀌었습니다. 진행 상태 초기화에 체크한 뒤 저장하세요.",
          true,
        );
        return;
      }
    }
    const ownVersion = version;
    if (!editing && createFingerprint !== fingerprintValues(values)) {
      createKey = null;
      createFingerprint = fingerprintValues(values);
    }
    lockForm(true);
    status(
      "alert-form-status",
      editing ? "규칙을 수정하는 중…" : "규칙을 만드는 중…",
    );
    try {
      if (editing) {
        const current = rules.find(function (r) {
          return r.id === editing.id;
        });
        const resetBox = $("alert-reset-state");
        const body = buildRuleUpdateBody(current, values, {
          expected_revision: editing.revision,
          reset_state: resetBox ? resetBox.checked : false,
        });
        const updated = await request(
          "/api/alerts/" + encodeURIComponent(editing.id),
          { method: "PATCH", body: body },
        );
        if (ownVersion !== version) return;
        rules = rules.map(function (r) {
          return r.id === editing.id ? updated : r;
        });
        cancelEditSilent();
        renderRules();
        status("alert-form-status", "");
        toast("규칙을 수정했습니다.");
      } else {
        const body = buildRuleCreateBody(values, getCreateKey());
        const created = await request("/api/alerts", {
          method: "POST",
          body: body,
        });
        if (ownVersion !== version) return;
        rules = rules.concat([created]);
        renderRules();
        status("alert-form-status", "");
        toast("규칙을 만들었습니다.");
      }
      clearCreateForm();
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError") {
        if (error.status === 409)
          status(
            "alert-form-status",
            errorMessage(error) + " 최신 내용을 다시 불러온 뒤 확인하세요.",
            true,
          );
        else status("alert-form-status", errorMessage(error), true);
      }
    } finally {
      if (ownVersion === version) lockForm(false);
    }
  }
  async function loadRules(ownVersion) {
    lockList(true);
    status("alert-status", "규칙을 불러오는 중…");
    try {
      const payload = await request("/api/alerts");
      if (ownVersion !== version) return;
      rules = Array.isArray(payload && payload.items) ? payload.items : [];
      discordConfigured =
        payload && payload.discord_configured !== undefined
          ? payload.discord_configured
          : null;
      renderRules();
      status("alert-status", "");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockList(false);
    }
  }
  async function toggleActive(rule) {
    if (listBusy) return;
    if (rule.active) {
      const ok = await confirmAction(
        "일시정지할까요?",
        "진행 단계는 유지되며, 다시 시작하면 이어서 감시합니다.",
        "일시정지",
      );
      if (!ok) return;
    }
    const ownVersion = version;
    lockList(true);
    status(
      "alert-status",
      rule.active ? "일시정지하는 중…" : "다시 시작하는 중…",
    );
    try {
      const next = valuesFromRule(rule);
      next.active = !rule.active;
      const body = buildRuleUpdateBody(rule, next, {
        expected_revision: rule.revision,
        reset_state: false,
      });
      const updated = await request(
        "/api/alerts/" + encodeURIComponent(rule.id),
        { method: "PATCH", body: body },
      );
      if (ownVersion !== version) return;
      rules = rules.map(function (r) {
        return r.id === rule.id ? updated : r;
      });
      if (editing && editing.id === rule.id)
        editing.revision = updated.revision;
      renderRules();
      status("alert-status", "");
      toast(rule.active ? "일시정지했습니다." : "다시 시작했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockList(false);
    }
  }
  async function resetRule(rule) {
    if (listBusy) return;
    const ok = await confirmAction(
      "처음부터 다시 감시할까요?",
      "진행 단계와 주기가 초기화됩니다.",
      "다시 감시",
    );
    if (!ok) return;
    const ownVersion = version;
    lockList(true);
    status("alert-status", "진행 상태를 초기화하는 중…");
    try {
      const updated = await request(
        "/api/alerts/" + encodeURIComponent(rule.id) + "/reset",
        { method: "POST", body: { expected_revision: rule.revision } },
      );
      if (ownVersion !== version) return;
      rules = rules.map(function (r) {
        return r.id === rule.id ? updated : r;
      });
      if (editing && editing.id === rule.id)
        editing.revision = updated.revision;
      renderRules();
      status("alert-status", "");
      toast("진행 상태를 초기화했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockList(false);
    }
  }
  async function removeRule(rule) {
    if (listBusy) return;
    const ok = await confirmAction(
      "규칙을 삭제할까요?",
      "감시는 취소되며 전송 이력은 보존됩니다.",
      "삭제하기",
    );
    if (!ok) return;
    const ownVersion = version;
    lockList(true);
    status("alert-status", "규칙을 삭제하는 중…");
    try {
      await request("/api/alerts/" + encodeURIComponent(rule.id), {
        method: "DELETE",
      });
      if (ownVersion !== version) return;
      rules = rules.filter(function (r) {
        return r.id !== rule.id;
      });
      if (editing && editing.id === rule.id) {
        clearCreateForm();
      }
      renderRules();
      status("alert-status", "");
      toast("규칙을 삭제했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lockList(false);
    }
  }
  function partLine(part) {
    const p = part && typeof part === "object" ? part : {};
    const bits = ["상태 " + deliveryLabel(p.status)];
    if (Number.isInteger(p.attempts)) bits.push("시도 " + String(p.attempts));
    if (p.last_error) bits.push("사유 " + errorText(p.last_error));
    if (p.message_id) bits.push("메시지 " + String(p.message_id));
    if (p.uncertain) bits.push("전송 여부 불확실");
    return bits.join(" · ");
  }
  function eventUncertain(item) {
    const parts = Array.isArray(item.parts) ? item.parts : [];
    return parts.some(function (p) {
      return p && p.uncertain;
    });
  }
  function renderEvents() {
    const list = $("alert-events-list");
    if (!list) return;
    list.replaceChildren();
    if (!events.length) {
      list.append(
        el(
          "p",
          "muted",
          "아직 전송 이력이 없습니다. 조건이 충족되면 여기에 표시됩니다.",
        ),
      );
    }
    events.forEach(function (item) {
      const card = el("article", "alert-history-item");
      card.dataset.eventId = item.id;
      const head = el("div", "alert-rule-head");
      head.append(
        el(
          "strong",
          "",
          nameOf(item.instrument_id) + " · " + dateTime(item.event_time),
        ),
      );
      head.append(el("span", "chip", deliveryLabel(item.delivery_status)));
      if (eventUncertain(item)) head.append(el("span", "chip", "불확실"));
      card.append(head);
      if (item.description)
        card.append(
          el("p", "alert-history-text", String(item.description).slice(0, 220)),
        );
      const meta = el("p", "muted");
      let metaText = formatPrice(item.price_decimal, item.currency);
      if (Number.isInteger(item.cycle))
        metaText += " · 주기 " + String(item.cycle + 1);
      if (item.gap) metaText += " · 공백 이후 첫 관측에서 확인";
      meta.textContent = metaText;
      card.append(meta);
      const parts = Array.isArray(item.parts) ? item.parts : [];
      if (parts.length) {
        const ul = el("ul", "alert-parts");
        parts.forEach(function (part) {
          ul.append(el("li", "muted", partLine(part)));
        });
        card.append(ul);
      }
      const actions = el("div", "row-actions alert-actions");
      const detail = el("button", "", "상세 보기");
      detail.type = "button";
      detail.addEventListener("click", function () {
        openEventDetail(item.id);
      });
      actions.append(detail);
      if (item.delivery_status === "failed") {
        const retry = el("button", "", "다시 시도");
        retry.type = "button";
        retry.addEventListener("click", function () {
          retryEvent(item.id);
        });
        actions.append(retry);
      }
      card.append(actions);
      list.append(card);
    });
    const more = $("alert-events-more");
    if (more) more.hidden = !eventsCursor;
  }
  async function loadEventsPage(more, ownVersion) {
    if (historyBusy) return;
    const useVersion = ownVersion === undefined ? version : ownVersion;
    lockHistory(true);
    if (!more) status("alert-events-status", "이력을 불러오는 중…");
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (more && eventsCursor) params.set("cursor", eventsCursor);
      const page = await request("/api/alert-events?" + params.toString());
      if (useVersion !== version) return;
      const items = Array.isArray(page && page.items) ? page.items : [];
      events = more ? events.concat(items) : items;
      eventsCursor =
        page && page.next_cursor !== undefined ? page.next_cursor : null;
      renderEvents();
      status("alert-events-status", "");
    } catch (error) {
      if (useVersion === version && error && error.name !== "AbortError")
        status("alert-events-status", errorMessage(error), true);
    } finally {
      if (useVersion === version) lockHistory(false);
    }
  }
  async function openEventDetail(id) {
    const ownVersion = version;
    const ownDetail = ++detailSequence;
    status("alert-events-status", "상세를 불러오는 중…");
    try {
      const detail = await request(
        "/api/alert-events/" + encodeURIComponent(id),
      );
      if (ownVersion !== version || ownDetail !== detailSequence) return;
      const panel = $("alert-event-detail");
      if (!panel) return;
      panel.hidden = false;
      panel.replaceChildren();
      const head = el("div", "section-heading");
      head.append(
        el(
          "h2",
          "",
          nameOf(detail.instrument_id) + " · " + dateTime(detail.event_time),
        ),
      );
      const close = el("button", "icon-button", "×");
      close.type = "button";
      close.setAttribute("aria-label", "상세 닫기");
      close.addEventListener("click", function () {
        detailSequence++;
        panel.hidden = true;
        panel.replaceChildren();
      });
      head.append(close);
      panel.append(head);
      const chips = el("div", "alert-detail-chips");
      chips.append(el("span", "chip", deliveryLabel(detail.delivery_status)));
      if (eventUncertain(detail)) chips.append(el("span", "chip", "불확실"));
      panel.append(chips);
      const meta = el("p", "muted");
      let metaText =
        "관측가 " + formatPrice(detail.price_decimal, detail.currency);
      if (Number.isInteger(detail.cycle))
        metaText += " · 주기 " + String(detail.cycle + 1);
      if (detail.gap)
        metaText += " · 공백 이후 첫 관측에서 확인 (최초 발생 시각 아님)";
      meta.textContent = metaText;
      panel.append(meta);
      if (detail.text || detail.description) {
        panel.append(
          el(
            "div",
            "alert-preview-text",
            String(detail.text == null ? detail.description : detail.text),
          ),
        );
      }
      const parts = el("ul", "alert-parts");
      (Array.isArray(detail.parts) ? detail.parts : []).forEach(
        function (part) {
          parts.append(el("li", "muted", partLine(part)));
        },
      );
      panel.append(parts);
      status("alert-events-status", "");
      if (panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError")
        status("alert-events-status", errorMessage(error), true);
    }
  }
  async function retryEvent(id) {
    if (historyBusy) return;
    const target = events.find(function (it) {
      return it.id === id;
    });
    if (!target || target.delivery_status !== "failed") return;
    const key = getRetryKey(id);
    const ownVersion = version;
    lockHistory(true);
    status("alert-events-status", "실패한 전송을 다시 시도하는 중…");
    try {
      const queued = await request(
        "/api/alert-events/" + encodeURIComponent(id) + "/retry",
        { method: "POST", body: { idempotency_key: key } },
      );
      if (ownVersion !== version) return;
      events = events.map(function (it) {
        return it.id === id ? queued : it;
      });
      retryKeys.delete(id);
      renderEvents();
      status("alert-events-status", "전송 대기열에 다시 등록했습니다.");
      toast("전송을 다시 예약했습니다.");
    } catch (error) {
      if (ownVersion === version && error && error.name !== "AbortError") {
        if (isAmbiguousError(error))
          status(
            "alert-events-status",
            errorMessage(error) +
              " 응답이 불확실하므로 같은 키로 다시 시도해 중복 전송을 막으세요.",
            true,
          );
        else {
          retryKeys.delete(id);
          status(
            "alert-events-status",
            errorMessage(error) + " 다음 시도는 새 키로 전송됩니다.",
            true,
          );
        }
      }
    } finally {
      if (ownVersion === version) lockHistory(false);
    }
  }
  async function load() {
    if (listBusy || formBusy || previewBusy || historyBusy) return;
    const ownVersion = ++version;
    renderRules();
    renderEvents();
    await loadRules(ownVersion);
    if (ownVersion !== version) return;
    await loadEventsPage(false, ownVersion);
  }
  function setInstruments(items) {
    instruments = Array.isArray(items) ? items.slice() : [];
    const sel = $("alert-instrument");
    if (sel) {
      const prev = sel.value;
      sel.replaceChildren();
      instruments.forEach(function (it) {
        const opt = el("option", "", it.name + " · " + it.market);
        opt.value = it.id;
        sel.append(opt);
      });
      if (
        instruments.some(function (it) {
          return it.id === prev;
        })
      )
        sel.value = prev;
    }
    updateCurrencyNote();
    renderRules();
    renderEvents();
  }
  function reset() {
    version++;
    detailSequence++;
    createFingerprint = null;
    instruments = [];
    rules = [];
    discordConfigured = null;
    editing = null;
    createKey = null;
    previewFingerprint = null;
    events = [];
    eventsCursor = null;
    retryKeys.clear();
    const form = $("alert-form");
    if (form) form.reset();
    renderStepsEditor([{ op: "gte", price: "" }]);
    cancelEditSilent();
    lockList(false);
    lockForm(false);
    lockPreview(false);
    lockHistory(false);
    renderRules();
    renderEvents();
    renderPreviewResult(null);
    const detail = $("alert-event-detail");
    if (detail) {
      detail.hidden = true;
      detail.replaceChildren();
    }
    status("alert-status", "");
    status("alert-form-status", "");
    status("alert-events-status", "");
    syncSave();
  }
  if (!bound) {
    bound = true;
    const form = $("alert-form");
    if (form) {
      form.addEventListener("submit", saveRule);
      form.addEventListener("input", markPreviewStale);
      form.addEventListener("change", markPreviewStale);
    }
    const run = $("alert-preview-run");
    if (run) run.addEventListener("click", runPreview);
    const add = $("alert-add-step");
    if (add)
      add.addEventListener("click", function () {
        const cur = readSteps();
        if (cur.length >= 3) return;
        cur.push({ op: "cross_above", price: "" });
        renderStepsEditor(cur);
        markPreviewStale();
      });
    const sel = $("alert-instrument");
    if (sel)
      sel.addEventListener("change", function () {
        updateCurrencyNote();
      });
    const cancel = $("alert-cancel-edit");
    if (cancel)
      cancel.addEventListener("click", function () {
        clearCreateForm();
        status("alert-form-status", "");
      });
    const reload = $("alert-reload");
    if (reload)
      reload.addEventListener("click", function () {
        load();
      });
    const eventsReload = $("alert-events-reload");
    if (eventsReload)
      eventsReload.addEventListener("click", function () {
        loadEventsPage(false, version);
      });
    const more = $("alert-events-more");
    if (more)
      more.addEventListener("click", function () {
        loadEventsPage(true, version);
      });
  }
  renderStepsEditor([{ op: "gte", price: "" }]);
  syncSave();
  return { setInstruments: setInstruments, reset: reset, load: load };
}
