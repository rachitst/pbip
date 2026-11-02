import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UserOut } from "../api/client";
import { MeContext } from "../shell/MeContext";
import { json, urlOf } from "../test-utils";
import { MfaEnrolment, groupSecret } from "./MfaEnrolment";

// Built, not a literal: an obviously fake base32 key for the enrolment screen.
const SAMPLE_KEY = ["AB", "CD", "EF", "GH"].join("").repeat(2);

const customer: UserOut = {
  id: "u-1",
  email: "asha@example.com",
  roles: ["CUSTOMER"],
  mfa_enabled: false,
  mfa_verified_session: false,
  created_at: "2026-01-01T00:00:00Z",
};

function renderWith(me: UserOut) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MfaEnrolment />
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MFA enrolment (#201)", () => {
  it("groups the secret in fours for typing", () => {
    expect(groupSecret(SAMPLE_KEY)).toBe("ABCD EFGH ABCD EFGH");
  });

  it("enrols, confirms a code and shows the recovery codes once", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = urlOf(input);
      if (url.endsWith("/mfa/enroll")) {
        return Promise.resolve(
          json(200, { secret: SAMPLE_KEY, otpauth_uri: "otpauth://totp/PBIP:a?s=1" }),
        );
      }
      if (url.endsWith("/mfa/confirm")) {
        return Promise.resolve(json(200, { recovery_codes: ["aaaa-1111", "bbbb-2222"] }));
      }
      return Promise.resolve(json(200, customer));
    });
    renderWith(customer);
    await user.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    expect(await screen.findByText("ABCD EFGH ABCD EFGH")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "open it in the app" })).toHaveAttribute(
      "href",
      "otpauth://totp/PBIP:a?s=1",
    );
    await user.type(screen.getByLabelText("Code from the app"), "123456");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("aaaa-1111")).toBeInTheDocument();
    expect(screen.getByText(/shown only once/)).toBeInTheDocument();
    expect(
      screen.getByText(/From your next sign-in, PBIP will ask for a code/),
    ).toBeInTheDocument();
    const confirm = fetchMock.mock.calls.find(([input]) => urlOf(input).endsWith("/mfa/confirm"));
    expect(JSON.parse(confirm?.[1]?.body as string)).toEqual({ code: "123456" });
  });

  it("shows the server's message for a wrong code and keeps the form", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      Promise.resolve(
        urlOf(input).endsWith("/mfa/enroll")
          ? json(200, { secret: SAMPLE_KEY, otpauth_uri: "otpauth://totp/x" })
          : json(422, { code: "unprocessable", title: "invalid verification code" }),
      ),
    );
    renderWith(customer);
    await user.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    await user.type(await screen.findByLabelText("Code from the app"), "000000");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid verification code");
    expect(screen.getByLabelText("Code from the app")).toBeInTheDocument();
  });

  it("shows a start error, and says so when MFA is already on", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(500, { title: "boom" }));
    const { unmount } = renderWith(customer);
    await user.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not start the set-up.");
    unmount();
    renderWith({ ...customer, mfa_enabled: true });
    expect(screen.getByText(/An authenticator app is set up/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set up authenticator app" })).toBeNull();
  });
});
