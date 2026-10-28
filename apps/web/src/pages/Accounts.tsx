import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { ApiError, api } from "../api/client";
import type { AccountOut, OpenAccountType, TransferOut } from "../api/client";
import { formatMinor, parseToMinor } from "../lib/money";
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
    <main>
      <h1>My accounts</h1>
      {accounts.isPending && <p>Loading accounts...</p>}
      {accounts.isError && <p role="alert">Could not load accounts.</p>}
      {accounts.data?.length === 0 && <p className="muted">You have no accounts yet.</p>}
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
    </main>
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
          <button
            type="button"
            className="link"
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={onToggle}
          >
            {expanded ? "Hide transactions" : "Transactions"}
          </button>
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
        <label>
          Account type
          <select
            value={accountType}
            onChange={(event) => {
              setAccountType(event.target.value === "CHECKING" ? "CHECKING" : "SAVINGS");
            }}
          >
            <option value="SAVINGS">Savings</option>
            <option value="CHECKING">Current (checking)</option>
          </select>
        </label>
        <button type="submit" disabled={open.isPending}>
          {open.isPending ? "Opening..." : "Open account"}
        </button>
      </form>
      {open.isSuccess && (
        <p role="status" className="ok">
          Account {open.data.account_number_masked} opened.
        </p>
      )}
      {open.isError && (
        <p role="alert" className="error">
          {open.error instanceof ApiError ? open.error.message : "Could not open the account."}
        </p>
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
      setError(caught instanceof Error ? caught.message : "Transfer failed.");
    }
  }

  return (
    <section>
      <h2>Send money</h2>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          Payee
          <select
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
        </label>
        <label>
          Amount (INR)
          <input
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
            }}
            inputMode="decimal"
            placeholder="0.00"
          />
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <button type="submit">Send</button>
      </form>
      {result && (
        <p role="status" className={result.status === "POSTED" ? "ok" : "warn"}>
          {result.message} ({formatMinor(result.amount_minor, result.currency)})
        </p>
      )}
    </section>
  );
}
