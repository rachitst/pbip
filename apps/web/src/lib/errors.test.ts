import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { errorMessage } from "./errors";

const FALLBACK = "Could not do that.";

describe("errorMessage", () => {
  it("shows the server-written title of a domain error", () => {
    const error = new ApiError(422, "insufficient_funds", "insufficient funds");
    expect(errorMessage(error, FALLBACK)).toBe("insufficient funds");
  });

  it("replaces framework validation detail with a plain sentence", () => {
    const error = new ApiError(422, "validation_error", "Request validation failed");
    expect(errorMessage(error, FALLBACK)).toBe(
      "Some details are not valid. Check them and try again.",
    );
  });

  it("uses fixed wording for rate limits and lockouts", () => {
    expect(errorMessage(new ApiError(429, "rate_limited", "slow down"), FALLBACK)).toMatch(
      /Too many requests/,
    );
    expect(errorMessage(new ApiError(423, "account_locked", "locked"), FALLBACK)).toMatch(
      /temporarily locked/,
    );
  });

  it("never shows a server error title, but gives the request id for support", () => {
    const error = new ApiError(500, "internal_error", "Internal server error", "req-123");
    expect(errorMessage(error, FALLBACK)).toBe(
      `${FALLBACK} If this keeps happening, contact support with reference req-123.`,
    );
    expect(errorMessage(new ApiError(502, "error", "Bad Gateway"), FALLBACK)).toBe(FALLBACK);
  });

  it("hides browser network errors and unknown values", () => {
    expect(errorMessage(new TypeError("Failed to fetch"), FALLBACK)).toBe(
      "We could not reach PBIP. Check your connection and try again.",
    );
    expect(errorMessage(new Error("boom"), FALLBACK)).toBe(FALLBACK);
    expect(errorMessage("x", FALLBACK)).toBe(FALLBACK);
  });

  it("falls back when a domain error has no title", () => {
    expect(errorMessage(new ApiError(409, "conflict", ""), FALLBACK)).toBe(FALLBACK);
  });
});
