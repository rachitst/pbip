import { describe, expect, it } from "vitest";
import { transactionStatus, transferStatus } from "./status";

describe("status views", () => {
  it("describes every transfer status in the contract", () => {
    expect(transferStatus("POSTED")).toEqual({ label: "Completed", tone: "success" });
    expect(transferStatus("PENDING_STEP_UP").tone).toBe("warning");
    expect(transferStatus("PENDING_REVIEW").label).toBe("Under review");
    expect(transferStatus("DECLINED").tone).toBe("danger");
    expect(transferStatus("REVERSED").tone).toBe("neutral");
  });

  it("shows an unknown ledger status as sent instead of guessing", () => {
    expect(transactionStatus("REVERSED").label).toBe("Reversed");
    expect(transactionStatus("SOMETHING_NEW")).toEqual({ label: "SOMETHING_NEW", tone: "neutral" });
  });
});
