import { defineConfig } from "@playwright/test";

// The six presentation slide screenshots (capture-slides.ts), from the running stack after the
// walkthrough recording. Like the other configs here: one run, no retries.
export default defineConfig({
  testDir: ".",
  testMatch: "capture-slides.ts",
  timeout: 6 * 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  reporter: "list",
  outputDir: "./out/test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "https://localhost:8443",
    ignoreHTTPSErrors: true,
    actionTimeout: 20_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
