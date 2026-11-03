import { expect, test } from "@playwright/test";
import { registerCustomer, signIn } from "./support";

// TASK-098: a customer compares two products side by side, on a phone-width screen too.
// Layout guard: the global `input { width: 100% }` rule once made each product checkbox fill its
// row, squeezing the product name to one character per line (found by screenshots on 2026-11-03).
// jsdom has no layout, so the check lives here, against the real stylesheet.
for (const width of [1280, 360]) {
  test(`a customer compares two products at ${String(width)}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 800 });
    const customer = await registerCustomer(request, `compare${String(width)}`);
    await signIn(page, customer.email, customer.password);
    await expect(page.getByRole("heading", { name: "My accounts", exact: true })).toBeVisible();
    await page.goto("/wealth");
    await page.getByRole("link", { name: "Compare products side by side", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Compare products", exact: true }),
    ).toBeVisible();

    const first = page.getByRole("checkbox", { name: "Fixed deposit, 1 to 3 years", exact: true });
    const second = page.getByRole("checkbox", {
      name: "Term life cover (illustrative)",
      exact: true,
    });
    const box = await first.boundingBox();
    expect(box?.width ?? Infinity).toBeLessThan(40);
    const label = await page
      .locator(".compare-picker__option")
      .filter({ has: first })
      .locator("span")
      .boundingBox();
    expect(label?.width ?? 0).toBeGreaterThan(120);
    expect(label?.height ?? Infinity).toBeLessThan(80);

    await first.check();
    await second.check();
    const table = page.getByRole("table", { name: "Product comparison" });
    await expect(table).toBeVisible();
    await expect(table).toContainText("Fixed deposit, 1 to 3 years");
    await expect(table).toContainText("Term life cover (illustrative)");
  });
}
