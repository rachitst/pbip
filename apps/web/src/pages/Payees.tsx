import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { PayeeOut, PayeeVerificationOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import type { Tone } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

/** PBIP's own bank code (Settings.internal_bank_code): only these payees get a name check. */
export const INTERNAL_BANK_CODE = "PBIP0001";
const ACCOUNT_NUMBER = /^[0-9A-Za-z-]{6,20}$/;
const BANK_CODE = /^[0-9A-Za-z]{4,20}$/;

const STATUS: Record<string, { label: string; tone: Tone }> = {
  VERIFIED: { label: "Verified", tone: "success" },
  PENDING_VERIFICATION: { label: "Not verified", tone: "warning" },
  BLOCKED: { label: "Removed", tone: "neutral" },
};

/** Confirmation of Payee result, in words. The real holder's name is never revealed. */
export function NameCheck({ result }: { result: PayeeVerificationOut["name_match"] }) {
  if (result === "MATCH") {
    return <Alert tone="success">The name matches the account. You can now send money.</Alert>;
  }
  if (result === "NO_MATCH") {
    return (
      <Alert tone="warning">
        The name you entered does not match the name on this account, so the payee is not verified.
        Check the exact name with the person you are paying, then edit it and verify again.
      </Alert>
    );
  }
  return (
    <Alert tone="info">
      This account is at another bank, so its name cannot be checked. The payee is verified: make
      sure the details are right before you send money.
    </Alert>
  );
}

function addProblem(name: string, accountNumber: string, bankCode: string): string | null {
  if (name.trim().length < 2) return "Enter the payee's full name.";
  if (!ACCOUNT_NUMBER.test(accountNumber)) {
    return "The account number must be 6 to 20 letters, digits or hyphens.";
  }
  if (!BANK_CODE.test(bankCode)) return "The bank code must be 4 to 20 letters or digits.";
  return null;
}

function AddPayee({ onClose }: { onClose: (result?: PayeeVerificationOut) => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [accountNumber, setAccountNumber] = useState("");
  const [bankCode, setBankCode] = useState(INTERNAL_BANK_CODE);
  const [nickname, setNickname] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  // Set once the payee exists, so a retry after a failed name check only re-runs the check.
  const [createdId, setCreatedId] = useState<string | null>(null);
  // Add, then run the name check straight away: a payee is only usable once verified.
  const add = useMutation({
    mutationFn: async () => {
      let id = createdId;
      if (id === null) {
        const payee = await api.addPayee({
          name: name.trim(),
          account_number: accountNumber.trim(),
          bank_code: bankCode.trim(),
          nickname: nickname.trim() === "" ? null : nickname.trim(),
        });
        id = payee.id;
        setCreatedId(id);
      }
      return api.verifyPayee(id);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["payees"] }),
    onSuccess: (result) => {
      onClose(result);
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    const found = addProblem(name, accountNumber.trim(), bankCode.trim());
    setProblem(found);
    if (!found) add.mutate();
  }

  const failure = createdId
    ? "The payee was added, but its name could not be checked. Try again."
    : "Could not add the payee.";
  const message = problem ?? (add.isError ? errorMessage(add.error, failure) : null);
  return (
    <form onSubmit={submit}>
      <Field label="Payee name" hint="Exactly as on their bank account.">
        {(control) => (
          <input
            {...control}
            value={name}
            maxLength={200}
            readOnly={createdId !== null}
            onChange={(e) => {
              setName(e.target.value);
            }}
            required
          />
        )}
      </Field>
      <Field label="Account number">
        {(control) => (
          <input
            {...control}
            value={accountNumber}
            maxLength={20}
            autoComplete="off"
            readOnly={createdId !== null}
            onChange={(e) => {
              setAccountNumber(e.target.value);
            }}
            required
          />
        )}
      </Field>
      <Field
        label="Bank code"
        hint={`${INTERNAL_BANK_CODE} is PBIP. Other banks' payees are not name-checked.`}
      >
        {(control) => (
          <input
            {...control}
            value={bankCode}
            maxLength={20}
            readOnly={createdId !== null}
            onChange={(e) => {
              setBankCode(e.target.value);
            }}
            required
          />
        )}
      </Field>
      <Field label="Nickname (optional)">
        {(control) => (
          <input
            {...control}
            value={nickname}
            maxLength={80}
            onChange={(e) => {
              setNickname(e.target.value);
            }}
          />
        )}
      </Field>
      {message && <Alert tone="error">{message}</Alert>}
      <div className="dialog__actions">
        <Button
          variant="secondary"
          onClick={() => {
            onClose();
          }}
        >
          Cancel
        </Button>
        <Button type="submit" loading={add.isPending}>
          Add and check name
        </Button>
      </div>
    </form>
  );
}

function EditPayee({ payee, onClose }: { payee: PayeeOut; onClose: (renamed: boolean) => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(payee.name);
  const [nickname, setNickname] = useState(payee.nickname ?? "");
  const renaming = name.trim() !== payee.name;
  const save = useMutation({
    mutationFn: () =>
      api.updatePayee(payee.id, {
        ...(renaming ? { name: name.trim() } : {}),
        nickname: nickname.trim() === "" ? null : nickname.trim(),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["payees"] });
      onClose(renaming);
    },
  });
  function submit(event: SyntheticEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <form onSubmit={submit}>
      <p className="muted">
        Account {payee.account_number_masked} at {payee.bank_code} cannot be changed. To pay a
        different account, add a new payee.
      </p>
      <Field label="Payee name">
        {(control) => (
          <input
            {...control}
            value={name}
            minLength={2}
            maxLength={200}
            onChange={(e) => {
              setName(e.target.value);
            }}
            required
          />
        )}
      </Field>
      <Field label="Nickname (optional)">
        {(control) => (
          <input
            {...control}
            value={nickname}
            maxLength={80}
            onChange={(e) => {
              setNickname(e.target.value);
            }}
          />
        )}
      </Field>
      {renaming && (
        <Alert tone="warning">
          Changing the name means the payee must be verified again before you can send money.
        </Alert>
      )}
      {save.isError && <Alert tone="error">{errorMessage(save.error, "Could not save.")}</Alert>}
      <div className="dialog__actions">
        <Button
          variant="secondary"
          onClick={() => {
            onClose(false);
          }}
        >
          Cancel
        </Button>
        <Button type="submit" loading={save.isPending}>
          Save
        </Button>
      </div>
    </form>
  );
}

function PayeeRow({
  payee,
  onEdit,
  onRemove,
  onChecked,
}: {
  payee: PayeeOut;
  onEdit: () => void;
  onRemove: () => void;
  onChecked: (result: PayeeVerificationOut) => void;
}) {
  const queryClient = useQueryClient();
  const verify = useMutation({
    mutationFn: () => api.verifyPayee(payee.id),
    onSuccess: async (result) => {
      onChecked(result);
      await queryClient.invalidateQueries({ queryKey: ["payees"] });
    },
  });
  const removed = payee.status === "BLOCKED";
  return (
    <tr>
      <td>
        {payee.name}
        {payee.nickname && <span className="muted"> ({payee.nickname})</span>}
      </td>
      <td>
        {payee.account_number_masked} · {payee.bank_code}
      </td>
      <td>
        <StatusBadge status={STATUS[payee.status] ?? { label: payee.status, tone: "neutral" }} />
      </td>
      <td>{formatDate(payee.created_at)}</td>
      <td className="row-actions">
        {payee.status === "PENDING_VERIFICATION" && (
          <Button
            size="sm"
            loading={verify.isPending}
            onClick={() => {
              verify.mutate();
            }}
          >
            Verify
          </Button>
        )}
        {!removed && (
          <>
            <Button size="sm" variant="secondary" onClick={onEdit}>
              Edit
            </Button>
            <Button size="sm" variant="ghost" onClick={onRemove}>
              Remove
            </Button>
          </>
        )}
        {verify.isError && (
          <Alert tone="error">{errorMessage(verify.error, "Could not check the name.")}</Alert>
        )}
      </td>
    </tr>
  );
}

/** TASK-020 + TASK-094: add, check, rename and remove payees. */
export function Payees() {
  const queryClient = useQueryClient();
  const payees = useQuery({ queryKey: ["payees"], queryFn: api.payees });
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<PayeeOut | null>(null);
  const [removing, setRemoving] = useState<PayeeOut | null>(null);
  const [check, setCheck] = useState<PayeeVerificationOut | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: (payee: PayeeOut) => api.removePayee(payee.id),
    onSuccess: async () => {
      setRemoving(null);
      setNotice("Payee removed.");
      await queryClient.invalidateQueries({ queryKey: ["payees"] });
    },
  });

  return (
    <div className="page">
      <PageHeader
        title="Payees"
        description="People and accounts you pay. A payee can receive money once its name is verified."
        actions={
          <Button
            onClick={() => {
              setCheck(null);
              setNotice(null);
              setAdding(true);
            }}
          >
            Add payee
          </Button>
        }
      />
      {check && <NameCheck result={check.name_match} />}
      {notice && <Alert tone="success">{notice}</Alert>}
      {payees.isPending && <LoadingState label="Loading your payees..." />}
      {payees.isError && (
        <ErrorState
          message={errorMessage(payees.error, "Could not load your payees.")}
          onRetry={() => {
            void payees.refetch();
          }}
        />
      )}
      {payees.data?.length === 0 && <EmptyState message="You have no payees yet." />}
      {payees.data && payees.data.length > 0 && (
        <div className="entries">
          <table>
            <caption className="sr-only">Your payees</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Account</th>
                <th scope="col">Status</th>
                <th scope="col">Added</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {payees.data.map((p) => (
                <PayeeRow
                  key={p.id}
                  payee={p}
                  onChecked={(result) => {
                    setNotice(null);
                    setCheck(result);
                  }}
                  onEdit={() => {
                    setCheck(null);
                    setNotice(null);
                    setEditing(p);
                  }}
                  onRemove={() => {
                    remove.reset();
                    setRemoving(p);
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Dialog
        open={adding}
        title="Add a payee"
        onClose={() => {
          setAdding(false);
        }}
      >
        {adding && (
          <AddPayee
            onClose={(result) => {
              setAdding(false);
              if (result) setCheck(result);
            }}
          />
        )}
      </Dialog>
      <Dialog
        open={editing !== null}
        title="Edit payee"
        onClose={() => {
          setEditing(null);
        }}
      >
        {editing && (
          <EditPayee
            payee={editing}
            onClose={(renamed) => {
              setEditing(null);
              if (renamed) setNotice("Name changed. Verify the payee before sending money.");
            }}
          />
        )}
      </Dialog>
      <ConfirmDialog
        open={removing !== null}
        title="Remove this payee?"
        items={
          removing
            ? [
                { label: "Payee", value: removing.name },
                { label: "Account", value: removing.account_number_masked },
              ]
            : []
        }
        confirmLabel="Remove payee"
        tone="danger"
        busy={remove.isPending}
        error={remove.isError ? errorMessage(remove.error, "Could not remove the payee.") : null}
        onConfirm={() => {
          if (removing) remove.mutate(removing);
        }}
        onCancel={() => {
          setRemoving(null);
        }}
      />
    </div>
  );
}
