import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { AuditLogOut, ChainStatusOut } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDateTime } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Button } from "../../ui/Button";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";

function resolveIp(entry: AuditLogOut): string {
  if (typeof entry.ip === "string" && entry.ip.length > 0) return entry.ip;
  if (typeof entry.details.ip === "string" && entry.details.ip.length > 0) return entry.details.ip;
  if (typeof entry.details.ip_address === "string" && entry.details.ip_address.length > 0) {
    return entry.details.ip_address;
  }
  return "—";
}

function formatDetailsSummary(details: Record<string, unknown>): string {
  const keys = Object.keys(details).filter((k) => k !== "ip" && k !== "ip_address");
  if (keys.length === 0) return "View raw details";
  if (keys.length === 1) {
    const k = keys[0];
    if (k !== undefined) {
      const val =
        typeof details[k] === "string" || typeof details[k] === "number" ? String(details[k]) : "…";
      return `${k}: ${val}`;
    }
  }
  return `View details (${keys.length} fields)`;
}

export function AuditLog() {
  const me = useCurrentUser();
  const hasComplianceRole = me.roles.includes("COMPLIANCE_OFFICER") || me.roles.includes("ADMIN");

  const [verifyResult, setVerifyResult] = useState<ChainStatusOut | null>(null);

  const logsQuery = useQuery({
    queryKey: ["compliance", "audit-logs"],
    queryFn: () => api.auditLogs(),
    enabled: hasComplianceRole,
  });

  const verifyMutation = useMutation({
    mutationFn: () => api.verifyAuditChain(),
    onMutate: () => {
      setVerifyResult(null);
    },
    onSuccess: (data) => {
      setVerifyResult(data);
    },
  });

  if (!hasComplianceRole) {
    return (
      <div className="page">
        <PageHeader title="Compliance audit log" />
        <Alert tone="error">
          Access restricted: The compliance audit log requires the Compliance Officer or
          Administrator role.
        </Alert>
      </div>
    );
  }

  const items: AuditLogOut[] = logsQuery.data ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Compliance audit log"
        description="Tamper-evident chronological audit trail with SHA-256 cryptographic hash-chain verification."
        actions={
          <Button
            variant="secondary"
            loading={verifyMutation.isPending}
            onClick={() => {
              verifyMutation.mutate();
            }}
          >
            Verify Hash Chain
          </Button>
        }
      />

      {verifyMutation.isError && (
        <Alert tone="error">
          Verification request failed:{" "}
          {errorMessage(verifyMutation.error, "Could not verify audit hash chain.")}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              verifyMutation.mutate();
            }}
            style={{ marginLeft: "var(--space-2)" }}
          >
            Retry
          </Button>
        </Alert>
      )}

      {verifyResult && (
        <div className="audit-verification-banner" data-testid="verification-banner">
          {verifyResult.valid ? (
            <Alert tone="success">
              Hash chain intact. Verified {verifyResult.checked} audit log{" "}
              {verifyResult.checked === 1 ? "entry" : "entries"} with no tampering detected.
            </Alert>
          ) : (
            <Alert tone="error">
              Hash chain broken! Data tampering detected at sequence #
              {verifyResult.first_invalid_seq}. Checked {verifyResult.checked} total entries.
            </Alert>
          )}
        </div>
      )}

      {logsQuery.isPending && <LoadingState label="Loading audit logs..." />}

      {logsQuery.isError && (
        <ErrorState
          message="Could not load compliance audit logs."
          onRetry={() => {
            void logsQuery.refetch();
          }}
        />
      )}

      {!logsQuery.isPending && !logsQuery.isError && items.length === 0 && (
        <EmptyState message="No audit log entries recorded yet." />
      )}

      {items.length > 0 && (
        <div
          className="entries table-scroll-container"
          tabIndex={0}
          role="region"
          aria-label="Compliance audit logs table"
          style={{ overflowX: "auto", maxWidth: "100%" }}
        >
          <table>
            <caption className="sr-only">Compliance audit logs, chronological order</caption>
            <thead>
              <tr>
                <th scope="col" style={{ whiteSpace: "nowrap" }}>
                  Seq
                </th>
                <th scope="col" style={{ whiteSpace: "nowrap" }}>
                  Timestamp
                </th>
                <th scope="col">Actor</th>
                <th scope="col">Action</th>
                <th scope="col">Resource</th>
                <th scope="col" style={{ whiteSpace: "nowrap" }}>
                  IP Address
                </th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => {
                const isTampered =
                  verifyResult !== null &&
                  !verifyResult.valid &&
                  verifyResult.first_invalid_seq === entry.seq;
                const detailKeys = Object.keys(entry.details);
                const hasDetails = detailKeys.length > 0;
                const ipValue = resolveIp(entry);

                return (
                  <tr
                    key={entry.seq}
                    data-seq={entry.seq}
                    className={isTampered ? "row--invalid" : undefined}
                    style={
                      isTampered
                        ? {
                            backgroundColor: "color-mix(in srgb, var(--danger) 15%, transparent)",
                            outline: "2px solid var(--danger)",
                          }
                        : undefined
                    }
                  >
                    <td style={{ whiteSpace: "nowrap" }}>
                      #{entry.seq}
                      {isTampered && (
                        <span className="badge badge--danger" style={{ marginLeft: "0.5rem" }}>
                          TAMPERED
                        </span>
                      )}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>{formatDateTime(entry.created_at)}</td>
                    <td>
                      {entry.actor_user_id ? (
                        <code title={entry.actor_user_id}>{entry.actor_user_id.slice(0, 8)}…</code>
                      ) : (
                        <span className="muted">System</span>
                      )}
                    </td>
                    <td>
                      <span className="badge badge--neutral">{entry.action}</span>
                    </td>
                    <td>
                      {entry.resource_type}
                      {entry.resource_id ? `:${entry.resource_id.slice(0, 8)}…` : ""}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {ipValue === "—" ? (
                        <span className="muted" title="No IP address recorded for this event">
                          —
                        </span>
                      ) : (
                        ipValue
                      )}
                    </td>
                    <td style={{ minWidth: "180px", maxWidth: "340px" }}>
                      {!hasDetails ? (
                        <span className="muted">—</span>
                      ) : (
                        <details style={{ fontSize: "var(--text-xs)" }}>
                          <summary style={{ cursor: "pointer", color: "var(--brand)" }}>
                            {formatDetailsSummary(entry.details)}
                          </summary>
                          <pre
                            style={{
                              margin: "var(--space-1) 0 0",
                              padding: "var(--space-2)",
                              background: "var(--surface)",
                              border: "1px solid var(--border)",
                              borderRadius: "var(--radius-sm)",
                              overflowX: "auto",
                              maxHeight: "150px",
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-all",
                            }}
                          >
                            {JSON.stringify(entry.details, null, 2)}
                          </pre>
                        </details>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
