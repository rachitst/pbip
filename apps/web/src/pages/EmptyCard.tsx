import type { LucideIcon } from "lucide-react";
import { EmptyState } from "../ui/States";
import "./CustomerPage.css";

/** An empty list on a customer page: the shared empty state, drawn as a quiet card with an icon. */
export function EmptyCard({ icon: Glyph, message }: { icon: LucideIcon; message: string }) {
  return (
    <div className="empty-card">
      <span className="empty-card__icon">
        <Glyph size={24} aria-hidden="true" focusable="false" />
      </span>
      <EmptyState message={message} />
    </div>
  );
}
