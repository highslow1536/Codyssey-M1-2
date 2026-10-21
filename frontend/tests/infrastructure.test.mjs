// Infrastructure tests for the vanilla frontend (Node built-in runner, no dependencies).
// Run from frontend/: node --test tests/*.test.mjs

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createApi, validateBaseUrl } from "../js/api.js";
import {
  assertHttpUrl,
  build,
  generateConfigSource,
  isLocalhostUrl,
  parseFirebaseWebConfig,
  resolvePublicConfig,
  sanitizeForInlineScript,
} from "../scripts/build.mjs";
import { assertEmulatorAllowed } from "../js/auth.js";

const here = fileURLToPath(import.meta.url);
const frontendDir = path.dirname(path.dirname(here));

function mockFetch(handler) {
  const calls = [];
  const fn = function (url, init) {
    calls.push({ url: url, init: init });
    return handler(url, init, calls.length);
  };
  fn.calls = calls;
  return fn;
}

function jsonResponse(status, payload, headerMap) {
  const bodyText = payload === undefined ? "" : JSON.stringify(payload);
  const headers = headerMap || {};
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: {
      get: function (name) {
        const key = String(name).toLowerCase();
        return headers[key] === undefined ? null : headers[key];
      },
    },
    text: function () {
      return Promise.resolve(bodyText);
    },
  };
}

function textResponse(status, bodyText, contentType) {
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: {
      get: function (name) {
        return String(name).toLowerCase() === "content-type"
          ? contentType || "text/plain"
          : null;
      },
    },
    text: function () {
      return Promise.resolve(bodyText);
    },
  };
}

const FIREBASE_OK = {
  apiKey: "test-api-key",
  authDomain: "test.firebaseapp.com",
  projectId: "test-project",
  appId: "1:123:web:abc",
};

describe("validateBaseUrl", function () {
  it("accepts https and strips trailing slashes", function () {
    assert.equal(
      validateBaseUrl("https://api.example.com///"),
      "https://api.example.com",
    );
  });
  it("rejects empty, relative and non-http(s) values", function () {
    assert.throws(function () {
      validateBaseUrl("");
    });
    assert.throws(function () {
      validateBaseUrl("/api");
    });
    assert.throws(function () {
      validateBaseUrl("ftp://api.example.com");
    });
  });
});

describe("createApi request", function () {
  it("attaches bearer token and JSON body", async function () {
    const fetchImpl = mockFetch(function () {
      return jsonResponse(200, { ok: true });
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      getToken: function () {
        return Promise.resolve("token-1");
      },
      fetchImpl: fetchImpl,
    });
    const result = await api.request("/api/data", {
      method: "POST",
      body: { a: 1 },
    });
    assert.deepEqual(result, { ok: true });
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(fetchImpl.calls[0].url, "https://api.example.com/api/data");
    assert.equal(
      fetchImpl.calls[0].init.headers.Authorization,
      "Bearer token-1",
    );
    assert.equal(
      fetchImpl.calls[0].init.headers["Content-Type"],
      "application/json",
    );
    assert.equal(fetchImpl.calls[0].init.body, JSON.stringify({ a: 1 }));
  });

  it("retries once on 401 with a refreshed token", async function () {
    const seen = [];
    const fetchImpl = mockFetch(function (url, init, n) {
      seen.push(init.headers.Authorization || null);
      if (n === 1)
        return jsonResponse(401, { code: "unauthorized", message: "expired" });
      return jsonResponse(200, { ok: true });
    });
    const tokenCalls = [];
    const api = createApi({
      baseUrl: "https://api.example.com",
      getToken: function (force) {
        tokenCalls.push(force);
        return Promise.resolve(force ? "token-2" : "token-1");
      },
      fetchImpl: fetchImpl,
    });
    const result = await api.request("/api/data");
    assert.deepEqual(result, { ok: true });
    assert.deepEqual(seen, ["Bearer token-1", "Bearer token-2"]);
    assert.deepEqual(tokenCalls, [false, true]);
  });

  it("throws after a second 401 with status, code and requestId", async function () {
    const fetchImpl = mockFetch(function () {
      return jsonResponse(
        401,
        { code: "unauthorized", message: "nope" },
        { "x-request-id": "req-1" },
      );
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      getToken: function (force) {
        return Promise.resolve(force ? "t2" : "t1");
      },
      fetchImpl: fetchImpl,
    });
    const error = await api.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(fetchImpl.calls.length, 2);
    assert.equal(error.status, 401);
    assert.equal(error.code, "unauthorized");
    assert.equal(error.requestId, "req-1");
  });

  it("does not retry 500 errors", async function () {
    const fetchImpl = mockFetch(function () {
      return jsonResponse(500, { code: "x", message: "" });
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: fetchImpl,
    });
    const error = await api.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(fetchImpl.calls.length, 1);
    assert.equal(error.status, 500);
  });

  it("reports network failure in Korean with network_error code", async function () {
    const fetchImpl = mockFetch(function () {
      return Promise.reject(new Error("boom"));
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: fetchImpl,
    });
    const error = await api.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(error.code, "network_error");
    assert.match(error.message, /네트워크/);
  });

  it("propagates abort instead of wrapping it", async function () {
    const fetchImpl = mockFetch(function () {
      const e = new Error("aborted");
      e.name = "AbortError";
      return Promise.reject(e);
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: fetchImpl,
    });
    const controller = new AbortController();
    const error = await api
      .request("/api/data", { signal: controller.signal })
      .then(
        function () {
          throw new Error("should have thrown");
        },
        function (e) {
          return e;
        },
      );
    assert.equal(error.name, "AbortError");
  });

  it("rejects non-JSON success bodies with response_error", async function () {
    const apiText = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: mockFetch(function () {
        return textResponse(200, "<html>not json</html>");
      }),
    });
    const error = await apiText.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(error.code, "response_error");
    assert.equal(error.status, 200);
    assert.match(error.message, /형식/);
  });

  it("returns null for empty and 204 success bodies", async function () {
    const apiEmpty = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: mockFetch(function () {
        return textResponse(200, "");
      }),
    });
    assert.equal(await apiEmpty.request("/api/data"), null);
    const api204 = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: mockFetch(function () {
        return jsonResponse(204, undefined);
      }),
    });
    assert.equal(await api204.request("/api/data"), null);
  });

  it("rejects external URLs and non-JSON bodies", async function () {
    const api = createApi({
      baseUrl: "https://api.example.com",
      fetchImpl: mockFetch(function () {
        return jsonResponse(200, {});
      }),
    });
    assert.throws(function () {
      api.request("https://evil.example.com/x");
    });
    assert.throws(function () {
      api.request("//evil.example.com/x");
    });
    await assert.rejects(
      api.request("/api/data", { method: "POST", body: "nope" }),
    );
  });
});

describe("firebase config allowlist", function () {
  it("returns null when the raw value is missing", function () {
    assert.equal(parseFirebaseWebConfig(""), null);
    assert.equal(parseFirebaseWebConfig(undefined), null);
  });

  it("rejects service account keys mistaken for web config", function () {
    const serviceAccount = JSON.stringify({
      type: "service_account",
      project_id: "x",
      private_key: "secret",
    });
    assert.throws(function () {
      parseFirebaseWebConfig(serviceAccount);
    }, /서비스 계정/);
  });

  it("keeps only allowlisted web keys and requires the core four", function () {
    const raw = JSON.stringify(
      Object.assign({ evil: "drop-me", private: "drop-me-too" }, FIREBASE_OK),
    );
    assert.deepEqual(parseFirebaseWebConfig(raw), FIREBASE_OK);
    assert.throws(function () {
      parseFirebaseWebConfig(JSON.stringify({ apiKey: "k" }));
    });
    assert.throws(function () {
      parseFirebaseWebConfig("not-json");
    });
  });
});

describe("config generation", function () {
  it("escapes markup-breaking characters in the generated source", function () {
    const backslash = String.fromCharCode(92);
    const line = String.fromCharCode(8232);
    const source = generateConfigSource({
      apiBaseUrl: "https://api.example.com/?a=<b",
      firebase: FIREBASE_OK,
      authEmulatorUrl: null,
    });
    assert.match(source, /window\.APP_CONFIG/);
    assert.ok(source.indexOf(backslash + "u003c") !== -1);
    assert.equal(source.indexOf("<"), -1);
    assert.ok(
      sanitizeForInlineScript("a" + line + "b").indexOf(backslash + "u2028") !==
        -1,
    );
  });

  it("allows missing firebase config (setup state)", function () {
    const source = generateConfigSource({
      apiBaseUrl: "",
      firebase: null,
      authEmulatorUrl: null,
    });
    assert.ok(source.indexOf('"firebase":null') !== -1);
  });

  it("enforces localhost for the auth emulator", function () {
    assert.equal(isLocalhostUrl("http://localhost:9099"), true);
    assert.equal(isLocalhostUrl("http://127.0.0.1:9099"), true);
    assert.equal(isLocalhostUrl("https://auth.example.com"), false);
    assert.throws(function () {
      resolvePublicConfig({
        env: { FRONTEND_AUTH_EMULATOR_URL: "https://auth.example.com" },
      });
    }, /localhost/);
    const ok = resolvePublicConfig({
      env: {
        API_BASE_URL: "https://api.example.com",
        FRONTEND_AUTH_EMULATOR_URL: "http://localhost:9099",
      },
    });
    assert.equal(ok.authEmulatorUrl, "http://localhost:9099");
  });

  it("build writes dist/config.js without secrets", async function () {
    const outDir = await mkdtemp(path.join(tmpdir(), "frontend-dist-"));
    try {
      const summary = await build({
        frontendDir: frontendDir,
        outDir: outDir,
        env: {
          API_BASE_URL: "https://api.example.com",
          FIREBASE_WEB_CONFIG_JSON: JSON.stringify(FIREBASE_OK),
        },
      });
      const source = await readFile(path.join(outDir, "config.js"), "utf8");
      assert.match(source, /window\.APP_CONFIG/);
      assert.ok(source.indexOf("test-api-key") !== -1);
      assert.equal(summary.firebase, "configured");
    } finally {
      await rm(outDir, { recursive: true, force: true });
    }
  });

  it("build succeeds with empty env (setup state)", async function () {
    const outDir = await mkdtemp(path.join(tmpdir(), "frontend-dist-"));
    try {
      await build({ frontendDir: frontendDir, outDir: outDir, env: {} });
      const source = await readFile(path.join(outDir, "config.js"), "utf8");
      assert.ok(source.indexOf('"firebase":null') !== -1);
    } finally {
      await rm(outDir, { recursive: true, force: true });
    }
  });
});

describe("token provider failures", function () {
  it("propagates initial getToken failure without sending", async function () {
    const fetchImpl = mockFetch(function () {
      return jsonResponse(200, {});
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      getToken: function () {
        return Promise.reject(new Error("token boom"));
      },
      fetchImpl: fetchImpl,
    });
    const error = await api.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(error.message, "token boom");
    assert.equal(fetchImpl.calls.length, 0);
  });

  it("propagates refresh getToken failure on 401", async function () {
    const fetchImpl = mockFetch(function () {
      return jsonResponse(401, { code: "unauthorized" });
    });
    const api = createApi({
      baseUrl: "https://api.example.com",
      getToken: function (force) {
        return force
          ? Promise.reject(new Error("refresh boom"))
          : Promise.resolve("t1");
      },
      fetchImpl: fetchImpl,
    });
    const error = await api.request("/api/data").then(
      function () {
        throw new Error("should have thrown");
      },
      function (e) {
        return e;
      },
    );
    assert.equal(error.message, "refresh boom");
    assert.equal(fetchImpl.calls.length, 1);
  });
});

describe("base URL credential hygiene", function () {
  it("rejects credentials, query and hash (runtime)", function () {
    assert.throws(function () {
      validateBaseUrl("https://user:pass@api.example.com");
    });
    assert.throws(function () {
      validateBaseUrl("https://api.example.com/?x=1");
    });
    assert.throws(function () {
      validateBaseUrl("https://api.example.com/#frag");
    });
    assert.throws(function () {
      createApi({
        baseUrl: "https://user@api.example.com",
        fetchImpl: mockFetch(function () {
          return jsonResponse(200, {});
        }),
      });
    });
  });

  it("rejects credentials, query and hash (build)", function () {
    assert.throws(function () {
      assertHttpUrl("https://user:pass@api.example.com", "API_BASE_URL");
    });
    assert.throws(function () {
      assertHttpUrl("https://api.example.com/?x=1", "API_BASE_URL");
    });
    assert.throws(function () {
      assertHttpUrl("https://api.example.com/#frag", "API_BASE_URL");
    });
    assert.equal(
      assertHttpUrl("https://api.example.com/v1", "API_BASE_URL"),
      "https://api.example.com/v1",
    );
  });
});

describe("auth emulator fail-closed", function () {
  it("returns null when unconfigured", function () {
    assert.equal(assertEmulatorAllowed("", "localhost"), null);
    assert.equal(assertEmulatorAllowed(null, "localhost"), null);
  });

  it("accepts localhost app with localhost emulator", function () {
    assert.equal(
      assertEmulatorAllowed("http://localhost:9099", "localhost"),
      "http://localhost:9099",
    );
    assert.equal(
      assertEmulatorAllowed("http://127.0.0.1:9099", "localhost"),
      "http://127.0.0.1:9099",
    );
  });

  it("throws for nonlocal app host or emulator URL", function () {
    assert.throws(function () {
      assertEmulatorAllowed("http://localhost:9099", "app.example.com");
    });
    assert.throws(function () {
      assertEmulatorAllowed("https://auth.example.com", "localhost");
    });
    assert.throws(function () {
      assertEmulatorAllowed("not-a-url", "localhost");
    });
  });
});

describe("auth module contract (static)", function () {
  it("pins the CDN, uses session persistence and guards the emulator", async function () {
    const source = await readFile(
      path.join(frontendDir, "js", "auth.js"),
      "utf8",
    );
    for (const needle of [
      "12.19.0",
      "firebase-app.js",
      "firebase-auth.js",
      "browserSessionPersistence",
      "onAuthStateChanged",
      "connectAuthEmulator",
      "localhost",
      "signInWithEmailAndPassword",
      "signInWithPopup",
      "getIdToken",
      "getToken",
      "signInGoogle",
      "authError",
      "assertEmulatorAllowed",
    ]) {
      assert.ok(source.indexOf(needle) !== -1, "missing: " + needle);
    }
  });
});
