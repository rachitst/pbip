import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger" | "link";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: "md" | "sm";
  /** Disables the button and marks it busy; the caller keeps control of the visible label. */
  loading?: boolean;
}

/** The only way to render a styled button: global `button` styles are a neutral reset. Defaults to
 * type="button" so a button inside a form never submits it by accident. */
export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  className,
  type = "button",
  disabled,
  ...rest
}: ButtonProps) {
  const classes = ["btn", `btn--${variant}`, size === "sm" ? "btn--sm" : "", className ?? ""];
  return (
    <button
      {...rest}
      type={type}
      className={classes.filter(Boolean).join(" ")}
      disabled={disabled === true || loading}
      aria-busy={loading || undefined}
    />
  );
}
