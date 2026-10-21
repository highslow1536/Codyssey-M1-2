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

export function initializeChat({ request }) {
  let conversations = [],
    listCursor = null,
    currentId = null,
    messages = [],
    retry = null,
    busy = false,
    version = 0,
    listVersion = 0;
  const pendingDeletes = new Map();
  function lock(value) {
    busy = value;
    [
      "send-chat",
      "new-chat",
      "chat-instrument",
      "load-conversations-more",
    ].forEach((id) => {
      $(id).disabled = value;
    });
    $("chat-input").disabled = value || !!retry;
    $("send-chat").disabled = value || !!retry;
    $("retry-chat").disabled = value;
    $("send-chat").textContent = value
      ? "응답을 준비하는 중…"
      : "질문 보내기 ↑";
    $("conversations")
      .querySelectorAll("button")
      .forEach((button) => {
        button.disabled = value || button.dataset.deleting === "true";
      });
  }
  function renderList() {
    $("conversations").replaceChildren(
      ...[
        ...pendingDeletes.values(),
        ...conversations.filter((c) => !pendingDeletes.has(c.id)),
      ].map((c) => {
        const item = el(
            "div",
            `conversation-item${c.id === currentId ? " selected" : ""}`,
          ),
          open = el("button", "conversation-open");
        open.type = "button";
        open.append(
          el("strong", "", c.title),
          el(
            "small",
            "",
            `${dateTime(c.updated_at)} · ${c.message_count}개 메시지`,
          ),
        );
        open.addEventListener("click", () => loadConversation(c.id));
        open.disabled = busy || pendingDeletes.has(c.id);
        open.dataset.deleting = String(pendingDeletes.has(c.id));
        const remove = el(
          "button",
          "icon-button",
          pendingDeletes.has(c.id) ? "↻" : "×",
        );
        remove.type = "button";
        remove.setAttribute(
          "aria-label",
          `${c.title} 대화 ${pendingDeletes.has(c.id) ? "삭제 재개" : "삭제"}`,
        );
        remove.disabled = busy;
        remove.addEventListener("click", () => removeConversation(c));
        item.append(open, remove);
        return item;
      }),
    );
    if (!conversations.length && !pendingDeletes.size)
      $("conversations").append(
        el("p", "muted", "저장된 대화가 없습니다. 첫 질문을 시작해보세요."),
      );
    $("load-conversations-more").hidden = !listCursor;
  }
  async function loadList(more = false) {
    const ownVersion = version,
      ownList = ++listVersion;
    status("history-status", "대화를 불러오는 중…");
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (more && listCursor) params.set("cursor", listCursor);
      const page = await request(`/api/conversations?${params}`);
      if (ownVersion !== version || ownList !== listVersion) return;
      conversations = more ? [...conversations, ...page.items] : page.items;
      listCursor = page.next_cursor;
      renderList();
      status("history-status");
    } catch (error) {
      if (
        ownVersion === version &&
        ownList === listVersion &&
        error.name !== "AbortError"
      )
        status("history-status", errorMessage(error), true);
    }
  }
  function renderMessages(extra = null) {
    const nodes = messages.map((message) => {
      const item = el("article", `message ${message.role}`);
      item.append(
        el(
          "span",
          "message-label",
          message.role === "user" ? "YOU" : "MARKETNOTE AI",
        ),
        el("div", "message-content", message.content),
      );
      return item;
    });
    if (!nodes.length) {
      const welcome = el("div", "chat-welcome");
      welcome.append(
        el("span", "empty-icon", "✳"),
        el("h3", "", "어떤 흐름이 궁금한가요?"),
        el(
          "p",
          "",
          "선택한 종목의 분석 데이터를 바탕으로\n평균, 추세, 데이터의 특징을 함께 살펴보세요.",
        ),
      );
      const suggestions = el("div", "suggestions");
      for (const text of [
        "최근 추세를 요약해줘",
        "평균과 최고·최저값을 비교해줘",
      ]) {
        const button = el("button", "", text);
        button.type = "button";
        button.addEventListener("click", () => {
          $("chat-input").value = text;
          $("chat-input").focus();
        });
        suggestions.append(button);
      }
      welcome.append(suggestions);
      nodes.push(welcome);
    }
    $("messages").replaceChildren(...nodes);
    if (extra && nodes.length) {
      const last = nodes.at(-1),
        sources = el("div", "message-sources");
      for (const source of extra.sources || []) {
        const url = safeLink(source.url);
        if (!url) continue;
        const link = el(
          "a",
          "",
          `${source.title || source.provider || "출처"} ↗`,
        );
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        sources.append(link);
      }
      if (sources.childNodes.length) last.append(sources);
      const traces = (extra.tool_trace || [])
        .map((t) => `${t.ok ? "✓" : "!"} ${t.tool}: ${t.summary}`)
        .join("\n");
      if (traces) last.append(el("div", "message-tools", traces));
      if (extra.data_as_of)
        last.append(
          el(
            "div",
            "message-tools",
            `데이터 기준 ${dateTime(extra.data_as_of)}`,
          ),
        );
    }
    $("messages").scrollTop = $("messages").scrollHeight;
  }
  function clearError() {
    retry = null;
    $("chat-error").hidden = true;
  }
  async function leaveRetry() {
    if (!retry) return true;
    return confirmAction(
      "재시도를 종료하고 이동할까요?",
      "질문이 서버에 이미 저장되었을 수 있습니다. 이동하면 같은 요청으로 재시도할 수 없습니다. 대화 목록에서 저장 여부를 확인하세요.",
      "이동하기",
    );
  }
  async function newChat() {
    if (busy) return;
    const previousVersion = version;
    if (!(await leaveRetry()) || previousVersion !== version) return;
    version++;
    currentId = null;
    messages = [];
    clearError();
    $("chat-input").value = "";
    $("chat-title").textContent = "새 리서치 대화";
    renderMessages();
    renderList();
    lock(false);
    loadList();
  }
  async function fetchHistory(id, ownVersion) {
    let cursor = null,
      loaded = [],
      detail;
    do {
      const params = new URLSearchParams({ limit: "100" });
      if (cursor) params.set("cursor", cursor);
      detail = await request(
        `/api/conversations/${encodeURIComponent(id)}?${params}`,
      );
      if (ownVersion !== version)
        throw new DOMException("화면이 변경되었습니다.", "AbortError");
      loaded.push(...detail.messages);
      cursor = detail.next_cursor;
    } while (cursor);
    return { ...detail, messages: loaded };
  }
  async function loadConversation(id) {
    if (busy) return;
    const previousVersion = version;
    if (!(await leaveRetry()) || previousVersion !== version) return;
    const ownVersion = ++version;
    lock(true);
    clearError();
    status("history-status", "전체 대화 기록을 불러오는 중…");
    try {
      const detail = await fetchHistory(id, ownVersion);
      if (ownVersion !== version) return;
      currentId = id;
      messages = detail.messages;
      $("chat-title").textContent = detail.title;
      renderMessages();
      renderList();
      $("chat-input").value = "";
      status("history-status");
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError")
        status("history-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lock(false);
    }
  }
  async function send(payload) {
    if (busy) return;
    const ownVersion = version;
    lock(true);
    $("chat-error").hidden = true;
    if (!retry) {
      messages.push({ role: "user", content: payload.message });
      renderMessages();
    }
    try {
      const response = await request("/api/chat", {
        method: "POST",
        body: payload,
      });
      if (ownVersion !== version) return;
      currentId = response.conversation_id;
      messages.push({ role: "assistant", content: response.message });
      clearError();
      $("chat-input").value = "";
      $("chat-title").textContent =
        conversations.find((c) => c.id === currentId)?.title ||
        payload.message.slice(0, 60);
      renderMessages(response);
      await loadList();
      if (ownVersion === version)
        $("chat-title").textContent =
          conversations.find((c) => c.id === currentId)?.title ||
          $("chat-title").textContent;
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError") {
        retry = payload;
        $("chat-error-text").textContent =
          `${errorMessage(error)}\n같은 질문을 재시도하면 중복 저장을 방지합니다. 새 대화를 시작할 수도 있습니다.`;
        $("chat-error").hidden = false;
      }
    } finally {
      if (ownVersion === version) lock(false);
    }
  }
  async function removeConversation(c) {
    if (busy) return;
    const ownVersion = version;
    if (
      (!pendingDeletes.has(c.id) &&
        !(await confirmAction(
          "대화를 삭제하시겠어요?",
          `“${c.title}”의 메시지를 모두 삭제합니다.`,
        ))) ||
      ownVersion !== version
    )
      return;
    lock(true);
    status("history-status", "대화를 삭제하는 중…");
    try {
      let result = await request(
        `/api/conversations/${encodeURIComponent(c.id)}`,
        { method: "DELETE" },
      );
      if (ownVersion !== version) return;
      if (result?.status === "pending") {
        pendingDeletes.set(c.id, c);
        renderList();
      }
      // Large histories are removed by bounded jobs. Each explicit DELETE resumes cleanup.
      for (
        let attempt = 0;
        result?.status === "pending" && attempt < 8 && ownVersion === version;
        attempt++
      ) {
        status("history-status", "큰 대화의 하위 메시지를 정리하는 중…");
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(500 * (attempt + 1), 2000)),
        );
        if (ownVersion !== version) return;
        const job = await request(
          `/api/jobs/${encodeURIComponent(result.job_id)}`,
        );
        if (job.status === "completed") {
          result = null;
          break;
        }
        if (job.status !== "pending")
          throw new Error(
            "삭제 작업을 완료하지 못했습니다. 삭제 재개 버튼을 눌러 다시 시도하세요.",
          );
        result = await request(
          `/api/conversations/${encodeURIComponent(c.id)}`,
          { method: "DELETE" },
        );
      }
      if (ownVersion !== version) return;
      if (result?.status !== "pending") {
        pendingDeletes.delete(c.id);
        toast("대화를 삭제했습니다.");
      }
      if (currentId === c.id) {
        currentId = null;
        messages = [];
        clearError();
        $("chat-title").textContent = "새 리서치 대화";
        renderMessages();
      }
      await loadList();
      if (result?.status === "pending")
        status(
          "history-status",
          "삭제가 진행 중입니다. 목록의 ↻ 버튼으로 이어서 정리할 수 있습니다.",
        );
    } catch (error) {
      if (ownVersion === version && error.name !== "AbortError")
        status("history-status", errorMessage(error), true);
    } finally {
      if (ownVersion === version) lock(false);
    }
  }
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || retry) return;
    const message = $("chat-input").value.trim();
    if (!message) return;
    send({
      message,
      client_request_id: crypto.randomUUID(),
      instrument_id: $("chat-instrument").value,
      ...(currentId ? { conversation_id: currentId } : {}),
    });
  });
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("retry-chat").addEventListener("click", () => {
    if (retry) send(retry);
  });
  $("new-chat").addEventListener("click", newChat);
  $("load-conversations-more").addEventListener("click", () => loadList(true));
  return {
    loadList,
    reset() {
      version++;
      listVersion++;
      conversations = [];
      pendingDeletes.clear();
      listCursor = null;
      currentId = null;
      messages = [];
      clearError();
      $("chat-title").textContent = "새 리서치 대화";
      $("chat-input").value = "";
      status("history-status");
      renderList();
      renderMessages();
      lock(false);
    },
  };
}
