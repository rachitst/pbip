import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { TransferOut } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { formatMinor, parseToMinor } from "../lib/money";

export function Accounts() {
  const { logout } = useAuth();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const payees = useQuery({ queryKey: ["payees"], queryFn: api.payees });

  return (
    <main className="card">
      <header className="row">
        <h1>My accounts</h1>
        <button
          className="secondary"
          onClick={() => {
            void logout();
          }}
        >
          Sign out
        </button>
      </header>
      {accounts.isPending && <p>Loading accounts...</p>}
      {accounts.isError && <p role="alert">Could not load accounts.</p>}
      <ul className="list">
        {accounts.data?.map((account) => (
          <li key={account.id}>
            <div>
              <strong>{account.account_type}</strong>
              <div className="muted">{account.account_number_masked}</div>
            </div>
            <div className="right">
              <div aria-label="available balance">
                {formatMinor(account.available_minor, account.currency)}
              </div>
              {account.held_minor > 0 && (
                <div className="muted">{formatMinor(account.held_minor)} on hold</div>
              )}
            </div>
          </li>
        ))}
      </ul>
      {accounts.data?.[0] && (
        <SendMoney
          fromAccountId={accounts.data[0].id}
          payees={(payees.data ?? []).filter((p) => p.status === "VERIFIED")}
        />
      )}
    </main>
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
