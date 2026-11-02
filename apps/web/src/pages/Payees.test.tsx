import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PayeeOut } from "../api/client";
import { json, noContent, urlOf } from "../test-utils";
import { Payees } from "./Payees";

const verified: PayeeOut = {
  id: "p-1",
  name: "Ravi Kumar",
  account_number_masked: "XXXXXXXX1234",
  bank_code: "PBIP0001",
  nickname: "Landlord",
  status: "VERIFIED",
  created_at: "2026-10-01T00:00:00Z",
  verified_at: "2026-10-01T00:00:00Z",
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Payees />
    </QueryClientProvider>,
  );
}

type Handler = (url: string, init: RequestInit | undefined) => Response;

function mockApi(handler: Handler) {
  return vi
    .spyOn(globalThis, "fetch")
    .mockImplementation((input, init) => Promise.resolve(handler(urlOf(input), init)));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Payees (TASK-020, TASK-094)", () => {
  it("lists payees with plain status labels, and shows empty and error states", async () => {
    mockApi(() =>
      json(200, [verified, { ...verified, id: "p-2", status: "PENDING_VERIFICATION" }]),
    );
    const { unmount } = renderPage();
    expect(await screen.findAllByText("Ravi Kumar")).toHaveLength(2);
    expect(screen.getByText("Verified")).toBeInTheDocument();
    expect(screen.getByText("Not verified")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Verify" })).toHaveLength(1);
    unmount();
    vi.restoreAllMocks();
    mockApi(() => json(200, []));
    const { unmount: u2 } = renderPage();
    expect(await screen.findByText("You have no payees yet.")).toBeInTheDocument();
    u2();
    vi.restoreAllMocks();
    mockApi(() => json(500, { title: "boom" }));
    renderPage();
    expect(await screen.findByText(/Could not load your payees/)).toBeInTheDocument();
  });

  it("validates the account number locally before calling the API", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi(() => json(200, []));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add payee" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Payee name/), "Ravi Kumar");
    await user.type(within(dialog).getByLabelText("Account number"), "12#4");
    await user.click(within(dialog).getByRole("button", { name: "Add and check name" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("6 to 20 letters");
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the list
  });

  it("adds a payee, runs the name check and warns on a mismatch without revealing the name", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((url, init) => {
      if (url === "/api/v1/payees" && init?.method === "POST") {
        return json(201, { ...verified, status: "PENDING_VERIFICATION", verified_at: null });
      }
      if (url.endsWith("/verify")) {
        return json(200, {
          payee: { ...verified, status: "PENDING_VERIFICATION" },
          name_match: "NO_MATCH",
        });
      }
      return json(200, []);
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add payee" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Payee name/), "Ravi K");
    await user.type(within(dialog).getByLabelText("Account number"), "100000001234");
    await user.click(within(dialog).getByRole("button", { name: "Add and check name" }));
    expect(await screen.findByText(/does not match the name on this account/)).toBeInTheDocument();
    const add = fetchMock.mock.calls.find(
      ([input, init]) => urlOf(input) === "/api/v1/payees" && init?.method === "POST",
    );
    expect(JSON.parse(add?.[1]?.body as string)).toEqual({
      name: "Ravi K",
      account_number: "100000001234",
      bank_code: "PBIP0001",
      nickname: null,
    });
  });

  it("retries only the name check when the payee was already created", async () => {
    const user = userEvent.setup();
    let verifyCalls = 0;
    const fetchMock = mockApi((url, init) => {
      if (url === "/api/v1/payees" && init?.method === "POST") return json(201, verified);
      if (url.endsWith("/verify")) {
        verifyCalls += 1;
        return verifyCalls === 1
          ? json(503, { title: "unavailable" })
          : json(200, { payee: verified, name_match: "MATCH" });
      }
      return json(200, []);
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add payee" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Payee name/), "Ravi Kumar");
    await user.type(within(dialog).getByLabelText("Account number"), "100000001234");
    await user.click(within(dialog).getByRole("button", { name: "Add and check name" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("was added");
    await user.click(within(dialog).getByRole("button", { name: "Add and check name" }));
    expect(await screen.findByText(/The name matches the account/)).toBeInTheDocument();
    const posts = fetchMock.mock.calls.filter(
      ([input, init]) => urlOf(input) === "/api/v1/payees" && init?.method === "POST",
    );
    expect(posts).toHaveLength(1);
  });

  it("edits the nickname, warns that a new name needs re-verification, and sends only changes", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((_url, init) =>
      init?.method === "PATCH"
        ? json(200, { ...verified, name: "Ravi K Kumar", status: "PENDING_VERIFICATION" })
        : json(200, [verified]),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/cannot be changed/)).toBeInTheDocument();
    const nameInput = within(dialog).getByLabelText("Payee name");
    await user.clear(nameInput);
    await user.type(nameInput, "Ravi K Kumar");
    expect(within(dialog).getByText(/must be verified again/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Verify the payee before sending money/)).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(urlOf(patch?.[0] ?? "")).toBe("/api/v1/payees/p-1");
    expect(JSON.parse(patch?.[1]?.body as string)).toEqual({
      name: "Ravi K Kumar",
      nickname: "Landlord",
    });
  });

  it("removes a payee after confirmation", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((_url, init) =>
      init?.method === "DELETE" ? noContent() : json(200, [verified]),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Remove payee" }));
    expect(await screen.findByText("Payee removed.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(true);
  });
});
