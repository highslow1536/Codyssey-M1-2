import {
  initializeAuth,
  signIn,
  signInGoogle,
  signOut,
  getToken,
} from "./auth.js";
import { createApi } from "./api.js";
import { initializeData } from "./data.js";
import { initializeChat } from "./chat.js";
import { initializeResearch } from "./research.js";
import { $, el, localDate, status, errorMessage } from "./ui.js";

let user = null,
  instruments = [],
  session = 0,
  controller = new AbortController(),
  ready = false;
const config = window.APP_CONFIG || {};
let api;
async function request(path, options = {}) {
  const ownSession = session;
  if (!user) throw new Error("로그인이 필요합니다.");
  const result = await api.request(path, {
    ...options,
    signal: controller.signal,
  });
  if (ownSession !== session)
    throw new DOMException("로그인 계정이 변경되었습니다.", "AbortError");
  return result;
}
const data = initializeData({
  request,
  getInstrument: () => instruments.find((i) => i.id === $("instrument").value),
  onInstrumentChange: (id) => {
    $("chat-instrument").value = id;
  },
});
const chat = initializeChat({ request });
const research = initializeResearch({ request });
const today = new Date(),
  start = new Date(today);
start.setFullYear(start.getFullYear() - 1);
$("start-date").value = localDate(start);
$("end-date").value = localDate(today);
function navigate() {
  const view = ["#chat", "#research"].includes(location.hash)
    ? location.hash.slice(1)
    : "data";
  const isChat = view === "chat",
    isResearch = view === "research";
  $("data-view").hidden = view !== "data";
  $("chat-view").hidden = !isChat;
  $("research-view").hidden = !isResearch;
  document.querySelectorAll("[data-view]").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("breadcrumb").textContent = isResearch
    ? "시장 브리핑"
    : isChat
      ? "AI 리서치 대화"
      : "데이터 워크스페이스";
  $("page-eyebrow").textContent = isResearch
    ? "MARKET BRIEFING"
    : isChat
      ? "AI RESEARCH CHAT"
      : "DATA WORKSPACE";
  $("page-title").replaceChildren(
    document.createTextNode(
      isResearch
        ? "뉴스와 지표로 읽는 시장의 온도"
        : isChat
          ? "시장을 이해하는 또 하나의 시선"
          : "숫자에서 시작하는 시장 읽기",
    ),
    el("span", "", "."),
  );
  $("page-description").textContent = isResearch
    ? "실제 뉴스, 미국 시장 심리, 원본 일봉으로 계산한 변동성을 함께 살펴보세요."
    : isChat
      ? "분석 데이터를 바탕으로 질문하고, 이전 대화의 맥락을 이어가세요."
      : "관심 지수와 종목의 데이터를 수집하고, 나의 분석 기록을 쌓아보세요.";
  if (isResearch && user && instruments.length) research.load();
}
window.addEventListener("hashchange", navigate);
navigate();
async function onAuthChange(nextUser) {
  session++;
  const ownSession = session;
  controller.abort();
  controller = new AbortController();
  user = nextUser;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  $("toast").hidden = true;
  data.reset();
  chat.reset();
  research.reset();
  instruments = [];
  $("instrument").replaceChildren();
  $("chat-instrument").replaceChildren();
  $("authenticated").hidden = !user;
  $("login-view").hidden = !!user;
  $("logout").hidden = !user;
  $("account-name").textContent =
    user?.email || user?.displayName || "로그인이 필요합니다.";
  $("account-detail").textContent = user
    ? "개인 리서치 계정"
    : "나만의 분석 데이터를 보관하세요";
  $("connection-label").textContent = user ? "계정 연결됨" : "로그인 대기";
  if (!user) {
    status("login-status");
    return;
  }
  try {
    const result = await request("/api/instruments");
    if (ownSession !== session) return;
    instruments = result.items;
    research.setInstruments(instruments);
    for (const select of [$("instrument"), $("chat-instrument")]) {
      select.replaceChildren(
        ...instruments.map((item) => {
          const option = el("option", "", `${item.name} · ${item.market}`);
          option.value = item.id;
          return option;
        }),
      );
      select.value = instruments.some((item) => item.id === "INDEX_SP500")
        ? "INDEX_SP500"
        : instruments[0]?.id || "";
    }
    $("connection-label").textContent = "API 인증 완료";
    await Promise.all([data.load(), chat.loadList()]);
    if (ownSession === session && location.hash === "#research")
      research.load();
  } catch (error) {
    if (ownSession === session && error.name !== "AbortError") {
      status("data-status", errorMessage(error), true);
      status("history-status", errorMessage(error), true);
      $("connection-label").textContent = "연결 확인 필요";
    }
  }
}
function authBusy(busy) {
  ["login-submit", "google-login"].forEach((id) => {
    $(id).disabled = busy || !ready;
  });
}
async function authenticate(action) {
  authBusy(true);
  status("login-status", "로그인하는 중…");
  try {
    await action();
    $("password").value = "";
  } catch (error) {
    status("login-status", error.message || "로그인하지 못했습니다.", true);
  } finally {
    authBusy(false);
  }
}
$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  authenticate(() => signIn($("email").value.trim(), $("password").value));
});
$("google-login").addEventListener("click", () => authenticate(signInGoogle));
$("logout").addEventListener("click", async () => {
  try {
    await signOut();
  } catch (error) {
    status("data-status", error.message, true);
  }
});
// Mobile account control remains visible without the desktop sidebar footer.
const mobileLogout = el("button", "button subtle account-mobile", "로그아웃");
mobileLogout.id = "mobile-logout";
mobileLogout.hidden = true;
mobileLogout.addEventListener("click", () => $("logout").click());
document.querySelector(".topbar").append(mobileLogout);
const observer = new MutationObserver(() => {
  mobileLogout.hidden = $("logout").hidden;
});
observer.observe($("logout"), {
  attributes: true,
  attributeFilter: ["hidden"],
});
authBusy(true);
try {
  api = createApi({ baseUrl: config.apiBaseUrl, getToken });
  await initializeAuth(onAuthChange);
  ready = true;
  authBusy(false);
} catch (error) {
  status("login-status", error.message || "로그인 설정을 확인하세요.", true);
  $("connection-label").textContent = "인증 설정 필요";
}
