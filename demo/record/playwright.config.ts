import { defineConfig } from "@playwright/test";

// TASK-093: records the fallback demo video by driving the web UI on the running stack
// (demo/demo-script.md section 7.1). It is a recording, not a test suite: one long run, no retries,
// and it stops at the first step whose outcome the demo script does not allow.
// `@playwright/test` resolves to the copy in apps/web through tsconfig.json `paths`, so this
// directory adds no dependency of its own. Playwright's ESM loader needs index.mjs there; type-check
// with `npx tsc -p ../../demo/record/tsconfig.typecheck.json` (from apps/web), which maps the same
// import to the package's index.d.ts.
export default defineConfig({
  testDir: ".",
  testMatch: "record-demo.ts",
  timeout: 20 * 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  reporter: "list",
  outputDir: "./out/test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "https://localhost:8443",
    // The local dev CA (infra/certs/ca.crt) is not in Chromium's trust store; see apps/web's config.
    ignoreHTTPSErrors: true,
    actionTimeout: 20_000,
    launchOptions: { slowMo: Number(process.env.DEMO_SLOWMO_MS ?? "120") },
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
