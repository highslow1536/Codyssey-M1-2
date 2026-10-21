export const $ = (id) => document.getElementById(id);
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export const number = (value) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(
        value,
      );
export const unit = (currency) =>
  ({ KRW: "원", USD: "USD", POINT: "pt" })[currency] || currency || "";
export function dateTime(value) {
  if (!value) return "시점 정보 없음";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "시점 정보 없음"
    : new Intl.DateTimeFormat("ko-KR", {
        dateStyle: "short",
        timeStyle: "short",
      }).format(date);
}
export function localDate(date = new Date()) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
export function status(id, text = "", error = false) {
  $(id).textContent = text;
  $(id).classList.toggle("error", error);
}
export function errorMessage(error) {
  if (error.status === 403)
    return "이 계정은 API 접근 권한이 없습니다. 관리자에게 Firebase UID 허용 여부를 확인하세요.";
  if (error.status === 409)
    return `${error.message} 최신 내용을 다시 불러온 후 확인하세요.`;
  return (
    error.message || "요청을 완료하지 못했습니다. 잠시 후 다시 시도하세요."
  );
}
let toastTimer;
export function toast(text) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}
export function safeLink(url) {
  try {
    const parsed = new URL(url);
    return ["https:", "http:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}
export function confirmAction(title, description, action = "삭제하기") {
  const dialog = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-description").textContent = description;
  $("confirm-ok").textContent = action;
  return new Promise((resolve) => {
    const finish = (result) => {
      dialog.close();
      cleanup();
      resolve(result);
    };
    const cancel = (event) => {
      event.preventDefault();
      finish(false);
    };
    const yes = () => finish(true),
      no = () => finish(false);
    const cleanup = () => {
      $("confirm-ok").removeEventListener("click", yes);
      $("confirm-cancel").removeEventListener("click", no);
      dialog.removeEventListener("cancel", cancel);
      dialog.removeEventListener("close", closed);
    };
    const closed = () => {
      cleanup();
      resolve(false);
    };
    $("confirm-ok").addEventListener("click", yes);
    $("confirm-cancel").addEventListener("click", no);
    dialog.addEventListener("cancel", cancel);
    dialog.addEventListener("close", closed);
    dialog.showModal();
    $("confirm-cancel").focus();
  });
}
