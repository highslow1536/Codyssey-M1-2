// Build script for the vanilla frontend (Node >= 22, no dependencies).
// Only three env keys become public configuration (allowlist):
//   API_BASE_URL, FIREBASE_WEB_CONFIG_JSON, FRONTEND_AUTH_EMULATOR_URL
// VERCEL_ENV controls deployment validation and is never emitted.
// Output: frontend/dist/ with index.html, styles.css, js/ (when present) and generated config.js.
// Local builds allow missing Firebase config (setup state); Vercel builds fail closed.
// Never removes anything outside dist. Run directly: node frontend/scripts/build.mjs

import { cp, mkdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const ALLOWLISTED_ENV_KEYS = [
  "API_BASE_URL",
  "FIREBASE_WEB_CONFIG_JSON",
  "FRONTEND_AUTH_EMULATOR_URL",
];
export const ALLOWED_FIREBASE_KEYS = [
  "apiKey",
  "authDomain",
  "projectId",
  "storageBucket",
  "messagingSenderId",
  "appId",
  "measurementId",
];
export const REQUIRED_FIREBASE_KEYS = [
  "apiKey",
  "authDomain",
  "projectId",
  "appId",
];

export function parseFirebaseWebConfig(raw) {
  if (raw === undefined || raw === null || String(raw).trim() === "")
    return null;
  let parsed;
  try {
    parsed = JSON.parse(String(raw));
  } catch (ignored) {
    throw new Error("FIREBASE_WEB_CONFIG_JSON이 올바른 JSON이 아닙니다.");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("FIREBASE_WEB_CONFIG_JSON이 올바른 형식이 아닙니다.");
  }
  if (
    parsed.type === "service_account" ||
    parsed.private_key ||
    parsed.privateKey ||
    parsed.client_email ||
    parsed.clientEmail
  ) {
    throw new Error(
      "FIREBASE_WEB_CONFIG_JSON에 서비스 계정 키가 들어 있습니다. 웹 클라이언트용 Firebase 구성을 사용하세요.",
    );
  }
  const filtered = {};
  for (const key of ALLOWED_FIREBASE_KEYS) {
    if (parsed[key] === undefined) continue;
    if (typeof parsed[key] !== "string" || !parsed[key].trim()) {
      throw new Error("Firebase 구성 값(" + key + ")이 올바르지 않습니다.");
    }
    filtered[key] = parsed[key];
  }
  if (Object.keys(filtered).length === 0) {
    throw new Error(
      "FIREBASE_WEB_CONFIG_JSON에 허용된 Firebase 키가 없습니다.",
    );
  }
  for (const key of REQUIRED_FIREBASE_KEYS) {
    if (!filtered[key]) {
      throw new Error("Firebase 구성에 " + key + " 값이 필요합니다.");
    }
  }
  return filtered;
}

export function assertHttpUrl(raw, label) {
  let parsed;
  try {
    parsed = new URL(String(raw).trim());
  } catch (ignored) {
    throw new Error(label + " 주소가 올바르지 않습니다.");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(label + " 주소는 http(s) 주소여야 합니다.");
  }
  if (parsed.username || parsed.password) {
    throw new Error(label + " 주소에 계정 정보가 포함되어 있습니다.");
  }
  if (parsed.search !== "" || parsed.hash !== "") {
    throw new Error(label + " 주소에 쿼리나 해시를 포함할 수 없습니다.");
  }
  return parsed.toString().replace(/\/+$/, "");
}

export function isLocalhostUrl(raw) {
  let parsed;
  try {
    parsed = new URL(String(raw));
  } catch (ignored) {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  return (
    parsed.hostname === "localhost" ||
    parsed.hostname.endsWith(".localhost") ||
    parsed.hostname === "127.0.0.1" ||
    parsed.hostname === "[::1]"
  );
}

export function sanitizeForInlineScript(json) {
  // '<', U+2028, U+2029 would break out of (or corrupt) an inline <script> block,
  // so they are emitted as unicode escapes. Backslash is built from its char code
  // to keep the escape sequence in the generated text.
  const backslash = String.fromCharCode(92);
  return json
    .split(String.fromCharCode(60))
    .join(backslash + "u003c")
    .split(String.fromCharCode(8232))
    .join(backslash + "u2028")
    .split(String.fromCharCode(8233))
    .join(backslash + "u2029");
}

export function generateConfigSource(input) {
  const data = input || {};
  const config = {
    apiBaseUrl: data.apiBaseUrl || "",
    firebase: data.firebase || null,
    authEmulatorUrl: data.authEmulatorUrl || null,
  };
  return (
    "window.APP_CONFIG = " +
    sanitizeForInlineScript(JSON.stringify(config)) +
    ";" +
    String.fromCharCode(10)
  );
}

export function loadPublicEnv(input) {
  const opts = input || {};
  const repoRoot = opts.repoRoot;
  const source = opts.env || process.env;
  if (!opts.env && repoRoot && typeof process.loadEnvFile === "function") {
    // process.loadEnvFile never overrides preexisting variables: explicit env wins.
    try {
      process.loadEnvFile(path.join(repoRoot, ".env"));
    } catch (ignored) {
      // No root .env file: environment variables alone are enough.
    }
  }
  const picked = {};
  for (const key of ALLOWLISTED_ENV_KEYS) {
    picked[key] = source[key] || "";
  }
  return picked;
}

export function resolvePublicConfig(input) {
  const opts = input || {};
  const publicEnv = loadPublicEnv(opts);
  const apiRaw = publicEnv.API_BASE_URL.trim();
  const emulatorRaw = publicEnv.FRONTEND_AUTH_EMULATOR_URL.trim();
  let apiBaseUrl = "";
  if (apiRaw) apiBaseUrl = assertHttpUrl(apiRaw, "API_BASE_URL");
  let authEmulatorUrl = null;
  if (emulatorRaw) {
    authEmulatorUrl = assertHttpUrl(emulatorRaw, "FRONTEND_AUTH_EMULATOR_URL");
    if (!isLocalhostUrl(authEmulatorUrl)) {
      throw new Error(
        "FRONTEND_AUTH_EMULATOR_URL은 로컬(localhost) 주소만 사용할 수 있습니다.",
      );
    }
  }
  const config = {
    apiBaseUrl: apiBaseUrl,
    firebase: parseFirebaseWebConfig(publicEnv.FIREBASE_WEB_CONFIG_JSON),
    authEmulatorUrl: authEmulatorUrl,
  };
  const deploymentEnv = (opts.env || process.env).VERCEL_ENV;
  if (deploymentEnv === "production" || deploymentEnv === "preview") {
    if (
      !apiBaseUrl ||
      new URL(apiBaseUrl).protocol !== "https:" ||
      isLocalhostUrl(apiBaseUrl)
    ) {
      throw new Error("Vercel 배포에는 외부 HTTPS API_BASE_URL이 필요합니다.");
    }
    if (!config.firebase) {
      throw new Error("Vercel 배포에는 FIREBASE_WEB_CONFIG_JSON이 필요합니다.");
    }
    if (authEmulatorUrl) {
      throw new Error(
        "Vercel 배포에서는 인증 에뮬레이터를 사용할 수 없습니다.",
      );
    }
  }
  return config;
}

export async function build(input) {
  const opts = input || {};
  const here = fileURLToPath(import.meta.url);
  const frontendDir = opts.frontendDir || path.dirname(path.dirname(here));
  const repoRoot = opts.repoRoot || path.dirname(frontendDir);
  const outDir = opts.outDir || path.join(frontendDir, "dist");
  const config = resolvePublicConfig({ repoRoot: repoRoot, env: opts.env });
  await rm(outDir, { recursive: true, force: true });
  await mkdir(outDir, { recursive: true });
  const copied = [];
  const missing = [];
  for (const name of ["index.html", "styles.css"]) {
    const src = path.join(frontendDir, name);
    try {
      await stat(src);
      await cp(src, path.join(outDir, name));
      copied.push(name);
    } catch (ignored) {
      missing.push(name);
    }
  }
  try {
    await stat(path.join(frontendDir, "js"));
    await cp(path.join(frontendDir, "js"), path.join(outDir, "js"), {
      recursive: true,
    });
    copied.push("js/");
  } catch (ignored) {
    missing.push("js/");
  }
  await writeFile(
    path.join(outDir, "config.js"),
    generateConfigSource(config),
    "utf8",
  );
  if (missing.length) {
    console.warn(
      "frontend build: UI 파일이 아직 없어 건너뜁니다: " + missing.join(", "),
    );
  }
  return {
    outDir: outDir,
    copied: copied,
    missing: missing,
    firebase: config.firebase ? "configured" : "missing",
  };
}

const invokedDirectly =
  typeof process.argv[1] === "string" &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  try {
    const summary = await build();
    console.log(
      "frontend build complete: " +
        summary.outDir +
        " (firebase: " +
        summary.firebase +
        ")",
    );
  } catch (error) {
    console.error(
      "frontend build failed: " +
        (error && error.message ? error.message : error),
    );
    process.exitCode = 1;
  }
}
