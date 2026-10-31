import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api } from "../../api/client";
import type { AuditLogOut, ChainStatusOut } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDateTime } from "../../lib/format";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Button } from "../../ui/Button";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";

/** Page size for the keyset-paginated log (newest first; `before_seq` fetches older pages). */
export const AUDIT_PAGE_SIZE = 50;

const IP_KEYS = new Set(["ip", "ip_address"]);

function resolveIp(entry: AuditLogOut): string {
  if (typeof entry.ip === "string" && entry.ip.length > 0) return entry.ip;
  if (typeof entry.details.ip === "string" && entry.details.ip.length > 0) return entry.details.ip;
  if (typeof entry.details.ip_address === "string" && entry.details.ip_address.length > 0) {
    return entry.details.ip_address;
  }
  return "—";
}

/** The actual value of a details field: lists joined, nested objects as compact JSON. */
export function detailValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return value.map(detailValue).join(", ");
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

function detailFields(details: Record<string, unknown>): [string, string][] {
  return Object.entries(details)
    .filter(([key]) => !IP_KEYS.has(key))
    .map(([key, value]) => [key, detailValue(value)]);
}

/** One line for the collapsed row: the first two fields with their values. */
export function detailsSummary(details: Record<string, unknown>): string {
  const fields = detailFields(details);
  const shown = fields
    .slice(0, 2)
    .map(([key, value]) => `${key}: ${value}`)
    .join(" · ");
  return fields.length > 2 ? `${shown} · +${fields.length - 2} more` : shown;
}

/** "Entries #1–#119 verified": `checked` counts the entries verified before the break. */
function brokenChainMessage(result: ChainStatusOut): string {
  if (result.first_invalid_seq === null) {
    return `Hash chain broken. ${result.checked} entries verified before the break.`;
  }
  const verified =
    result.first_invalid_seq > 1
      ? `Entries #1–#${result.first_invalid_seq - 1} verified.`
      : "No earlier entries to verify.";
  return `Hash chain broken at entry #${result.first_invalid_seq}. ${verified}`;
}

export function AuditLog() {
  const me = useCurrentUser();
  const hasComplianceRole = me.roles.includes("COMPLIANCE_OFFICER") || me.roles.includes("ADMIN");

  const [verifyResult, setVerifyResult] = useState<ChainStatusOut | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());

  const logsQuery = useInfiniteQuery({
    queryKey: ["compliance", "audit-logs"],
    queryFn: ({ pageParam }) => api.auditLogs(AUDIT_PAGE_SIZE, pageParam),
    initialPageParam: undefined as number | undefined,
    // A short page is the oldest one; otherwise continue below its last (oldest) sequence number.
    getNextPageParam: (page) => {
      const oldest = page.at(-1)?.seq;
      return page.length < AUDIT_PAGE_SIZE || oldest === undefined || oldest <= 1
        ? undefined
        : oldest;
    },
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

  const toggle = (seq: number) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  };

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

  const items: AuditLogOut[] = logsQuery.data?.pages.flat() ?? [];
  const brokenSeq = verifyResult && !verifyResult.valid ? verifyResult.first_invalid_seq : null;
  const brokenEntryLoaded = brokenSeq !== null && items.some((entry) => entry.seq === brokenSeq);

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
            Verify hash chain
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
            className="audit-log__retry"
            onClick={() => {
              verifyMutation.mutate();
            }}
          >
            Retry
          </Button>
        </Alert>
      )}

      {verifyResult && (
        <div data-testid="verification-banner">
          {verifyResult.valid ? (
            <Alert tone="success">
              Hash chain intact. Verified {verifyResult.checked} audit log{" "}
              {verifyResult.checked === 1 ? "entry" : "entries"} with no tampering detected.
            </Alert>
          ) : (
            <Alert tone="error">
              {brokenChainMessage(verifyResult)}
              {brokenSeq !== null && !brokenEntryLoaded && (
                <>
                  {" "}
                  Entry #{brokenSeq} is older than the entries shown: load older entries to reach
                  it.
                </>
              )}
            </Alert>
          )}
        </div>
      )}

      {logsQuery.isPending && <LoadingState label="Loading audit logs..." />}

      {logsQuery.isError && !logsQuery.isFetchNextPageError && (
        <ErrorState
          message={errorMessage(logsQuery.error, "Could not load compliance audit logs.")}
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
          className="entries"
          tabIndex={0}
          role="region"
          aria-label="Compliance audit logs table"
        >
          <table className="audit-log">
            <caption className="sr-only">Compliance audit logs, chronological order</caption>
            <thead>
              <tr>
                <th scope="col">Seq</th>
                <th scope="col">Timestamp</th>
                <th scope="col">Actor</th>
                <th scope="col">Action</th>
                <th scope="col">Resource</th>
                <th scope="col">IP Address</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => {
                const isTampered = brokenSeq !== null && brokenSeq === entry.seq;
                const fields = detailFields(entry.details);
                const resource = entry.resource_id
                  ? `${entry.resource_type}:${entry.resource_id}`
                  : null;
                const panelFields: [string, string][] = resource
                  ? [["resource", resource], ...fields]
                  : fields;
                const ipValue = resolveIp(entry);
                const isOpen = expanded.has(entry.seq);
                const panelId = `audit-details-${entry.seq}`;

                return (
                  <Fragment key={entry.seq}>
                    <tr
                      data-seq={entry.seq}
                      className={isTampered ? "audit-log__row--tampered" : undefined}
                    >
                      <td className="audit-log__seq">
                        #{entry.seq}
                        {isTampered && (
                          <span className="badge badge--danger audit-log__badge">TAMPERED</span>
                        )}
                      </td>
                      <td className="audit-log__nowrap">{formatDateTime(entry.created_at)}</td>
                      <td>
                        {entry.actor_user_id ? (
                          <code title={entry.actor_user_id}>
                            {entry.actor_user_id.slice(0, 8)}…
                          </code>
                        ) : (
                          <span className="muted">System</span>
                        )}
                      </td>
                      <td>
                        <span className="badge badge--neutral">{entry.action}</span>
                      </td>
                      <td title={resource ?? undefined}>{entry.resource_type}</td>
                      <td className="audit-log__nowrap">
                        {ipValue === "—" ? (
                          <span className="muted" title="No IP address recorded for this event">
                            —
                          </span>
                        ) : (
                          ipValue
                        )}
                      </td>
                      <td className="audit-log__details">
                        {panelFields.length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          <button
                            type="button"
                            className="audit-log__toggle"
                            aria-expanded={isOpen}
                            aria-controls={panelId}
                            title={
                              fields.length > 0 ? detailsSummary(entry.details) : (resource ?? "")
                            }
                            onClick={() => {
                              toggle(entry.seq);
                            }}
                          >
                            <span className="audit-log__summary">
                              {fields.length > 0 ? detailsSummary(entry.details) : "Resource id"}
                            </span>
                            <span className="sr-only"> (details of entry #{entry.seq})</span>
                          </button>
                        )}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr id={panelId} className="audit-log__panel">
                        <td colSpan={7}>
                          <dl className="details">
                            {panelFields.map(([key, value]) => (
                              <div key={key} className="details__row">
                                <dt>{key}</dt>
                                <dd>{value}</dd>
                              </div>
                            ))}
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {logsQuery.isFetchNextPageError && (
        <Alert tone="error">
          {errorMessage(logsQuery.error, "Could not load older audit log entries.")}
        </Alert>
      )}

      {logsQuery.hasNextPage && (
        <Button
          variant="secondary"
          loading={logsQuery.isFetchingNextPage}
          onClick={() => {
            void logsQuery.fetchNextPage();
          }}
        >
          Load older entries
        </Button>
      )}
    </div>
  );
}
