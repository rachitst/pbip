/** Mirrors the server policy (core/security.py); the server remains the authority. */
export const MIN_PASSWORD_LENGTH = 12;

/** Checks a new password before it is sent, so the obvious mistakes are caught locally. */
export function newPasswordProblem(password: string, confirmation: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `The password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
  }
  if (password !== confirmation) return "The passwords do not match.";
  return null;
}
