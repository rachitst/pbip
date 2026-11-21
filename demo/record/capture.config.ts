import { defineConfig } from "@playwright/test";

// UI redesign: before/after screenshots of every page (capture-screens.ts). Read-only, runs on the
// local seeded stack like the recordings in this directory. SCREENS_SET=before|after picks the folder
// under docs/design/screenshots/.
export default defineConfig({
  testDir: ".",
  testMatch: "capture-screens.ts",
  timeout: 5 * 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  reporter: "list",
  outputDir: "./out/test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "https://localhost:8443",
    ignoreHTTPSErrors: true,
    actionTimeout: 20_000,
    colorScheme: "light",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
