// Docs tooling only. Puppeteer must not download its own browser: scripts/render_diagrams.sh points it
// at the Chromium that Playwright already installed for the E2E suite (puppeteer-config.json).
module.exports = { skipDownload: true };
