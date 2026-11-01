import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { api } from "../api/client";
import type { AccountOut, OpenAccountType } from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel } from "../lib/format";
import { formatMinor } from "../lib/money";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { AccountTransactions } from "./AccountTransactions";
import { SendMoney } from "./SendMoney";

export function Accounts() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const payees = useQuery({ queryKey: ["payees"], queryFn: api.payees });
  // A notification link (?account=&transaction=) opens that account and highlights the transaction;
  // a new link replaces whatever the customer had opened (state is reset during render, not in an
  // effect, so there is no flash of the old state).
  const [params] = useSearchParams();
  const linkedAccount = params.get("account");
  const linkedTransaction = params.get("transaction");
  const [openedId, setOpenedId] = useState<string | null>(linkedAccount);
  const [link, setLink] = useState(params.toString());
  if (link !== params.toString()) {
    setLink(params.toString());
    setOpenedId(linkedAccount);
  }

  return (
    <div className="page">
      <PageHeader title="My accounts" />
      {accounts.isPending && <LoadingState label="Loading accounts..." />}
      {accounts.isError && (
        <ErrorState
          message="Could not load accounts."
          onRetry={() => {
            void accounts.refetch();
          }}
        />
      )}
      {accounts.data?.length === 0 && <EmptyState message="You have no accounts yet." />}
      <ul className="list">
        {accounts.data?.map((account) => (
          <AccountRow
            key={account.id}
            account={account}
            expanded={openedId === account.id}
            highlightTransactionId={account.id === linkedAccount ? linkedTransaction : null}
            onToggle={() => {
              setOpenedId(openedId === account.id ? null : account.id);
            }}
          />
        ))}
      </ul>
      <OpenAccount />
      {accounts.data && accounts.data.length > 0 && (
        <SendMoney
          accounts={accounts.data.filter((a) => a.status === "ACTIVE")}
          payees={(payees.data ?? []).filter((p) => p.status === "VERIFIED")}
        />
      )}
    </div>
  );
}

interface AccountRowProps {
  account: AccountOut;
  expanded: boolean;
  highlightTransactionId: string | null;
  onToggle: () => void;
}

function AccountRow({ account, expanded, highlightTransactionId, onToggle }: AccountRowProps) {
  const panelId = `transactions-${account.id}`;
  return (
    <li className="account">
      <div className="row account-header">
        <div>
          <strong>{accountTypeLabel(account.account_type)}</strong>
          <div className="muted">{account.account_number_masked}</div>
        </div>
        <div className="right account-balances">
          <div aria-label="available balance" className="balance-available">
            {formatMinor(account.available_minor, account.currency)}
          </div>
          <div className="balance-sub">
            <div aria-label="ledger balance" className="muted">
              Ledger: {formatMinor(account.ledger_balance_minor, account.currency)}
            </div>
            <div aria-label="held funds" className="muted">
              {formatMinor(account.held_minor, account.currency)} on hold
            </div>
          </div>
          <Button
            variant="link"
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={onToggle}
          >
            {expanded ? "Hide transactions" : "Transactions"}
          </Button>
        </div>
      </div>
      {expanded && (
        <div id={panelId}>
          <AccountTransactions
            accountId={account.id}
            currency={account.currency}
            highlightTransactionId={highlightTransactionId}
          />
        </div>
      )}
    </li>
  );
}

function OpenAccount() {
  const queryClient = useQueryClient();
  const [accountType, setAccountType] = useState<OpenAccountType>("SAVINGS");
  const open = useMutation({
    mutationFn: (type: OpenAccountType) => api.openAccount(type),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["accounts"] }),
  });

  return (
    <section>
      <h2>Open an account</h2>
      <form
        className="inline"
        onSubmit={(event) => {
          event.preventDefault();
          open.mutate(accountType);
        }}
      >
        <Field label="Account type">
          {(control) => (
            <select
              {...control}
              value={accountType}
              onChange={(event) => {
                setAccountType(event.target.value === "CHECKING" ? "CHECKING" : "SAVINGS");
              }}
            >
              <option value="SAVINGS">Savings</option>
              <option value="CHECKING">Current (checking)</option>
            </select>
          )}
        </Field>
        <Button type="submit" loading={open.isPending}>
          {open.isPending ? "Opening..." : "Open account"}
        </Button>
      </form>
      {open.isSuccess && (
        <Alert tone="success">Account {open.data.account_number_masked} opened.</Alert>
      )}
      {open.isError && (
        <Alert tone="error">{errorMessage(open.error, "Could not open the account.")}</Alert>
      )}
    </section>
  );
}
