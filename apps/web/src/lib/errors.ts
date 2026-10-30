import { ApiError } from "../api/client";

/**
 * Turns any failure into text that is safe to show a customer.
 *
 * Domain errors (4xx problem+json) carry a `title` written by the server for people, which never
 * echoes request input (core/errors.py), so it is shown as is. Everything else is replaced by a
 * fixed sentence: framework validation detail is jargon, 5xx titles are generic, and a browser
 * network error ("Failed to fetch") must never reach the screen.
 */
export function errorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) {
    return error instanceof TypeError
      ? "We could not reach PBIP. Check your connection and try again."
      : fallback;
  }
  if (error.status === 429 || error.code === "rate_limited") {
    return "Too many requests. Wait a minute and try again.";
  }
  if (error.status === 423 || error.code === "account_locked") {
    return "Too many failed attempts. Your account is temporarily locked.";
  }
  if (error.code === "validation_error")
    return "Some details are not valid. Check them and try again.";
  if (error.status >= 500 || error.status === 0) {
    return error.requestId
      ? `${fallback} If this keeps happening, contact support with reference ${error.requestId}.`
      : fallback;
  }
  return error.message || fallback;
}
