import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  LockoutStatusOut,
  RuntimeSettingOut,
  RuntimeSettingsOut,
  UnlockResultOut,
  UserOut,
} from "../../api/client";
import { MeContext } from "../../shell/MeContext";
import { json, urlOf } from "../../test-utils";
import { Administration, canAdminister } from "./Administration";
import { formatSettingValue, parseSettingValue, valueError } from "./AdminSettings";

const admin: UserOut = {
  id: "u-admin",
  email: "admin@bank.example",
  roles: ["ADMIN"],
  mfa_enabled: true,
  mfa_verified_session: true,
  created_at: "2026-01-01T00:00:00Z",
};

const USER_ID = "7d3c0e3a-1111-4222-8333-444455556666";
const INITIAL_PASSWORD = "Correct-Horse-42!"; // pragma: allowlist secret

const daily: RuntimeSettingOut = {
  key: "daily_transfer_limit_minor",
  label: "Daily transfer limit",
  description: "Most a customer may send per UTC day, transfers and bill payments together.",
  unit: "MINOR_UNITS",
  min_value: 10_000,
  max_value: 1_000_000_000,
  default_value: 50_000_000,
  effective_value: 50_000_000,
  overridden: false,
  updated_at: null,
  updated_by_user_id: null,
};

const schedules: RuntimeSettingOut = {
  ...daily,
  key: "max_active_scheduled_transfers_per_customer",
  label: "Scheduled transfers per customer",
  description: "Active or paused standing orders one customer may hold.",
  unit: "COUNT",
  min_value: 1,
  max_value: 200,
  default_value: 20,
  effective_value: 5,
  overridden: true,
  updated_at: new Date(Date.now() - 3_600_000).toISOString(),
  updated_by_user_id: "u-admin",
};

const settings: RuntimeSettingsOut = { items: [daily, schedules] };

const locked: LockoutStatusOut = {
  user_id: USER_ID,
  failed_login_count: 5,
  locked_until: new Date(Date.now() + 60_000).toISOString(),
  is_locked: true,
  last_failure_at: new Date().toISOString(),
};

const unlocked: UnlockResultOut = {
  user_id: USER_ID,
  failed_login_count: 0,
  locked_until: null,
  is_locked: false,
  last_failure_at: locked.last_failure_at,
  previous_failed_login_count: 5,
  was_locked: true,
  rate_limit_counters_cleared: 1,
};

type Handler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

const fetchMock = vi.fn<typeof fetch>();
let handler: Handler;

function routes(overrides: Partial<Record<string, Handler>> = {}): Handler {
  return (url, init) => {
    const method = init?.method ?? "GET";
    const key = `${method} ${url.replace(/^https?:\/\/[^/]+/, "")}`;
    const custom = overrides[key];
    if (custom) return custom(url, init);
    if (key === "GET /api/v1/admin/settings") return json(200, settings);
    return json(404, { code: "not_found", title: "Not found" });
  };
}

function bodyOf(method: string, suffix: string): unknown {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => urlOf(input).endsWith(suffix) && (init?.method ?? "GET") === method,
  );
  const body = call?.[1]?.body;
  if (typeof body !== "string") throw new Error(`no ${method} ${suffix} with a JSON body`);
  return JSON.parse(body);
}

function calls(suffix: string, method = "GET"): number {
  return fetchMock.mock.calls.filter(
    ([input, init]) => urlOf(input).endsWith(suffix) && (init?.method ?? "GET") === method,
  ).length;
}

function renderWith(ui: ReactElement, me: UserOut = admin) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MeContext.Provider value={me}>
        <MemoryRouter>{ui}</MemoryRouter>
      </MeContext.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  handler = routes();
  fetchMock.mockReset();
  fetchMock.mockImplementation((input, init) => Promise.resolve(handler(urlOf(input), init)));
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("canAdminister", () => {
  it("admits administrators only", () => {
    expect(canAdminister(admin)).toBe(true);
    for (const role of [
      "COMPLIANCE_OFFICER",
      "FRAUD_ANALYST",
      "RELATIONSHIP_MANAGER",
      "CUSTOMER",
    ]) {
      expect(canAdminister({ ...admin, roles: [role] })).toBe(false);
    }
  });
});

describe("setting values", () => {
  it("formats and parses money as integer minor units and counts as whole numbers", () => {
    expect(formatSettingValue(daily, 50_000_000)).toBe("INR 5,00,000.00");
    expect(formatSettingValue(schedules, 7)).toBe("7");
    expect(parseSettingValue(daily, "1,500.50")).toBe(150_050);
    expect(parseSettingValue(daily, "1.234")).toBeNull();
    expect(parseSettingValue(schedules, "12")).toBe(12);
    expect(parseSettingValue(schedules, "1.5")).toBeNull();
  });

  it("checks the allow-listed bounds and refuses the value already in force", () => {
    expect(valueError(daily, "99.99")).toMatch(/Allowed range: INR 100.00 to INR 1,00,00,000.00/);
    expect(valueError(daily, "10000000.01")).toMatch(/Allowed range/);
    expect(valueError(daily, "500000")).toBe("This is already the value in force.");
    expect(valueError(daily, "abc")).toMatch(/at most two decimals/);
    expect(valueError(schedules, "201")).toMatch(/Allowed range: 1 to 200/);
    expect(valueError(schedules, "10")).toBeNull();
  });
});

describe("Administration page", () => {
  it("shows a restricted page and calls no admin endpoint for other staff roles", () => {
    renderWith(<Administration />, { ...admin, roles: ["COMPLIANCE_OFFICER"] });
    expect(screen.getByRole("alert")).toHaveTextContent("needs the Administrator role");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("warns an admin whose session was not verified with an authenticator", async () => {
    renderWith(<Administration />, { ...admin, mfa_verified_session: false });
    expect(screen.getByText(/need a session verified with an authenticator/)).toBeVisible();
    await screen.findByRole("table");
  });

  it("lists the settings with value in force, default, range and change status", async () => {
    renderWith(<Administration />);
    expect(screen.getByText("Loading settings...")).toBeInTheDocument();
    const table = await screen.findByRole("table");
    const dailyRow = within(table).getByRole("row", { name: /Daily transfer limit/ });
    expect(dailyRow).toHaveTextContent("INR 5,00,000.00");
    expect(dailyRow).toHaveTextContent("Default");
    expect(dailyRow).toHaveTextContent("INR 100.00 to INR 1,00,00,000.00");
    expect(dailyRow).toHaveTextContent("Never");
    const countRow = within(table).getByRole("row", { name: /Scheduled transfers per customer/ });
    expect(countRow).toHaveTextContent("Changed");
    expect(countRow).toHaveTextContent("1 to 200");
    expect(
      screen.getByText(/Secrets, sign-in and security settings are never configurable/),
    ).toBeVisible();
  });

  it("shows an empty state when nothing is configurable", async () => {
    handler = routes({ "GET /api/v1/admin/settings": () => json(200, { items: [] }) });
    renderWith(<Administration />);
    expect(await screen.findByText("No settings can be changed at runtime.")).toBeVisible();
  });

  it("shows an error with retry when the settings cannot be loaded", async () => {
    handler = routes({
      "GET /api/v1/admin/settings": () =>
        json(403, { code: "mfa_required", title: "Multi-factor authentication required" }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Multi-factor authentication required");
    handler = routes();
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });

  it("changes a limit with a reason, sending integer minor units, and confirms it", async () => {
    handler = routes({
      "PUT /api/v1/admin/settings/daily_transfer_limit_minor": () =>
        json(200, { ...daily, effective_value: 15_000_000, overridden: true }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    await user.click(await screen.findByRole("button", { name: "Change daily transfer limit" }));
    const dialog = screen.getByRole("dialog");
    const value = within(dialog).getByLabelText("New value");
    await user.clear(value);
    await user.type(value, "50");
    await user.click(within(dialog).getByRole("button", { name: "Save change" }));
    expect(
      within(dialog).getByText(/Allowed range: INR 100.00/, { selector: ".field__error" }),
    ).toBeVisible();
    expect(within(dialog).getByText(/at least 10 characters/)).toBeVisible();
    expect(calls("/admin/settings/daily_transfer_limit_minor", "PUT")).toBe(0);

    await user.clear(value);
    await user.type(value, "1,50,000");
    await user.type(within(dialog).getByLabelText("Reason"), "Fraud spike this week");
    await user.click(within(dialog).getByRole("button", { name: "Save change" }));
    expect(await screen.findByText(/Daily transfer limit is now INR 1,50,000.00/)).toBeVisible();
    expect(bodyOf("PUT", "/admin/settings/daily_transfer_limit_minor")).toEqual({
      value: 15_000_000,
      reason: "Fraud spike this week",
    });
    await waitFor(() => {
      expect(calls("/admin/settings")).toBe(2); // the list is re-read after the change
    });
  });

  it("keeps the dialog open and shows the server's refusal", async () => {
    handler = routes({
      "PUT /api/v1/admin/settings/max_active_scheduled_transfers_per_customer": () =>
        json(422, {
          code: "unprocessable",
          title: "value is outside the allowed range for this setting",
        }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    await user.click(
      await screen.findByRole("button", { name: "Change scheduled transfers per customer" }),
    );
    const dialog = screen.getByRole("dialog");
    const value = within(dialog).getByLabelText("New value");
    await user.clear(value);
    await user.type(value, "10");
    await user.type(within(dialog).getByLabelText("Reason"), "More standing orders for SMEs");
    await user.click(within(dialog).getByRole("button", { name: "Save change" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "value is outside the allowed range",
    );
    expect(bodyOf("PUT", "/admin/settings/max_active_scheduled_transfers_per_customer")).toEqual({
      value: 10,
      reason: "More standing orders for SMEs",
    });
  });
});

describe("staff users and lockouts", () => {
  it("creates a staff user only with explicitly chosen roles", async () => {
    handler = routes({
      "POST /api/v1/admin/users": () =>
        json(201, {
          id: USER_ID,
          email: "new.analyst@bank.example",
          roles: ["FRAUD_ANALYST", "COMPLIANCE_OFFICER"],
          mfa_enabled: false,
          mfa_verified_session: false,
          created_at: "2026-11-03T00:00:00Z",
        }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    const form = screen.getByRole("region", { name: "Create a staff user" });
    const submit = within(form).getByRole("button", { name: "Create user" });
    await user.type(within(form).getByLabelText("Work e-mail"), "new.analyst@bank.example");
    await user.type(within(form).getByLabelText("Full name"), "New Analyst");
    await user.type(within(form).getByLabelText("Initial password"), INITIAL_PASSWORD);
    expect(submit).toBeDisabled(); // no role is preselected
    for (const role of ["Fraud analyst", "Compliance officer"]) {
      await user.click(within(form).getByRole("checkbox", { name: role }));
    }
    expect(within(form).getByRole("checkbox", { name: "Administrator" })).not.toBeChecked();
    await user.click(submit);
    expect(await within(form).findByText(/Created new.analyst@bank.example/)).toBeVisible();
    expect(within(form).getByText(USER_ID)).toBeVisible();
    expect(bodyOf("POST", "/admin/users")).toEqual({
      email: "new.analyst@bank.example",
      full_name: "New Analyst",
      password: INITIAL_PASSWORD,
      roles: ["FRAUD_ANALYST", "COMPLIANCE_OFFICER"],
    });
    expect(within(form).getByLabelText("Initial password")).toHaveValue("");
  });

  it("shows why a user could not be created", async () => {
    handler = routes({
      "POST /api/v1/admin/users": () =>
        json(409, { code: "conflict", title: "email already registered" }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    const form = screen.getByRole("region", { name: "Create a staff user" });
    await user.type(within(form).getByLabelText("Work e-mail"), "taken@bank.example");
    await user.type(within(form).getByLabelText("Full name"), "Taken Name");
    await user.type(within(form).getByLabelText("Initial password"), INITIAL_PASSWORD);
    await user.click(within(form).getByRole("checkbox", { name: "Administrator" }));
    await user.click(within(form).getByRole("button", { name: "Create user" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("email already registered");
  });

  it("validates the user id before asking the server", async () => {
    const user = userEvent.setup();
    renderWith(<Administration />);
    const panel = screen.getByRole("region", { name: "Sign-in lockout" });
    expect(within(panel).getByText(/Enter a user id/)).toBeVisible();
    await user.type(within(panel).getByLabelText("User id"), "42; DROP TABLE users");
    await user.click(within(panel).getByRole("button", { name: "Inspect" }));
    expect(within(panel).getByText(/Enter the user id in the form/)).toBeVisible();
    expect(fetchMock.mock.calls.some(([input]) => urlOf(input).includes("/lockout"))).toBe(false);
  });

  it("inspects a lockout and clears it after confirmation", async () => {
    handler = routes({
      [`GET /api/v1/admin/users/${USER_ID}/lockout`]: () => json(200, locked),
      [`POST /api/v1/admin/users/${USER_ID}/unlock`]: () => json(200, unlocked),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    const panel = screen.getByRole("region", { name: "Sign-in lockout" });
    await user.type(within(panel).getByLabelText("User id"), USER_ID);
    await user.click(within(panel).getByRole("button", { name: "Inspect" }));
    expect(await within(panel).findByText("Locked")).toBeVisible();
    const failed = within(panel).getAllByText("Failed sign-ins")[0]?.nextElementSibling;
    expect(failed).toHaveTextContent("5");

    await user.click(within(panel).getByRole("button", { name: "Clear lockout" }));
    const dialog = screen.getByRole("dialog");
    expect(calls(`/users/${USER_ID}/unlock`, "POST")).toBe(0); // nothing before confirming
    await user.click(within(dialog).getByRole("button", { name: "Clear lockout" }));
    expect(
      await within(panel).findByText(/The lockout was cleared; 5 failed sign-ins reset/),
    ).toBeVisible();
    expect(within(panel).getByText("Not locked")).toBeVisible();
    expect(calls(`/users/${USER_ID}/unlock`, "POST")).toBe(1);
  });

  it("shows a not-found user as an error with retry", async () => {
    handler = routes({
      [`GET /api/v1/admin/users/${USER_ID}/lockout`]: () =>
        json(404, { code: "not_found", title: "user not found" }),
    });
    const user = userEvent.setup();
    renderWith(<Administration />);
    const panel = screen.getByRole("region", { name: "Sign-in lockout" });
    await user.type(within(panel).getByLabelText("User id"), USER_ID);
    await user.click(within(panel).getByRole("button", { name: "Inspect" }));
    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent("user not found");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeVisible();
  });
});
