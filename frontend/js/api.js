// Vanilla JS API client for the existing backend (no dependencies).
// The token provider must succeed before any request is sent; its errors propagate.
// Only JSON (or empty/204) success bodies are accepted; anything else is a response_error.
//
// Usage:
//   import { createApi } from './api.js';
//   const api = createApi({ baseUrl: window.APP_CONFIG.apiBaseUrl, getToken });
//   const items = await api.request('/api/data?limit=50');

const ABSOLUTE_URL_RE = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const BACKSLASH = String.fromCharCode(92);

export function validateBaseUrl(baseUrl) {
  const raw = String(
    baseUrl === undefined || baseUrl === null ? "" : baseUrl,
  ).trim();
  if (!raw) {
    throw new Error("API 주소가 설정되지 않았습니다. 관리자에게 문의하세요.");
  }
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (ignored) {
    throw new Error("API 주소가 올바르지 않습니다. 관리자에게 문의하세요.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(
      "API 주소는 http(s) 주소여야 합니다. 관리자에게 문의하세요.",
    );
  }
  if (parsed.username || parsed.password) {
    throw new Error(
      "API 주소에 계정 정보가 포함되어 있습니다. 관리자에게 문의하세요.",
    );
  }
  if (parsed.search !== "" || parsed.hash !== "") {
    throw new Error(
      "API 주소에 쿼리나 해시를 포함할 수 없습니다. 관리자에게 문의하세요.",
    );
  }
  return parsed.toString().replace(/\/+$/, "");
}

function validatePath(path) {
  if (
    typeof path !== "string" ||
    path.charAt(0) !== "/" ||
    path.indexOf("//") === 0
  ) {
    throw new Error("요청 경로가 올바르지 않습니다.");
  }
  if (ABSOLUTE_URL_RE.test(path) || path.indexOf(BACKSLASH) !== -1) {
    throw new Error("요청 경로가 올바르지 않습니다.");
  }
  return path;
}

function errorFromResponse(status, payload, requestId) {
  const data = payload && typeof payload === "object" ? payload : null;
  let message;
  if (data && typeof data.message === "string" && data.message) {
    message = data.message;
  } else if (status === 401) {
    message = "인증이 만료되었습니다. 다시 로그인해 주세요.";
  } else if (status === 403) {
    message = "이 작업을 수행할 권한이 없습니다.";
  } else if (status === 404) {
    message = "요청한 항목을 찾을 수 없습니다.";
  } else if (status >= 500) {
    message = "서버에 일시적인 문제가 있습니다. 잠시 후 다시 시도해 주세요.";
  } else {
    message = "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  const error = new Error(message);
  error.status = status;
  error.code =
    data && typeof data.code === "string" && data.code
      ? data.code
      : "http_" + status;
  if (data && typeof data.request_id === "string" && data.request_id) {
    error.requestId = data.request_id;
  } else {
    error.requestId = requestId || null;
  }
  return error;
}

function toNetworkError() {
  const error = new Error(
    "네트워크 연결에 실패했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.",
  );
  error.code = "network_error";
  return error;
}

async function readBody(response) {
  const bodyText = await response.text();
  if (!bodyText) return { json: null, text: "" };
  const contentType = response.headers.get("content-type") || "";
  if (contentType.indexOf("application/json") === -1) {
    try {
      return { json: JSON.parse(bodyText), text: bodyText };
    } catch (ignored) {
      return { json: null, text: bodyText };
    }
  }
  try {
    return { json: JSON.parse(bodyText), text: bodyText };
  } catch (ignored) {
    return { json: null, text: bodyText };
  }
}

export function createApi(options) {
  const opts = options || {};
  const base = validateBaseUrl(opts.baseUrl);
  const getToken = opts.getToken;
  const fetchImpl =
    opts.fetchImpl === undefined ? globalThis.fetch : opts.fetchImpl;
  if (
    getToken !== undefined &&
    getToken !== null &&
    typeof getToken !== "function"
  ) {
    throw new Error("getToken은 함수여야 합니다.");
  }
  if (typeof fetchImpl !== "function") {
    throw new Error("fetch 함수를 사용할 수 없습니다.");
  }

  async function perform(path, method, body, signal, token, retried) {
    const headers = {};
    const init = { method: method, headers: headers };
    if (signal !== undefined && signal !== null) init.signal = signal;
    if (body !== undefined) {
      if (
        body === null ||
        typeof body !== "object" ||
        typeof body.append === "function"
      ) {
        throw new Error("요청 본문은 JSON 객체 형태여야 합니다.");
      }
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    if (token) {
      headers.Authorization = "Bearer " + token;
    }
    let response;
    try {
      response = await fetchImpl(base + path, init);
    } catch (error) {
      if ((signal && signal.aborted) || (error && error.name === "AbortError"))
        throw error;
      throw toNetworkError();
    }
    if (signal && signal.aborted) {
      const aborted = new Error("요청이 취소되었습니다.");
      aborted.name = "AbortError";
      throw aborted;
    }
    const requestId = response.headers.get("x-request-id");
    if (response.status === 401 && getToken && !retried) {
      const nextToken = await getToken(true);
      if (nextToken && nextToken !== token) {
        return perform(path, method, body, signal, nextToken, true);
      }
      const failed = await readBody(response);
      throw errorFromResponse(401, failed.json, requestId);
    }
    if (response.status === 204) return null;
    const parsed = await readBody(response);
    if (response.ok) {
      if (parsed.json !== null) return parsed.json;
      if (parsed.text === "") return null;
      const invalid = new Error(
        "서버 응답 형식이 올바르지 않습니다. 잠시 후 다시 시도해 주세요.",
      );
      invalid.code = "response_error";
      invalid.status = response.status;
      invalid.requestId = requestId;
      throw invalid;
    }
    throw errorFromResponse(response.status, parsed.json, requestId);
  }

  return {
    baseUrl: base,
    request: function request(path, reqOptions) {
      const reqOpts = reqOptions || {};
      const safePath = validatePath(path);
      const method = reqOpts.method === undefined ? "GET" : reqOpts.method;
      const body = reqOpts.body;
      const signal = reqOpts.signal;
      const tokenPromise = getToken
        ? Promise.resolve().then(function () {
            return getToken(false);
          })
        : Promise.resolve(null);
      return tokenPromise.then(function (token) {
        if (
          token !== null &&
          token !== undefined &&
          typeof token !== "string"
        ) {
          throw new Error(
            "인증 토큰이 올바르지 않습니다. 다시 로그인해 주세요.",
          );
        }
        return perform(safePath, method, body, signal, token || null, false);
      });
    },
  };
}
