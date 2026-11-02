import { defineConfig } from "@playwright/test";

// E2E runs against the full Docker Compose stack over HTTPS (web on :8443 proxies /api to the API).
// The certificate is from the throwaway local dev CA (scripts/dev_certs.py), which the browser does not
// trust; CI verifies that chain separately with `curl --cacert` and scripts/verify_tls.sh.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "https://localhost:8443",
    ignoreHTTPSErrors: true,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
