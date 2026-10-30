import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { AccountOut, OpenAccountType, TransferOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatMinor, parseToMinor } from "../lib/money";
import { transferStatus } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { AccountTransactions } from "./AccountTransactions";

const ACCOUNT_LABELS: Record<string, string> = {
  SAVINGS: "Savings",
  CURRENT: "Current (checking)",
  CHECKING: "Current (checking)",
};

export function Accounts() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const payees = useQuery({ queryKey: ["payees"], queryFn: api.payees });
  const [openedId, setOpenedId] = useState<string | null>(null);

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
            onToggle={() => {
              setOpenedId(openedId === account.id ? null : account.id);
            }}
          />
        ))}
      </ul>
      <OpenAccount />
      {accounts.data?.[0] && (
        <SendMoney
          fromAccountId={accounts.data[0].id}
          payees={(payees.data ?? []).filter((p) => p.status === "VERIFIED")}
        />
      )}
    </div>
  );
}

interface AccountRowProps {
  account: AccountOut;
  expanded: boolean;
  onToggle: () => void;
}

function AccountRow({ account, expanded, onToggle }: AccountRowProps) {
  const panelId = `transactions-${account.id}`;
  return (
    <li className="account">
      <div className="row account-header">
        <div>
          <strong>{ACCOUNT_LABELS[account.account_type] ?? account.account_type}</strong>
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
          <AccountTransactions accountId={account.id} currency={account.currency} />
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

interface SendMoneyProps {
  fromAccountId: string;
  payees: { id: string; name: string; account_number_masked: string }[];
}

function SendMoney({ fromAccountId, payees }: SendMoneyProps) {
  const queryClient = useQueryClient();
  const [payeeId, setPayeeId] = useState("");
  const [amount, setAmount] = useState("");
  const [result, setResult] = useState<TransferOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  // ONE key per user intent. If the request times out and the user presses the button again,
  // the SAME key is reused, so a retry can never create a duplicate transfer.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setError(null);
    const minor = parseToMinor(amount);
    if (minor === null || !payeeId) {
      setError("Choose a payee and enter a valid amount (up to 2 decimals).");
      return;
    }
    setSending(true);
    try {
      const transfer = await api.transfer(
        { from_account_id: fromAccountId, payee_id: payeeId, amount_minor: minor },
        intentKey,
      );
      setResult(transfer);
      setIntentKey(crypto.randomUUID()); // intent finished: next submit is a NEW intent
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      await queryClient.invalidateQueries({ queryKey: ["entries"] });
    } catch (caught) {
      setError(errorMessage(caught, "The transfer could not be sent. Try again."));
    } finally {
      setSending(false);
    }
  }

  return (
    <section>
      <h2>Send money</h2>
      <form onSubmit={(event) => void submit(event)}>
        <Field label="Payee">
          {(control) => (
            <select
              {...control}
              value={payeeId}
              onChange={(event) => {
                setPayeeId(event.target.value);
              }}
            >
              <option value="">Select a verified payee</option>
              {payees.map((payee) => (
                <option key={payee.id} value={payee.id}>
                  {payee.name} ({payee.account_number_masked})
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Amount (INR)">
          {(control) => (
            <MoneyInput {...control} value={amount} onValueChange={setAmount} currency="INR" />
          )}
        </Field>
        {error && <Alert tone="error">{error}</Alert>}
        <Button type="submit" loading={sending}>
          Send
        </Button>
      </form>
      {result && (
        <Alert tone={transferStatus(result.status).tone === "success" ? "success" : "warning"}>
          {result.message} ({formatMinor(result.amount_minor, result.currency)})
        </Alert>
      )}
    </section>
  );
}
