import { defineConfig } from "@playwright/test";

// Item 14: records the A-to-Z walkthrough (record-walkthrough.ts) on the running stack. Like
// playwright.config.ts it is a recording, not a test suite: one long run, no retries, and it stops
// at the first step whose API answer the story does not allow. See README.md in this directory.
export default defineConfig({
  testDir: ".",
  testMatch: "record-walkthrough.ts",
  timeout: 60 * 60_000,
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
    // No slowMo: forms are filled at once and the pauses are explicit holds (DEMO_PACE scales them).
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
