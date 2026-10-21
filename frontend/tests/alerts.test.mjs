// 가격 알림 pure helper 단위 테스트 (Node 내장 runner, DOM 불필요).
// frontend/ 에서 실행: node --test tests/alerts.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  isValidPriceDecimal,
  buildRuleValues,
  validateRuleValues,
  buildRuleCreateBody,
  buildRuleUpdateBody,
  ruleConditionsChanged,
  isAmbiguousError,
} from "../js/alerts.js";

const INSTRUMENTS = [
  { id: "KRX_005930", name: "삼성전자", market: "KR", currency: "KRW" },
  { id: "NASDAQ_AAPL", name: "애플", market: "US", currency: "USD" },
];

function validValues() {
  return {
    instrument_id: "KRX_005930",
    currency: "KRW",
    steps: [
      { op: "cross_above", price: "70000" },
      { op: "lte", price: "65000" },
    ],
    session_scope: "regular",
    repeat_mode: "once",
    expires_at: null,
    step_timeout_hours: null,
    active: true,
  };
}

test("소수점 문자열 가격은 원본 그대로 유지하고 양수만 허용한다", () => {
  assert.equal(isValidPriceDecimal("70000"), true);
  assert.equal(isValidPriceDecimal("35000.50"), true);
  assert.equal(isValidPriceDecimal("0"), false);
  assert.equal(isValidPriceDecimal("-10"), false);
  assert.equal(isValidPriceDecimal("1e5"), false);
  assert.equal(isValidPriceDecimal("abc"), false);
});

test("1~3단계와 연산자·가격·만료를 검증한다", () => {
  assert.equal(validateRuleValues(validValues(), INSTRUMENTS), null);
  assert.match(
    validateRuleValues({ ...validValues(), steps: [] }, INSTRUMENTS),
    /단계/,
  );
  const tooMany = {
    ...validValues(),
    steps: [1, 2, 3, 4].map(() => ({ op: "gte", price: "1" })),
  };
  assert.match(validateRuleValues(tooMany, INSTRUMENTS), /3개/);
  const badOp = { ...validValues(), steps: [{ op: "nope", price: "1" }] };
  assert.match(validateRuleValues(badOp, INSTRUMENTS), /연산자/);
  const badPrice = { ...validValues(), steps: [{ op: "gte", price: "0" }] };
  assert.match(validateRuleValues(badPrice, INSTRUMENTS), /가격/);
  const andOne = {
    ...validValues(),
    steps: [{ all: [{ op: "gte", price: "1" }] }],
  };
  assert.match(validateRuleValues(andOne, INSTRUMENTS), /AND/);
  const past = { ...validValues(), expires_at: "2000-01-01T00:00:00.000Z" };
  assert.match(validateRuleValues(past, INSTRUMENTS), /만료/);
});

test("생성 본문은 멱등 키를 포함하고 수정 본문은 전체 값과 revision을 포함한다", () => {
  const values = validValues();
  const create = buildRuleCreateBody(values, "key-1");
  assert.equal(create.idempotency_key, "key-1");
  assert.equal(create.session_scope, "regular");
  const rule = { ...values, revision: 4 };
  const update = buildRuleUpdateBody(rule, values, {
    expected_revision: 4,
    reset_state: true,
  });
  assert.equal(update.expected_revision, 4);
  assert.equal(update.reset_state, true);
  assert.ok(!Object.hasOwn(update, "idempotency_key"));
});

test("조건 변경 여부를 감지해 reset_state 요구를 판단한다", () => {
  const rule = { ...validValues(), revision: 2 };
  assert.equal(ruleConditionsChanged(rule, validValues()), false);
  assert.equal(
    ruleConditionsChanged(rule, { ...validValues(), active: false }),
    false,
  );
  assert.equal(
    ruleConditionsChanged(rule, {
      ...validValues(),
      steps: [{ op: "gte", price: "70000" }],
    }),
    true,
  );
  assert.equal(
    ruleConditionsChanged(rule, { ...validValues(), repeat_mode: "repeat" }),
    true,
  );
});

test("재시도 키 정책: 애매한 실패는 유지하고 확정 실패는 교체한다", () => {
  assert.equal(isAmbiguousError({ code: "network_error" }), true);
  assert.equal(isAmbiguousError({ code: "response_error" }), true);
  assert.equal(isAmbiguousError({ status: 409, code: "conflict" }), false);
  assert.equal(isAmbiguousError({ status: 500, code: "x" }), true);
});
