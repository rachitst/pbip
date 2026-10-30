import type { StatusView } from "../lib/status";

export function StatusBadge({ status }: { status: StatusView }) {
  return <span className={`badge badge--${status.tone}`}>{status.label}</span>;
}
