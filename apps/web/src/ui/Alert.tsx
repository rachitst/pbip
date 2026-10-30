import type { ReactNode } from "react";

type AlertTone = "error" | "success" | "warning" | "info";

/** Errors interrupt screen readers (role=alert); everything else is announced politely. */
export function Alert({ tone, children }: { tone: AlertTone; children: ReactNode }) {
  return (
    <p role={tone === "error" ? "alert" : "status"} className={`alert alert--${tone}`}>
      {children}
    </p>
  );
}
