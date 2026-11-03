import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// TASK-089 bug bash layout regressions. jsdom applies no stylesheets, so (like the notification
// and audit-log style tests) these read the CSS source: the rule must exist, say the right thing
// and be imported by every page that renders the markup. Paths are relative to apps/web.

function source(path: string): string {
  return readFileSync(path, "utf8");
}

/** The declarations of the first rule whose selector is exactly ``selector``. */
function rule(css: string, selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `no rule for ${selector}`).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("radio choices (Statements format, risk profile)", () => {
  const css = source("src/pages/ChoiceGroup.css");

  it("keeps a radio its own size instead of the full-width text-box rule", () => {
    // Without this the global `input` rule made each radio a 100%-wide, control-height box:
    // a large circle centred above its label.
    const radio = rule(css, '.choice-group input[type="radio"]');
    expect(radio).toMatch(/width:\s*auto;/);
    expect(radio).toMatch(/min-height:\s*auto;/);
    expect(radio).toMatch(/flex:\s*none;/);
    expect(rule(css, ".choice-group label")).toMatch(/display:\s*flex;/);
  });

  it.each(["src/pages/Statements.tsx", "src/pages/RiskProfileSection.tsx"])(
    "is imported by %s",
    (page) => {
      expect(source(page)).toContain('import "./ChoiceGroup.css";');
    },
  );
});

describe("figure cards (.loan-card) at phone width", () => {
  const css = source("src/pages/LoanCard.css");

  it("gives the value its own column on narrow screens so amounts do not break mid-number", () => {
    const narrow = css.slice(css.indexOf("@media (max-width: 40rem)"));
    expect(narrow.length).toBeLessThan(css.length);
    expect(rule(narrow, ".loan-card .details")).toMatch(
      /grid-template-columns:\s*minmax\(45%, 1fr\) auto;/,
    );
    // The shared .details rule lets a value break anywhere ("INR 6,34,76" / "8.10").
    expect(rule(narrow, ".loan-card .details dd")).toMatch(/overflow-wrap:\s*break-word;/);
  });

  it.each(["src/pages/Loans.tsx", "src/pages/Wealth.tsx"])("is imported by %s", (page) => {
    expect(source(page)).toContain('import "./LoanCard.css";');
  });
});

describe("check-and-confirm dialogs at phone width", () => {
  const css = source("src/ui/Dialog.css");

  it("gives the value its own column so amounts and account numbers do not break", () => {
    const narrow = css.slice(css.indexOf("@media (max-width: 40rem)"));
    expect(narrow.length).toBeLessThan(css.length);
    expect(rule(narrow, ".dialog .details")).toMatch(
      /grid-template-columns:\s*minmax\(45%, 1fr\) auto;/,
    );
    expect(rule(narrow, ".dialog .details dd")).toMatch(/overflow-wrap:\s*break-word;/);
  });

  it("is imported by the Dialog every confirm dialog is built on", () => {
    expect(source("src/ui/Dialog.tsx")).toContain('import "./Dialog.css";');
    expect(source("src/ui/ConfirmDialog.tsx")).toContain('from "./Dialog"');
  });
});
