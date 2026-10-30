import type { ReactNode } from "react";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";

export interface ReviewItem {
  label: string;
  value: ReactNode;
}

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** What will happen, shown as label/value pairs (from, to, amount, reference...). */
  items: ReviewItem[];
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  busy?: boolean;
  /** Already user-safe text (see lib/errors `errorMessage`). */
  error?: string | null;
  tone?: "primary" | "danger";
}

/** The review step every money-moving or destructive action goes through before it is sent. */
export function ConfirmDialog({
  open,
  title,
  items,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
  error = null,
  tone = "primary",
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      title={title}
      onClose={() => {
        if (!busy) onCancel(); // never abandon a request that is already in flight
      }}
    >
      <dl className="details">
        {items.map((item) => (
          <div key={item.label} className="details__row">
            <dt>{item.label}</dt>
            <dd>{item.value}</dd>
          </div>
        ))}
      </dl>
      {error && <Alert tone="error">{error}</Alert>}
      <div className="dialog__actions">
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button variant={tone} onClick={onConfirm} loading={busy} autoFocus>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
