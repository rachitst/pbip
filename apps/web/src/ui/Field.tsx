import { useId } from "react";
import type { ReactNode } from "react";

export interface ControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

interface FieldProps {
  label: string;
  hint?: string;
  error?: string | null;
  /** Receives the id and ARIA wiring for the control, so label, hint and error stay linked. */
  children: (control: ControlProps) => ReactNode;
}

export function Field({ label, hint, error, children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {hint && (
        <span id={hintId} className="field__hint">
          {hint}
        </span>
      )}
      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
      })}
      {error && (
        <span id={errorId} className="field__error">
          {error}
        </span>
      )}
    </div>
  );
}
