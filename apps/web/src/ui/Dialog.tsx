import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";

interface DialogProps {
  open: boolean;
  title: string;
  /** Called for Esc and for the caller's own cancel controls; the parent owns `open`. */
  onClose: () => void;
  children: ReactNode;
}

/**
 * Modal dialog on the native <dialog> element: showModal() gives the focus trap, inert
 * background, Esc handling and top-layer stacking from the browser instead of hand-written code,
 * and the browser returns focus to the opener on close.
 */
export function Dialog({ open, title, onClose, children }: DialogProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault(); // keep React state the single source of truth for `open`
        onClose();
      }}
    >
      <h2 id={titleId} className="dialog__title">
        {title}
      </h2>
      {children}
    </dialog>
  );
}
