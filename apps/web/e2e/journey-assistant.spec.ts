import { expect, test } from "@playwright/test";
import { fundCustomer, inr, registerCustomer, signIn } from "./support";

// TASK-077 journey 5: the assistant drawer opens, answers a simple balance question and closes.
// The local LLM (Ollama) may be off, as in CI. The assertions follow the documented behaviour
// (modules/assistant/service.py): a clear one-intent question never reaches the model; the
// deterministic router picks the read-only account tool and the SERVER writes the reply from the
// tool's result. So the answer is the same with or without a model, and the test checks that
// (engine "rules") and the customer's real balance, never model wording.
test.describe.configure({ timeout: 90_000 });

const BALANCE = 1_234_567; // INR 12,345.67

test("the assistant answers a balance question from the customer's own account", async ({
  page,
  request,
}) => {
  const customer = await registerCustomer(request, "assistant");
  fundCustomer(customer.email, BALANCE);
  await signIn(page, customer.email, customer.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const masked =
    (await page.locator("li.account").first().locator(".muted").first().textContent()) ?? "";
  const lastFour = masked.replace(/\D/g, "").slice(-4);
  expect(lastFour).toMatch(/^\d{4}$/);

  await page.getByRole("button", { name: "Ask the PBIP assistant" }).click();
  const drawer = page.getByRole("dialog", { name: "PBIP assistant" });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("can't move money");

  await drawer.getByRole("textbox", { name: "Message" }).fill("What is my balance?");
  await drawer.getByRole("button", { name: "Send" }).click();

  const reply = drawer.locator(".assistant-msg--assistant").last();
  await expect(reply).toContainText("Your accounts:");
  await expect(reply).toContainText(`${lastFour}: ${inr(BALANCE)} available`);
  // The stored conversation says which approved tool answered. Read through the API with the
  // customer's own token: a streamed (SSE) body cannot be read back from the browser.
  const login = await request.post("/api/v1/auth/login", {
    data: { email: customer.email, password: customer.password },
  });
  expect(login.ok()).toBe(true);
  const token = ((await login.json()) as { access_token: string }).access_token;
  const history = await request.get("/api/v1/assistant/messages", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(history.ok()).toBe(true);
  const body = (await history.json()) as { items?: { role: string; tool: string | null }[] };
  const assistantTurns = (body.items ?? []).filter((m) => m.role === "assistant");
  expect(assistantTurns.at(-1)?.tool).toBe("get_account_summary");

  await drawer.getByRole("button", { name: "Close assistant" }).click();
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", { name: "Ask the PBIP assistant" })).toBeFocused();
});
