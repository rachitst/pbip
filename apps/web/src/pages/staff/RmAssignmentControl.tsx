import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../../api/client";
import type { AssignedRm } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { formatDate } from "../../lib/format";
import { Alert } from "../../ui/Alert";
import { Button } from "../../ui/Button";
import { Field } from "../../ui/Field";
import { ErrorState, LoadingState } from "../../ui/States";
import "./RmAssignmentControl.css";

interface RmAssignmentControlProps {
  customerId: string;
  assigned: AssignedRm | null | undefined;
  /** Only an administrator may change it (ADR-0032); everyone else sees who is responsible. */
  canAssign: boolean;
}

/** ADR-0032: who looks after this customer, and (ADMIN only) assign, reassign or unassign. */
export function RmAssignmentControl({ customerId, assigned, canAssign }: RmAssignmentControlProps) {
  return (
    <section className="flyout-section rm-assignment" aria-labelledby="rm-assignment">
      <h3 id="rm-assignment">Relationship manager</h3>
      <p className="rm-assignment__current">
        {assigned ? (
          <>
            <strong>{assigned.rm_email}</strong>{" "}
            <span className="muted">since {formatDate(assigned.assigned_at)}</span>
          </>
        ) : (
          <span className="muted">
            Unassigned. Only administrators see this customer until one is assigned.
          </span>
        )}
      </p>
      {canAssign && <AssignForm customerId={customerId} assigned={assigned ?? null} />}
    </section>
  );
}

function AssignForm({ customerId, assigned }: { customerId: string; assigned: AssignedRm | null }) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState(assigned?.rm_user_id ?? "");
  const rms = useQuery({
    queryKey: ["insights", "relationship-managers"],
    queryFn: () => api.relationshipManagers(),
  });
  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ["insights"] });
  }
  const assign = useMutation({
    mutationFn: (rmUserId: string) => api.assignRelationshipManager(customerId, rmUserId),
    onSuccess: refresh,
  });
  const unassign = useMutation({
    mutationFn: () => api.unassignRelationshipManager(customerId),
    onSuccess: async () => {
      setChoice("");
      await refresh();
    },
  });

  if (rms.isPending) return <LoadingState label="Loading relationship managers..." />;
  if (rms.isError) {
    return (
      <ErrorState
        message={errorMessage(rms.error, "Could not load relationship managers.")}
        onRetry={() => {
          void rms.refetch();
        }}
      />
    );
  }
  const options = rms.data.items;
  if (options.length === 0) {
    return <p className="muted">There are no active relationship managers to assign.</p>;
  }

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (choice !== "") assign.mutate(choice);
  }

  const busy = assign.isPending || unassign.isPending;
  const failed = assign.error ?? unassign.error;
  return (
    <form className="rm-assignment__form" onSubmit={submit} noValidate>
      <Field label="Assign to">
        {(control) => (
          <select
            {...control}
            value={choice}
            onChange={(e) => {
              setChoice(e.target.value);
            }}
          >
            <option value="" disabled>
              Choose a relationship manager
            </option>
            {options.map((rm) => (
              <option key={rm.user_id} value={rm.user_id}>
                {rm.email}
              </option>
            ))}
          </select>
        )}
      </Field>
      <div className="rm-assignment__actions">
        <Button
          type="submit"
          size="sm"
          loading={assign.isPending}
          disabled={busy || choice === "" || choice === assigned?.rm_user_id}
        >
          {assigned ? "Reassign" : "Assign"}
        </Button>
        {assigned && (
          <Button
            variant="secondary"
            size="sm"
            loading={unassign.isPending}
            disabled={busy}
            onClick={() => {
              unassign.mutate();
            }}
          >
            Unassign
          </Button>
        )}
      </div>
      {failed && (
        <Alert tone="error">{errorMessage(failed, "Could not change the assignment.")}</Alert>
      )}
    </form>
  );
}
