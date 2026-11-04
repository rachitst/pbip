import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomerProfileOut } from "../api/client";
import { json, noContent, urlOf } from "../test-utils";
import { PhoneSection, normalisePhone } from "./PhoneSection";

const base: CustomerProfileOut = {
  id: "c1",
  user_id: "u1",
  email: "alice@example.com",
  full_name: "Alice",
  date_of_birth: null,
  phone_masked: null,
  kyc_status: "VERIFIED",
  created_at: "2026-01-01T00:00:00Z",
};

function renderSection() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <PhoneSection />
    </QueryClientProvider>,
  );
}

describe("mobile number (#398)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  let current: CustomerProfileOut = base;
  let failGet = false;
  let limited = false;

  beforeEach(() => {
    current = base;
    failGet = false;
    limited = false;
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation((input, init) => {
      const url = urlOf(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/customers/me")) {
        if (failGet) return Promise.resolve(json(500, { code: "error", title: "boom" }));
        return Promise.resolve(json(200, current));
      }
      if (url.endsWith("/customers/me/phone") && method === "PUT") {
        if (limited) {
          return Promise.resolve(json(429, { code: "rate_limited", title: "too many requests" }));
        }
        const { phone } = JSON.parse(init?.body as string) as { phone: string };
        current = { ...current, phone_masked: `${"•".repeat(phone.length - 4)}${phone.slice(-4)}` };
        return Promise.resolve(json(200, current));
      }
      if (url.endsWith("/customers/me/phone") && method === "DELETE") {
        current = { ...current, phone_masked: null };
        return Promise.resolve(noContent());
      }
      return Promise.resolve(json(404, { code: "not_found", title: "not found" }));
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const sent = (method: string) =>
    fetchMock.mock.calls.filter(
      ([input, init]) => urlOf(input).endsWith("/customers/me/phone") && init?.method === method,
    );

  it("drops the separators people type and keeps anything else for the check", () => {
    expect(normalisePhone(" +91 (98123) 456-78 ")).toBe("+919812345678");
    expect(normalisePhone("+91x98")).toBe("+91x98");
  });

  it("shows loading, then an error state with a retry", async () => {
    failGet = true;
    const user = userEvent.setup();
    renderSection();
    expect(screen.getByRole("status")).toHaveTextContent("Loading your mobile number");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load your mobile number");
    failGet = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Number")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete mobile number" })).toBeNull();
  });

  it("sends the number in E.164 form and never keeps it on screen after saving", async () => {
    const user = userEvent.setup();
    renderSection();
    const input = await screen.findByLabelText("Number");
    await user.type(input, "+91 99000 11223");
    await user.click(screen.getByRole("button", { name: "Save mobile number" }));
    expect(await screen.findByText("Mobile number saved.")).toBeInTheDocument();
    const body = sent("PUT")[0]?.[1]?.body as string;
    expect(JSON.parse(body)).toEqual({ phone: "+919900011223" });
    expect(screen.getByLabelText("New number")).toHaveValue("");
    expect(screen.queryByText(/99000/)).toBeNull();
  });

  it("refuses a number without a country code before calling the API", async () => {
    const user = userEvent.setup();
    renderSection();
    await user.type(await screen.findByLabelText("Number"), "9812345678");
    await user.click(screen.getByRole("button", { name: "Save mobile number" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("country code");
    expect(sent("PUT")).toHaveLength(0);
  });

  it("explains a rate limit", async () => {
    limited = true;
    const user = userEvent.setup();
    renderSection();
    await user.type(await screen.findByLabelText("Number"), "+447700900123");
    await user.click(screen.getByRole("button", { name: "Save mobile number" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many requests");
  });

  it("deletes the number only after confirmation", async () => {
    current = { ...base, phone_masked: "•••••••••5678" };
    const user = userEvent.setup();
    renderSection();
    await user.click(await screen.findByRole("button", { name: "Delete mobile number" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/no longer hold a mobile number/)).toBeInTheDocument();
    expect(sent("DELETE")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await screen.findByText("Mobile number deleted.")).toBeInTheDocument();
    expect(sent("DELETE")).toHaveLength(1);
    expect(await screen.findByLabelText("Number")).toBeInTheDocument();
  });

  it("styles the field from its own stylesheet", () => {
    expect(readFileSync("src/pages/PhoneSection.tsx", "utf8")).toContain(
      'import "./PhoneSection.css";',
    );
    expect(readFileSync("src/pages/PhoneSection.css", "utf8")).toContain(".phone-form__input {");
  });
});
