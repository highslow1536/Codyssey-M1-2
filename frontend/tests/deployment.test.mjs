import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { build, resolvePublicConfig } from "../scripts/build.mjs";

const publicEnv = {
  API_BASE_URL: "https://assistant-api.onrender.com",
  FIREBASE_WEB_CONFIG_JSON: JSON.stringify({
    apiKey: "public-test-key",
    authDomain: "demo-deployment.firebaseapp.com",
    projectId: "demo-deployment",
    appId: "1:123:web:test",
  }),
};

for (const stage of ["production", "preview"]) {
  test(`Vercel ${stage} rejects incomplete or local configuration`, () => {
    for (const changes of [
      { API_BASE_URL: "" },
      { API_BASE_URL: "http://assistant-api.onrender.com" },
      { API_BASE_URL: "https://localhost:8000" },
      { API_BASE_URL: "https://[::1]:8000" },
      { FIREBASE_WEB_CONFIG_JSON: "" },
      { FRONTEND_AUTH_EMULATOR_URL: "http://127.0.0.1:9099" },
    ]) {
      assert.throws(() =>
        resolvePublicConfig({
          env: { ...publicEnv, VERCEL_ENV: stage, ...changes },
        }),
      );
    }
    assert.equal(
      resolvePublicConfig({ env: { ...publicEnv, VERCEL_ENV: stage } })
        .authEmulatorUrl,
      null,
    );
  });
}

test("deployment build packages UI and only public configuration", async () => {
  const outDir = await mkdtemp(path.join(tmpdir(), "assistant-deploy-"));
  try {
    const result = await build({
      outDir,
      env: {
        ...publicEnv,
        VERCEL_ENV: "production",
        OPENAI_API_KEY: "private-openai-sentinel",
        FIREBASE_SERVICE_ACCOUNT_JSON: "private-account-sentinel",
        DISCORD_WEBHOOK_URL: "private-discord-sentinel",
      },
    });
    assert.deepEqual(result.missing, []);
    const config = await readFile(path.join(outDir, "config.js"), "utf8");
    assert.match(config, /assistant-api.onrender.com/);
    assert.doesNotMatch(config, /private-|VERCEL_ENV/);
    assert.match(
      await readFile(path.join(outDir, "index.html"), "utf8"),
      /config.js/,
    );
    assert.match(
      await readFile(path.join(outDir, "js", "app.js"), "utf8"),
      /import/,
    );
    await assert.rejects(build({ outDir, env: { VERCEL_ENV: "production" } }));
    // Invalid replacement builds must preserve the last valid output.
    assert.equal(
      await readFile(path.join(outDir, "config.js"), "utf8"),
      config,
    );
  } finally {
    await rm(outDir, { recursive: true, force: true });
  }
});
