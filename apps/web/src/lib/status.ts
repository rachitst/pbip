import type { TransferOut } from "../api/client";

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

export interface StatusView {
  label: string;
  tone: Tone;
}

/** Every value of the TransferOut.status enum in the OpenAPI contract; the compiler fails if the
 * contract gains a status this map does not describe. */
const TRANSFER_STATUS: Record<TransferOut["status"], StatusView> = {
  POSTED: { label: "Completed", tone: "success" },
  PENDING_STEP_UP: { label: "Verification needed", tone: "warning" },
  PENDING_REVIEW: { label: "Under review", tone: "warning" },
  DECLINED: { label: "Declined", tone: "danger" },
  REVERSED: { label: "Reversed", tone: "neutral" },
};

export function transferStatus(status: TransferOut["status"]): StatusView {
  return TRANSFER_STATUS[status];
}

/** Ledger entries type `transaction_status` as a plain string, so unknown values are shown as
 * sent rather than guessed. */
export function transactionStatus(status: string): StatusView {
  if (status in TRANSFER_STATUS) return TRANSFER_STATUS[status as TransferOut["status"]];
  return { label: status, tone: "neutral" };
}
