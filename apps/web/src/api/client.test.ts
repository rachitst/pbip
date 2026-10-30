import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { json } from "../test-utils";
import { ApiError, api, setAccessToken } from "./client";

function problem(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

const transfer = {
  id: "t-1",
  status: "DECLINED",
  message: "Transfer declined",
  amount_minor: 100,
  currency: "INR",
};

describe("apiFetch error handling", () => {
  const fetchMock = vi.fn<typeof fetch>();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    setAccessToken("t");
  });
  afterEach(() => {
    setAccessToken(null);
    vi.unstubAllGlobals();
  });

  it("returns a declined transfer body (plain JSON 403) as a result", async () => {
    fetchMock.mockResolvedValue(json(403, transfer));
    await expect(
      api.transfer({ from_account_id: "a", payee_id: "p", amount_minor: 100 }, "k"),
    ).resolves.toMatchObject({ status: "DECLINED" });
  });

  it("raises a problem+json 403 on the transfers path instead of treating it as a transfer", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        problem(403, { code: "forbidden", title: "customer role required", request_id: "r-9" }),
      ),
    );
    const call = api.transfer({ from_account_id: "a", payee_id: "p", amount_minor: 100 }, "k");
    await expect(call).rejects.toBeInstanceOf(ApiError);
    await expect(
      api.transfer({ from_account_id: "a", payee_id: "p", amount_minor: 100 }, "k"),
    ).rejects.toMatchObject({ status: 403, code: "forbidden", requestId: "r-9" });
  });

  it("keeps the request id of an error for support", async () => {
    fetchMock.mockResolvedValue(
      problem(500, { code: "internal_error", title: "Internal server error", request_id: "r-1" }),
    );
    await expect(api.accounts()).rejects.toMatchObject({ status: 500, requestId: "r-1" });
  });
});
