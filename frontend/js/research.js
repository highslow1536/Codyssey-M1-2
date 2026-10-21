import {
  $,
  el,
  number,
  unit,
  dateTime,
  status,
  errorMessage,
  safeLink,
} from "./ui.js";

let bound = false;

const QUALITY_LABEL = {
  ok: "정상",
  stale: "일부 지연",
  unavailable: "이용 불가",
  insufficient_data: "관측치 부족",
};

const TREND_LABEL = {
  rising: "상승",
  falling: "하락",
  flat: "보합",
  insufficient_data: "관측치 부족",
};

function isMissing(value) {
  return value === null || value === undefined;
}

function bodyOf(component) {
  if (!component || typeof component !== "object") return {};
  return component;
}

function qualityOf(component) {
  return component?.quality_status ?? "unavailable";
}

function errorOf(component) {
  const error = component?.error;
  if (!error || typeof error !== "object") return null;
  return error;
}

function fmt(value) {
  if (isMissing(value) || typeof value !== "number" || Number.isNaN(value))
    return "—";
  return new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: 2,
  }).format(value);
}

function fmtSignedPct(value) {
  if (isMissing(value) || typeof value !== "number" || Number.isNaN(value))
    return "—";
  return `${value >= 0 ? "+" : ""}${fmt(value)}%`;
}

function setChip(id, quality) {
  const node = $(id);
  if (!node) return;
  node.textContent = QUALITY_LABEL[quality] ?? quality ?? "이용 불가";
  node.dataset.quality = quality ?? "unavailable";
}

function linkElement(url, label) {
  const safe = safeLink(url);
  if (!safe) return null;
  const anchor = el("a", "", label || safe);
  anchor.href = safe;
  anchor.target = "_blank";
  anchor.rel = "noopener";
  return anchor;
}

export function initializeResearch({ request }) {
  if (typeof request !== "function")
    throw new Error("request 함수가 필요합니다.");
  let instruments = [];
  let version = 0;
  let busy = false;
  const controls = [
    "research-refresh",
    "research-instrument",
    "research-window",
  ];

  function lock(value) {
    busy = value;
    for (const id of controls) {
      const node = $(id);
      if (node) node.disabled = value;
    }
  }

  function renderMetricCards(items) {
    const holder = $("research-metrics");
    if (!holder) return;
    holder.replaceChildren(
      ...items.map(([label, value, suffix, caption]) => {
        const card = el("div", "metric");
        const valueNode = el("div", "metric-value", value);
        valueNode.append(el("span", "metric-unit", suffix));
        card.append(
          el("div", "metric-label", label),
          valueNode,
          el("div", "metric-caption", caption),
        );
        return card;
      }),
    );
  }

  function renderPlaceholders(message) {
    renderMetricCards([
      ["최신 종가", "—", "", message],
      ["20일 수익률", "—", "", message],
      ["20일 실현 변동성", "—", "", message],
      ["RSI-14", "—", "", message],
      ["20일 평균 대비", "—", "", message],
    ]);
    for (const id of ["research-cnn", "research-vix"]) {
      const node = $(id);
      if (node) node.textContent = message;
    }
    const items = $("research-news-items");
    if (items) items.textContent = message;
  }

  function renderEmpty(message) {
    const text = message || "종목을 선택하고 새로 확인을 눌러보세요.";
    const nameNode = $("research-instrument-name");
    if (nameNode) nameNode.textContent = "";
    const asof = $("research-asof");
    if (asof) asof.textContent = "";
    renderPlaceholders(text);
    for (const id of [
      "research-cnn-chip",
      "research-vix-chip",
      "research-news-chip",
    ]) {
      const node = $(id);
      if (node) node.textContent = "";
    }
    const analysisNote = $("research-analysis-note");
    if (analysisNote) analysisNote.textContent = "";
    const summary = $("research-news-summary");
    if (summary) {
      summary.textContent = "";
      summary.parentNode?.querySelector("[data-summary-error]")?.remove();
    }
    const method = $("research-news-summary-method");
    if (method) method.textContent = "";
    const highlights = $("research-news-highlights");
    if (highlights) highlights.replaceChildren();
    const count = $("research-news-count");
    if (count) count.textContent = "0";
    const newsNote = $("research-news-note");
    if (newsNote) newsNote.textContent = "";
  }

  function renderAnalysis(component, instrument) {
    const body = bodyOf(component);
    const quality = qualityOf(component);
    const asofValue =
      body.data_as_of ?? body.event_time ?? component?.event_time;
    const asof = $("research-asof");
    if (asof)
      asof.textContent = asofValue
        ? `데이터 기준 ${dateTime(asofValue)}`
        : "기준 시점 없음";
    const nameNode = $("research-instrument-name");
    if (nameNode)
      nameNode.textContent =
        instrument?.name ?? component?.instrument?.name ?? "";
    const currency = body.currency ?? instrument?.currency ?? "";
    const suffix = unit(currency) || currency || "";
    const returns = body.returns_pct ?? {};
    const usable = quality !== "unavailable";
    renderMetricCards([
      [
        "최신 종가",
        !usable || isMissing(body.latest_close) ? "—" : fmt(body.latest_close),
        suffix,
        "수정 종가 기준",
      ],
      [
        "20일 수익률",
        !usable || isMissing(returns?.["20"])
          ? "—"
          : fmtSignedPct(returns["20"]),
        "",
        "수정 종가 기준",
      ],
      [
        "20일 실현 변동성",
        !usable || isMissing(body.realized_volatility_20d_pct)
          ? "—"
          : `${fmt(body.realized_volatility_20d_pct)}%`,
        "",
        "표본 로그수익률 · 연율화",
      ],
      [
        "RSI-14",
        !usable || isMissing(body.rsi_14) ? "—" : fmt(body.rsi_14),
        "",
        "Wilder 방식 · 14일",
      ],
      [
        "20일 평균 대비",
        !usable || isMissing(body.sma_20_distance_pct)
          ? "—"
          : fmtSignedPct(body.sma_20_distance_pct),
        "",
        "SMA-20 대비",
      ],
    ]);
    const note = $("research-analysis-note");
    if (!note) return;
    note.replaceChildren();
    if (quality !== "ok") {
      note.append(
        el(
          "div",
          "",
          quality === "stale"
            ? "분석 데이터가 일부 지연되고 있습니다."
            : quality === "insufficient_data"
              ? "관측치가 부족한 지표는 비워 두었습니다."
              : "분석 데이터를 이용할 수 없습니다.",
        ),
      );
    }
    const err = errorOf(component);
    if (err?.message) note.append(el("div", "", String(err.message)));
    const trend = TREND_LABEL[body.trend] ?? body.trend ?? "데이터 대기";
    const details = [
      `관측 ${isMissing(body.observation_count) ? "—" : number(body.observation_count)}건`,
      `추세 ${trend}`,
      `1일 ${fmtSignedPct(returns?.["1"])} · 5일 ${fmtSignedPct(returns?.["5"])} · 60일 ${fmtSignedPct(returns?.["60"])}`,
      `SMA-20 ${isMissing(body.sma_20) ? "—" : fmt(body.sma_20)} · SMA-60 ${isMissing(body.sma_60) ? "—" : fmt(body.sma_60)}`,
      `SMA-60 대비 ${fmtSignedPct(body.sma_60_distance_pct)} · 거래량 비율(20일) ${isMissing(body.volume_ratio_20d) ? "—" : fmt(body.volume_ratio_20d)}`,
      "원본 일봉 기준 · 직접 입력·수정값 미포함",
      "연율화 252일 가정 · RSI-14 Wilder 방식",
    ];
    note.append(el("div", "", details.join("\n")));
    const meta = [
      `가져온 시각 ${dateTime(component?.fetched_at)}`,
      `이벤트 시각 ${dateTime(asofValue)}`,
    ];
    if (component?.cache_expires_at)
      meta.push(`캐시 만료 ${dateTime(component.cache_expires_at)}`);
    if (component?.last_attempt_at)
      meta.push(`마지막 시도 ${dateTime(component.last_attempt_at)}`);
    note.append(el("div", "", meta.join(" · ")));
    const urls = Array.isArray(body.source_urls) ? body.source_urls : [];
    if (urls.length) {
      const wrap = el("div", "research-links");
      wrap.append(el("span", "", "출처 "));
      let added = 0;
      urls.forEach((url, index) => {
        const link = linkElement(url, `원본 ${index + 1}`);
        if (link) {
          if (added > 0) wrap.append(document.createTextNode(" · "));
          wrap.append(link);
          added++;
        }
      });
      if (added > 0) note.append(wrap);
    }
  }

  function renderCnn(component) {
    const body = bodyOf(component);
    const quality = qualityOf(component);
    setChip("research-cnn-chip", quality);
    const holder = $("research-cnn");
    if (!holder) return;
    holder.replaceChildren();
    holder.append(
      el("p", "research-kind", "CNN 공포·탐욕 지수 · 미국 주식 시장 기준"),
    );
    const eventValue = body.event_time ?? component?.event_time;
    const fetchedValue = body.fetched_at ?? component?.fetched_at;
    if (quality === "unavailable" || isMissing(body.value)) {
      holder.append(
        el(
          "p",
          "research-unavailable",
          quality === "insufficient_data"
            ? "관측치가 부족해 심리 지수를 표시할 수 없습니다."
            : "심리 지수를 이용할 수 없습니다.",
        ),
      );
      const err = errorOf(component);
      if (err?.message) holder.append(el("p", "muted", String(err.message)));
      holder.append(
        el(
          "p",
          "muted",
          `가져온 시각 ${dateTime(fetchedValue)} · 이벤트 시각 ${dateTime(eventValue)}`,
        ),
      );
      return;
    }
    const valueLine = el("div", "research-big");
    valueLine.append(
      el(
        "span",
        "research-value",
        isMissing(body.value) ? "—" : fmt(body.value),
      ),
      el("span", "metric-unit", "/ 100"),
    );
    holder.append(valueLine);
    if (!isMissing(body.rating))
      holder.append(el("p", "research-rating", String(body.rating)));
    if (quality === "stale")
      holder.append(el("p", "muted", "일부 지연된 심리 지수입니다."));
    if (errorOf(component)?.message)
      holder.append(el("p", "muted", String(errorOf(component).message)));
    const lines = el("div", "research-lines");
    for (const [label, v] of [
      ["직전", body.previous_close],
      ["1주 전", body.previous_1_week],
      ["1개월 전", body.previous_1_month],
    ]) {
      const row = el("div", "research-line");
      row.append(el("span", "muted", label));
      row.append(el("span", "", isMissing(v) ? "—" : fmt(v)));
      lines.append(row);
    }
    holder.append(lines);
    const history = Array.isArray(body.history) ? body.history : [];
    if (history.length) {
      holder.append(el("p", "muted", "최근 기록"));
      const list = el("ul", "research-history");
      for (const item of history.slice(-7).reverse()) {
        const li = el("li", "");
        li.textContent = `${dateTime(item?.event_time)} · ${isMissing(item?.value) ? "—" : fmt(item.value)}${item?.rating ? ` · ${item.rating}` : ""}`;
        list.append(li);
      }
      holder.append(list);
    }
    holder.append(
      el(
        "p",
        "muted",
        `이벤트 시각 ${dateTime(eventValue)} · 가져온 시각 ${dateTime(fetchedValue)}`,
      ),
    );
    if (body.source_url) {
      const link = linkElement(body.source_url, "출처 보기");
      if (link) holder.append(link);
    }
  }

  function renderVix(component) {
    const body = bodyOf(component);
    const quality = qualityOf(component);
    setChip("research-vix-chip", quality);
    const holder = $("research-vix");
    if (!holder) return;
    holder.replaceChildren();
    holder.append(
      el(
        "p",
        "research-kind",
        "VIX · 미국 기대 변동성 · 실현 변동성(20일)과 다름",
      ),
    );
    if (quality === "unavailable" || isMissing(body.latest_close)) {
      holder.append(
        el(
          "p",
          "research-unavailable",
          quality === "insufficient_data"
            ? "관측치가 부족해 변동성 지수를 표시할 수 없습니다."
            : "변동성 지수를 이용할 수 없습니다.",
        ),
      );
      const err = errorOf(component);
      if (err?.message) holder.append(el("p", "muted", String(err.message)));
      return;
    }
    const valueLine = el("div", "research-big");
    valueLine.append(
      el(
        "span",
        "research-value",
        isMissing(body.latest_close) ? "—" : fmt(body.latest_close),
      ),
      el("span", "metric-unit", "pt"),
    );
    holder.append(valueLine);
    const lines = el("div", "research-lines");
    const rows = [
      ["전일 대비", fmtSignedPct(body.change_pct)],
      [
        "백분위",
        isMissing(body.percentile_rank)
          ? "—"
          : `${fmt(body.percentile_rank)}%${isMissing(body.percentile_observations) ? "" : ` · 관측 ${number(body.percentile_observations)}건`}`,
      ],
      ["가져온 시각", dateTime(body.fetched_at ?? component?.fetched_at)],
      [
        "이벤트 시각",
        dateTime(body.event_time ?? body.data_as_of ?? component?.event_time),
      ],
    ];
    for (const [label, text] of rows) {
      const row = el("div", "research-line");
      row.append(el("span", "muted", label));
      row.append(el("span", "", text));
      lines.append(row);
    }
    holder.append(lines);
    if (quality === "stale")
      holder.append(el("p", "muted", "일부 지연된 변동성 지수입니다."));
    if (errorOf(component)?.message)
      holder.append(el("p", "muted", String(errorOf(component).message)));
  }

  function renderNews(component) {
    const body = bodyOf(component);
    const quality = qualityOf(component);
    setChip("research-news-chip", quality);
    const count = $("research-news-count");
    const methodNode = $("research-news-summary-method");
    const summaryNode = $("research-news-summary");
    const highlightsNode = $("research-news-highlights");
    const itemsNode = $("research-news-items");
    const noteNode = $("research-news-note");
    if (highlightsNode) highlightsNode.replaceChildren();
    if (itemsNode) itemsNode.replaceChildren();
    if (summaryNode) summaryNode.textContent = "";
    if (methodNode) methodNode.textContent = "";
    if (noteNode) noteNode.textContent = "";
    const meta = `발행 기준 ${dateTime(body.event_time)} · 수집 ${dateTime(body.fetched_at)} · 마지막 시도 ${dateTime(body.last_attempt_at)}`;
    const items = Array.isArray(body.items) ? body.items : null;
    const summary = body.summary ?? null;
    const summaryError =
      summary &&
      typeof summary === "object" &&
      summary.error &&
      typeof summary.error === "object"
        ? summary.error
        : null;
    if (summaryNode)
      summaryNode.parentNode?.querySelector("[data-summary-error]")?.remove();
    if (quality === "unavailable" || !body || items === null) {
      if (count) count.textContent = "0";
      const err = errorOf(component);
      if (itemsNode) {
        if (err?.code === "news_not_configured")
          itemsNode.textContent = "뉴스가 설정되지 않았습니다.";
        else
          itemsNode.textContent = err?.message
            ? String(err.message)
            : "뉴스를 이용할 수 없습니다.";
      }
      if (noteNode) noteNode.textContent = meta;
      return;
    }
    if (count) count.textContent = String(items.length);
    const market = body.market ?? "";
    const provider = body.provider ?? "";
    if (methodNode && summary) {
      if (summary.method === "openai")
        methodNode.textContent =
          "AI 한국어 요약 · 제목·요약 범위에서 제공합니다";
      else if (summary.method === "extractive")
        methodNode.textContent =
          market === "US"
            ? "제공된 제목·요약 발췌 · 원문 범위(번역 없음)"
            : "제공된 제목·요약 발췌";
    }
    if (summaryNode) {
      if (summary?.text) summaryNode.textContent = String(summary.text);
      else if (quality === "insufficient_data")
        summaryNode.textContent = "요약할 뉴스가 부족합니다.";
    }
    const highlights = Array.isArray(summary?.highlights)
      ? summary.highlights
      : [];
    if (summaryNode && summary?.text && summary.method === "openai") {
      summaryNode.replaceChildren();
      for (const part of String(summary.text).split(/(\[[^\[\]]+\])/g)) {
        const index = highlights.findIndex(
          (item) => `[${item.article_id}]` === part,
        );
        const link =
          index >= 0
            ? linkElement(highlights[index].source_url, `[${index + 1}]`)
            : null;
        summaryNode.append(link || document.createTextNode(part));
      }
    }
    if (
      summaryNode &&
      summaryError &&
      (summaryError.code || summaryError.message)
    ) {
      const reason = el(
        "p",
        "muted",
        summaryError.message
          ? `요약 생성 실패${summaryError.code ? `(${String(summaryError.code)})` : ""}: ${String(summaryError.message)}`
          : `요약 생성 실패(${String(summaryError.code)})`,
      );
      reason.dataset.summaryError = "true";
      summaryNode.after(reason);
    }
    if (highlightsNode && highlights.length) {
      highlightsNode.append(el("p", "muted", "주요 요약"));
      const list = el("ul", "research-highlights");
      for (const [index, h] of highlights.entries()) {
        const li = el("li", "");
        li.append(
          el(
            "strong",
            "",
            `[${index + 1}] ${h?.title ? String(h.title) : "제목 없음"}`,
          ),
        );
        if (h?.summary) li.append(el("p", "", String(h.summary)));
        li.append(el("p", "muted", dateTime(h?.published_at)));
        const link = h?.source_url
          ? linkElement(h.source_url, "원문 링크")
          : null;
        if (link) li.append(link);
        list.append(li);
      }
      highlightsNode.append(list);
    }
    if (itemsNode) {
      if (!items.length) {
        itemsNode.textContent = "해당 기간의 뉴스가 없습니다.";
      } else {
        const list = el("ul", "research-list");
        for (const item of items) {
          const li = el("li", "research-item");
          li.append(
            el("h3", "", item?.title ? String(item.title) : "제목 없음"),
          );
          if (item?.summary) li.append(el("p", "", String(item.summary)));
          li.append(
            el(
              "p",
              "muted",
              `${item?.source ? String(item.source) : "출처 없음"} · ${dateTime(item?.published_at)}`,
            ),
          );
          const link = item?.source_url
            ? linkElement(item.source_url, "원문 링크")
            : null;
          if (link) li.append(link);
          list.append(li);
        }
        itemsNode.append(list);
      }
    }
    if (noteNode) {
      const parts = [meta];
      if (errorOf(component)?.message)
        parts.push(String(errorOf(component).message));
      if (provider || market)
        parts.push(`제공 ${[provider, market].filter(Boolean).join(" · ")}`);
      if (body.window) parts.push(`기간 ${String(body.window)}`);
      if (body.coverage_note) parts.push(String(body.coverage_note));
      if (body.truncated) parts.push("일부만 표시합니다.");
      parts.push("제목·요약 범위에서 제공합니다. 전문을 제공하지 않습니다.");
      noteNode.textContent = parts.join(" · ");
    }
  }

  function render(payload, instrumentId) {
    const instrument =
      instruments.find((item) => item.id === instrumentId) ??
      payload?.instrument ??
      null;
    renderAnalysis(payload?.analysis, instrument);
    renderCnn(payload?.sentiment?.cnn);
    renderVix(payload?.sentiment?.vix);
    renderNews(payload?.news);
  }

  async function load() {
    if (busy) return;
    const instrumentId = $("research-instrument")?.value || "";
    const windowValue = $("research-window")?.value || "24h";
    if (!instrumentId) {
      renderEmpty("브리핑할 종목을 먼저 불러오세요.");
      status("research-status", "브리핑할 종목이 없습니다.", true);
      return;
    }
    const ownVersion = ++version;
    lock(true);
    renderEmpty("시장 브리핑을 불러오는 중…");
    status("research-status", "시장 브리핑을 불러오는 중…");
    try {
      const params = new URLSearchParams({
        instrument_id: instrumentId,
        window: windowValue,
      });
      const payload = await request(`/api/market/overview?${params}`);
      if (ownVersion !== version) return;
      if (
        !payload ||
        (!payload.analysis && !payload.sentiment && !payload.news)
      ) {
        renderEmpty("해당 조건의 브리핑이 없습니다.");
        status("research-status", "해당 조건의 브리핑이 없습니다.");
      } else {
        render(payload, instrumentId);
        status("research-status", "");
      }
    } catch (error) {
      if (ownVersion === version && error?.name !== "AbortError")
        status("research-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lock(false);
    }
  }

  function setInstruments(items) {
    const list = Array.isArray(items) ? [...items] : [];
    instruments = list;
    const select = $("research-instrument");
    if (!select) return;
    const previous = select.value;
    select.replaceChildren(
      ...list.map((item) => {
        const option = el("option", "", `${item.name} · ${item.market}`);
        option.value = item.id;
        return option;
      }),
    );
    if (!list.length) {
      select.value = "";
      return;
    }
    if (list.some((item) => item.id === previous)) select.value = previous;
    else if (list.some((item) => item.id === "INDEX_SP500"))
      select.value = "INDEX_SP500";
    else select.value = list[0].id;
  }

  function reset() {
    version++;
    instruments = [];
    lock(false);
    const select = $("research-instrument");
    if (select) select.replaceChildren();
    renderEmpty();
    status("research-status", "");
  }

  if (!bound) {
    bound = true;
    $("research-filter")?.addEventListener("submit", (event) => {
      event.preventDefault();
      load();
    });
    $("research-instrument")?.addEventListener("change", () => {
      load();
    });
    $("research-window")?.addEventListener("change", () => {
      load();
    });
  }

  return { setInstruments, reset, load };
}
