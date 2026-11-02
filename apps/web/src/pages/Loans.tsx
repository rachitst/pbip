import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useState } from "react";
import { api } from "../api/client";
import type {
  AccountOut,
  EligibilityOut,
  LoanOut,
  LoanProductOut,
  LoanSummaryOut,
} from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel, formatDate } from "../lib/format";
import { formatMinor } from "../lib/money";
import type { Tone } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

/** Slider step for the principal: INR 5,000 in paise. Every catalogue bound is a multiple. */
export const AMOUNT_STEP_MINOR = 5_000_00;
/** Wait for the slider to settle before asking the server (one request per pause, not per pixel). */
export const DEBOUNCE_MS = 300;
const UPCOMING_SHOWN = 6;

const INSTALMENT_STATUS: Record<string, { label: string; tone: Tone }> = {
  PAID: { label: "Paid", tone: "success" },
  DUE: { label: "Upcoming", tone: "neutral" },
  OVERDUE: { label: "Overdue", tone: "danger" },
};

/** Basis points to "11.50% p.a." with integer arithmetic only. */
export function formatRate(bps: number): string {
  return `${String(Math.trunc(bps / 100))}.${String(bps % 100).padStart(2, "0")}% p.a.`;
}

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSettled(value);
    }, ms);
    return () => {
      clearTimeout(timer);
    };
  }, [value, ms]);
  return settled;
}

interface Terms {
  productId: string;
  amountMinor: number;
  tenureMonths: number;
}

function sameTerms(a: Terms, b: Terms): boolean {
  return (
    a.productId === b.productId &&
    a.amountMinor === b.amountMinor &&
    a.tenureMonths === b.tenureMonths
  );
}

function accountLabel(account: AccountOut): string {
  return `${accountTypeLabel(account.account_type)} ${account.account_number_masked}`;
}

// ------------------------------------------------------------------ active loans
function UpcomingSchedule({ loanId }: { loanId: string }) {
  const loan = useQuery({ queryKey: ["loan", loanId], queryFn: () => api.loan(loanId) });
  if (loan.isPending) return <LoadingState label="Loading the repayment schedule..." />;
  if (loan.isError) {
    return (
      <ErrorState
        message={errorMessage(loan.error, "Could not load the repayment schedule.")}
        onRetry={() => {
          void loan.refetch();
        }}
      />
    );
  }
  const unpaid = loan.data.instalments.filter((i) => i.status !== "PAID");
  if (unpaid.length === 0) return <EmptyState message="Every EMI has been paid." />;
  return (
    <div className="entries">
      <table>
        <caption className="sr-only">Upcoming EMIs, earliest first</caption>
        <thead>
          <tr>
            <th scope="col">EMI</th>
            <th scope="col">Due date</th>
            <th scope="col" className="right">
              Amount
            </th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {unpaid.slice(0, UPCOMING_SHOWN).map((i) => (
            <tr key={i.seq}>
              <td>
                {i.seq} of {loan.data.tenure_months}
              </td>
              <td>{formatDate(i.due_date)}</td>
              <td className="right">{formatMinor(i.amount_minor, loan.data.currency)}</td>
              <td>
                <StatusBadge
                  status={INSTALMENT_STATUS[i.status] ?? { label: i.status, tone: "neutral" }}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LoanCard({ loan }: { loan: LoanSummaryOut }) {
  const [open, setOpen] = useState(false);
  const headingId = useId();
  const repaid = loan.principal_minor - loan.outstanding_principal_minor;
  return (
    <section className="loan-card" aria-labelledby={headingId}>
      <h3 id={headingId}>{loan.product_name}</h3>
      {loan.overdue_instalments > 0 && (
        <Alert tone="warning">
          {loan.overdue_instalments === 1
            ? "One EMI is overdue."
            : `${String(loan.overdue_instalments)} EMIs are overdue.`}{" "}
          Keep enough money in the account: we retry every day.
        </Alert>
      )}
      <dl className="details">
        <div className="details__row">
          <dt>Outstanding principal</dt>
          <dd>{formatMinor(loan.outstanding_principal_minor, loan.currency)}</dd>
        </div>
        <div className="details__row">
          <dt>Monthly EMI</dt>
          <dd>{formatMinor(loan.emi_minor, loan.currency)}</dd>
        </div>
        <div className="details__row">
          <dt>Next EMI</dt>
          <dd>
            {loan.next_due_date && loan.next_due_amount_minor !== null
              ? `${formatMinor(loan.next_due_amount_minor, loan.currency)} on ${formatDate(loan.next_due_date)}`
              : "None"}
          </dd>
        </div>
        <div className="details__row">
          <dt>Interest rate</dt>
          <dd>{formatRate(loan.annual_rate_bps)}</dd>
        </div>
      </dl>
      <p>
        {loan.paid_instalments} of {loan.tenure_months} EMIs paid ·{" "}
        {formatMinor(repaid, loan.currency)} of {formatMinor(loan.principal_minor, loan.currency)}{" "}
        principal repaid
      </p>
      <progress
        className="meter meter--success"
        value={loan.paid_instalments}
        max={loan.tenure_months}
        aria-label={`${loan.product_name} repayment progress`}
      />
      <Button
        variant="ghost"
        size="sm"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
        }}
      >
        {open ? "Hide upcoming EMIs" : "Show upcoming EMIs"}
      </Button>
      {open && <UpcomingSchedule loanId={loan.id} />}
    </section>
  );
}

function ActiveLoans() {
  const loans = useQuery({ queryKey: ["loans"], queryFn: api.loans });
  if (loans.isPending) return <LoadingState label="Loading your loans..." />;
  if (loans.isError) {
    return (
      <ErrorState
        message={errorMessage(loans.error, "Could not load your loans.")}
        onRetry={() => {
          void loans.refetch();
        }}
      />
    );
  }
  if (loans.data.length === 0) return <EmptyState message="You have no active loans." />;
  return (
    <div className="loan-list">
      {loans.data.map((loan) => (
        <LoanCard key={loan.id} loan={loan} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- calculator
function EmiSummary({ terms }: { terms: Terms }) {
  const emi = useQuery({
    queryKey: ["loan-emi", terms],
    queryFn: () => api.calculateEmi(terms.productId, terms.amountMinor, terms.tenureMonths),
    placeholderData: (previous) => previous, // keep the last figure while the next one loads
  });
  if (emi.isPending) return <LoadingState label="Calculating your EMI..." />;
  if (emi.isError) {
    return <ErrorState message={errorMessage(emi.error, "Could not calculate the EMI.")} />;
  }
  return (
    <section className="loan-card" aria-label="EMI estimate">
      <dl className="details">
        <div className="details__row">
          <dt>Monthly EMI</dt>
          <dd>{formatMinor(emi.data.emi_minor, "INR")}</dd>
        </div>
        <div className="details__row">
          <dt>Total interest</dt>
          <dd>{formatMinor(emi.data.total_interest_minor, "INR")}</dd>
        </div>
        <div className="details__row">
          <dt>Total payable</dt>
          <dd>{formatMinor(emi.data.total_payable_minor, "INR")}</dd>
        </div>
      </dl>
    </section>
  );
}

function EligibilityCard({
  product,
  eligibility,
}: {
  product: LoanProductOut;
  eligibility: EligibilityOut;
}) {
  const eligible = eligibility.status === "ELIGIBLE";
  return (
    <section className="loan-card" aria-labelledby="eligibility-title">
      <h3 id="eligibility-title">Eligibility pre-check</h3>
      <StatusBadge
        status={
          eligible
            ? { label: "Eligible", tone: "success" }
            : { label: "Not eligible", tone: "danger" }
        }
      />
      <dl className="details">
        <div className="details__row">
          <dt>Maximum over {eligibility.tenure_months} months</dt>
          <dd>{formatMinor(eligibility.max_eligible_amount_minor, "INR")}</dd>
        </div>
        <div className="details__row">
          <dt>Interest rate</dt>
          <dd>{formatRate(product.annual_rate_bps)}</dd>
        </div>
      </dl>
      <ul>
        {eligibility.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
    </section>
  );
}

function Receipt({ loan, account }: { loan: LoanOut; account: AccountOut | undefined }) {
  const first = loan.instalments[0];
  return (
    <section className="card receipt" aria-labelledby="loan-receipt-title">
      <h2 id="loan-receipt-title">Loan approved and disbursed</h2>
      <dl className="details">
        <div className="details__row">
          <dt>Amount credited</dt>
          <dd>{formatMinor(loan.principal_minor, loan.currency)}</dd>
        </div>
        <div className="details__row">
          <dt>Credited to</dt>
          <dd>{account ? accountLabel(account) : "Your account"}</dd>
        </div>
        <div className="details__row">
          <dt>Monthly EMI</dt>
          <dd>{formatMinor(loan.emi_minor, loan.currency)}</dd>
        </div>
        {first && (
          <div className="details__row">
            <dt>First EMI due</dt>
            <dd>{formatDate(first.due_date)}</dd>
          </div>
        )}
        <div className="details__row">
          <dt>Disbursal reference</dt>
          <dd>{loan.disbursal_transaction_id}</dd>
        </div>
      </dl>
      <p>EMIs are collected automatically from this account on each due date.</p>
    </section>
  );
}

function Calculator({
  products,
  accounts,
}: {
  products: LoanProductOut[];
  accounts: AccountOut[];
}) {
  const queryClient = useQueryClient();
  const [productId, setProductId] = useState(products[0]?.id ?? "");
  const product = products.find((p) => p.id === productId) ?? products[0];
  const [amountMinor, setAmountMinor] = useState(product?.min_amount_minor ?? 0);
  const [tenureIndex, setTenureIndex] = useState(0);
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [reviewing, setReviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<LoanOut | null>(null);
  // ONE key per confirmed intent: retrying the same application reuses it (never disburses twice).
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());

  const tenureMonths = product?.tenures_months[tenureIndex] ?? 0;
  const currentId = product?.id ?? "";
  // Memoised so the debounce restarts only when a value changes, not on every render.
  const terms: Terms = useMemo(
    () => ({ productId: currentId, amountMinor, tenureMonths }),
    [currentId, amountMinor, tenureMonths],
  );
  const settled = useDebounced(terms, DEBOUNCE_MS);
  const eligibility = useQuery({
    queryKey: ["loan-eligibility", settled],
    queryFn: () =>
      api.checkEligibility(settled.productId, settled.amountMinor, settled.tenureMonths),
  });
  if (!product) return <EmptyState message="No loan products are available right now." />;

  const upToDate = sameTerms(terms, settled);
  const canApply =
    upToDate && eligibility.data?.status === "ELIGIBLE" && accountId !== "" && !applying;
  const account = accounts.find((a) => a.id === accountId);

  function chooseProduct(id: string) {
    const next = products.find((p) => p.id === id);
    if (!next) return;
    setProductId(id);
    setAmountMinor(next.min_amount_minor);
    setTenureIndex(0);
    setReceipt(null);
  }

  async function confirm() {
    setApplying(true);
    setApplyError(null);
    try {
      const loan = await api.applyForLoan(
        {
          product_id: settled.productId,
          amount_minor: settled.amountMinor,
          tenure_months: settled.tenureMonths,
          account_id: accountId,
        },
        intentKey,
      );
      setIntentKey(crypto.randomUUID());
      setReviewing(false);
      setReceipt(loan);
      await queryClient.invalidateQueries({ queryKey: ["loans"] });
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      await queryClient.invalidateQueries({ queryKey: ["loan-eligibility"] });
    } catch (caught) {
      setApplyError(errorMessage(caught, "The application could not be completed. Try again."));
    } finally {
      setApplying(false);
    }
  }

  return (
    <>
      <Field label="Loan product">
        {(control) => (
          <select
            {...control}
            value={product.id}
            onChange={(e) => {
              chooseProduct(e.target.value);
            }}
          >
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} · from {formatRate(p.annual_rate_bps)}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field
        label="Loan amount"
        hint={`${formatMinor(product.min_amount_minor, "INR")} to ${formatMinor(product.max_amount_minor, "INR")}`}
      >
        {(control) => (
          <input
            {...control}
            type="range"
            min={product.min_amount_minor}
            max={product.max_amount_minor}
            step={AMOUNT_STEP_MINOR}
            value={amountMinor}
            aria-valuetext={formatMinor(amountMinor, "INR")}
            onChange={(e) => {
              setAmountMinor(Number.parseInt(e.target.value, 10));
              setReceipt(null);
            }}
          />
        )}
      </Field>
      <p className="loan-value" aria-live="polite">
        {formatMinor(amountMinor, "INR")}
      </p>
      <Field label="Tenure">
        {(control) => (
          <input
            {...control}
            type="range"
            min={0}
            max={product.tenures_months.length - 1}
            step={1}
            value={tenureIndex}
            aria-valuetext={`${String(tenureMonths)} months`}
            onChange={(e) => {
              setTenureIndex(Number.parseInt(e.target.value, 10));
              setReceipt(null);
            }}
          />
        )}
      </Field>
      <p className="loan-value" aria-live="polite">
        {tenureMonths} months
      </p>
      <EmiSummary terms={settled} />
      {eligibility.isPending && <LoadingState label="Checking your eligibility..." />}
      {eligibility.isError && (
        <ErrorState
          message={errorMessage(eligibility.error, "Could not check your eligibility.")}
          onRetry={() => {
            void eligibility.refetch();
          }}
        />
      )}
      {eligibility.data && <EligibilityCard product={product} eligibility={eligibility.data} />}
      <Field label="Pay into and repay from">
        {(control) => (
          <select
            {...control}
            value={accountId}
            onChange={(e) => {
              setAccountId(e.target.value);
            }}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {accountLabel(a)}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Button
        disabled={!canApply}
        onClick={() => {
          setApplyError(null);
          setReviewing(true);
        }}
      >
        Apply for this loan
      </Button>
      {receipt && <Receipt loan={receipt} account={account} />}
      <ConfirmDialog
        open={reviewing}
        title="Check and confirm your loan"
        items={[
          { label: "Product", value: product.name },
          { label: "Amount", value: formatMinor(settled.amountMinor, "INR") },
          { label: "Tenure", value: `${String(settled.tenureMonths)} months` },
          { label: "Interest rate", value: formatRate(product.annual_rate_bps) },
          { label: "Monthly EMI", value: <EmiFigure terms={settled} /> },
          { label: "Pay into and repay from", value: account ? accountLabel(account) : "" },
        ]}
        confirmLabel="Confirm and receive the money"
        busy={applying}
        error={applyError}
        onConfirm={() => void confirm()}
        onCancel={() => {
          setReviewing(false);
          setApplyError(null);
        }}
      />
    </>
  );
}

/** The EMI the server computed for these terms (already cached by the calculator). */
function EmiFigure({ terms }: { terms: Terms }) {
  const emi = useQuery({
    queryKey: ["loan-emi", terms],
    queryFn: () => api.calculateEmi(terms.productId, terms.amountMinor, terms.tenureMonths),
  });
  return <>{emi.data ? formatMinor(emi.data.emi_minor, "INR") : "Calculating..."}</>;
}

function ApplySection() {
  const products = useQuery({ queryKey: ["loan-products"], queryFn: api.loanProducts });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const failed = products.isError ? products.error : accounts.isError ? accounts.error : null;
  if (failed) {
    return (
      <ErrorState
        message={errorMessage(failed, "Could not load the loan products or your accounts.")}
        onRetry={() => {
          void products.refetch();
          void accounts.refetch();
        }}
      />
    );
  }
  const catalogue = products.data;
  const own = accounts.data;
  if (catalogue === undefined || own === undefined) {
    return <LoadingState label="Loading loan products..." />;
  }
  const usable = own.filter((a) => a.status === "ACTIVE" && a.currency === "INR");
  if (usable.length === 0) {
    return <EmptyState message="Open an account first to apply for a loan." />;
  }
  if (catalogue.length === 0) {
    return <EmptyState message="No loan products are available right now." />;
  }
  return <Calculator products={catalogue} accounts={usable} />;
}

/** TASK-051: loan calculator, eligibility pre-check, application with disbursal receipt, and the
 * customer's active loans with amortisation progress (API: TASK-045/046/047, ADR-0024). */
export function Loans() {
  return (
    <div className="page">
      <PageHeader
        title="Loans"
        description="Check what a loan would cost, whether you are eligible, and track your repayments."
      />
      <section aria-labelledby="active-loans-title">
        <h2 id="active-loans-title">Your loans</h2>
        <ActiveLoans />
      </section>
      <section aria-labelledby="apply-title">
        <h2 id="apply-title">Apply for a loan</h2>
        <ApplySection />
      </section>
    </div>
  );
}
