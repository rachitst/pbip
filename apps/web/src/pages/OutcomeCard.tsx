import { CheckCircle2, Clock, Info, XCircle } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import "./CustomerPage.css";

type OutcomeTone = "success" | "warning" | "error" | "info";

const OUTCOME_ICON: Record<OutcomeTone, LucideIcon> = {
  success: CheckCircle2,
  warning: Clock,
  error: XCircle,
  info: Info,
};

/** A calm result card: an icon, a short title and the full message, in the status colour. Errors
 * interrupt screen readers (role=alert); everything else is announced politely. */
export function OutcomeCard({
  tone,
  title,
  children,
}: {
  tone: OutcomeTone;
  title: string;
  children: ReactNode;
}) {
  const Glyph = OUTCOME_ICON[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`outcome outcome--${tone}`}>
      <span className="outcome__icon">
        <Glyph size={20} aria-hidden="true" focusable="false" />
      </span>
      <div>
        <p className="outcome__title">{title}</p>
        <p className="outcome__text">{children}</p>
      </div>
    </div>
  );
}
