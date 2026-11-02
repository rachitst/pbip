import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { apiToken, fundCustomer, registerCustomer, signIn, uniqueTag } from "./support";

// TASK-077 journey 6 (BOLA): a signed-in customer edits the id in a URL to another customer's
// account, payee or transfer and gets "not found", exactly as for an id that does not exist.
// The intruder uses their own real browser session (the bearer token the app sends), so this is
// the probe an attacker would make from the UI, not a synthetic call.
test.describe.configure({ timeout: 90_000 });

interface Problem {
  status: number;
  code: string;
  title: string;
}

test("another customer's ids in a URL give Not found, like ids that do not exist", async ({
  page,
  request,
}) => {
  // The owner: an account, a payee and a posted transfer, made through the API.
  const owner = await registerCustomer(request, "owner");
  fundCustomer(owner.email, 100_000);
  const auth = { Authorization: `Bearer ${await apiToken(request, owner)}` };
  const [account] = (await (await request.get("/api/v1/accounts", { headers: auth })).json()) as {
    id: string;
  }[];
  expect(account).toBeDefined();
  const payee = (await (
    await request.post("/api/v1/payees", {
      headers: auth,
      data: {
        name: `Owner payee ${uniqueTag()}`,
        account_number: "998877665544",
        bank_code: "EXTB0001",
      },
    })
  ).json()) as { id: string };
  expect(
    (await request.post(`/api/v1/payees/${payee.id}/verify`, { headers: auth })).status(),
  ).toBe(200);
  const transfer = await request.post("/api/v1/transfers", {
    headers: { ...auth, "Idempotency-Key": randomUUID() },
    data: { from_account_id: account?.id, payee_id: payee.id, amount_minor: 5_000 },
  });
  expect(transfer.status()).toBe(201);
  const transferId = ((await transfer.json()) as { id: string }).id;

  // The intruder signs in to the web app; their session's token is taken from the app's own call.
  const intruder = await registerCustomer(request, "intruder");
  const ownCall = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/v1/accounts");
  await signIn(page, intruder.email, intruder.password);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  const session = { Authorization: (await (await ownCall).headerValue("authorization")) ?? "" };
  expect(session.Authorization).toMatch(/^Bearer /);

  const missing = randomUUID();
  const probes = [
    ["/api/v1/accounts/{id}", account?.id ?? ""],
    ["/api/v1/accounts/{id}/transactions", account?.id ?? ""],
    ["/api/v1/payees/{id}", payee.id],
    ["/api/v1/transfers/{id}", transferId],
  ] as const;
  for (const [template, ownerId] of probes) {
    const theirs = await page.request.get(template.replace("{id}", ownerId), { headers: session });
    const nobodys = await page.request.get(template.replace("{id}", missing), { headers: session });
    expect(theirs.status(), template).toBe(404);
    expect(nobodys.status(), template).toBe(404);
    const [a, b] = [(await theirs.json()) as Problem, (await nobodys.json()) as Problem];
    // Same status, code and title: nothing tells "someone else's" apart from "does not exist".
    expect({ code: a.code, title: a.title }, template).toEqual({ code: b.code, title: b.title });
    expect(a.code).toBe("not_found");
    // The owner still sees their own object (the probe failed on ownership, not a bad id).
    expect((await request.get(template.replace("{id}", ownerId), { headers: auth })).status()).toBe(
      200,
    );
  }

  // The same edit in the app's own URL (a notification deep link) opens nothing of the owner's.
  await page.goto(`/accounts?account=${account?.id ?? ""}&transaction=${transferId}`);
  await expect(page.getByRole("heading", { name: "My accounts" })).toBeVisible();
  await expect(page.locator("li.account")).toHaveCount(1); // only the intruder's own account
  await expect(page.getByRole("table", { name: "Transactions, newest first" })).toHaveCount(0);
});
