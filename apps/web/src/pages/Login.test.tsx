import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { AuthProvider } from "../auth/AuthContext";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderApp() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>,
  );
}

describe("login flow", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    // First call is the silent session restore on page load: no cookie -> 401.
    fetchMock.mockResolvedValueOnce(json(401, { code: "unauthorized", title: "no session" }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the sign-in form when there is no session", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows a generic error for bad credentials", async () => {
    const user = userEvent.setup();
    renderApp();
    fetchMock.mockResolvedValueOnce(
      json(401, { code: "unauthorized", title: "invalid email or password" }),
    );
    await user.type(await screen.findByLabelText("Email"), "a@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid email or password");
  });

  it("asks for the authenticator code when the server requires MFA", async () => {
    const user = userEvent.setup();
    renderApp();
    fetchMock.mockResolvedValueOnce(
      json(200, { mfa_required: true, mfa_token: "challenge-token" }),
    );
    await user.type(await screen.findByLabelText("Email"), "staff@bank.example");
    await user.type(screen.getByLabelText("Password"), "Correct-Horse-9!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByLabelText("Authenticator or recovery code")).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });
});
