import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { api } from "../api/client";
import type {
  AccountOut,
  AllocationSlice,
  FixedDepositOut,
  PortfolioOut,
  RiskLevel,
  WealthProductOut,
} from "../api/client";
import { errorMessage } from "../lib/errors";
import { accountTypeLabel, formatDate } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import { BOOK_FIXED_DEPOSIT, BOOK_PARAM } from "../lib/recommendations";
import type { StatusView } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Amount } from "../ui/Amount";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Dialog } from "../ui/Dialog";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";
import { CHART_COLORS, TOOLTIP_STYLE } from "./insightsChart";
import { formatRate } from "./Loans";
import "./LoanCard.css";

/** Wait for typing to settle before asking the server for a maturity quote. */
export const QUOTE_DEBOUNCE_MS = 300;

const ASSET_LABEL: Record<AllocationSlice["asset_class"], string> = {
  CASH: "Cash",
  DEPOSITS: "Fixed deposits",
  FUNDS: "Funds",
};
const RISK: Record<RiskLevel, StatusView> = {
  LOW: { label: "Low risk", tone: "success" },
  MODERATE: { label: "Moderate risk", tone: "warning" },
  HIGH: { label: "High risk", tone: "danger" },
};
const PRODUCT_TYPE: Record<WealthProductOut["product_type"], string> = {
  FIXED_DEPOSIT: "Fixed deposit",
  MUTUAL_FUND: "Mutual fund",
  SOVEREIGN_GOLD_BOND: "Sovereign gold bond",
};
const DEPOSIT_STATUS: Record<FixedDepositOut["status"], StatusView> = {
  ACTIVE: { label: "Active", tone: "info" },
  MATURED: { label: "Matured", tone: "success" },
  CLOSED: { label: "Closed", tone: "neutral" },
};

/** Basis points of a whole (allocation share) to "12.34%" with integer arithmetic only. */
export function formatShareBps(bps: number): string {
  return `${String(Math.trunc(bps / 100))}.${String(bps % 100).padStart(2, "0")}%`;
}

/** The maturity countdown shown for a held deposit; the day count comes from the server. */
export function maturityCountdown(deposit: Pick<FixedDepositOut, "status" | "days_to_maturity">) {
  if (deposit.status === "CLOSED") return "Closed";
  if (deposit.status === "MATURED") return "Matured: ready to withdraw";
  if (deposit.days_to_maturity === 1) return "1 day to go";
  return `${String(deposit.days_to_maturity)} days to go`;
}

export function tenureLabel(months: number): string {
  if (months % 12 === 0) {
    const years = months / 12;
    return years === 1 ? "1 year" : `${String(years)} years`;
  }
  return `${String(months)} months`;
}

function lockInLabel(months: number): string {
  return months === 0 ? "None" : tenureLabel(months);
}

function accountLabel(account: AccountOut): string {
  return `${accountTypeLabel(account.account_type)} ${account.account_number_masked}`;
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

// ------------------------------------------------------------------ portfolio
function AllocationChart({ portfolio }: { portfolio: PortfolioOut }) {
  const slices = portfolio.allocation.map((slice, index) => ({
    name: ASSET_LABEL[slice.asset_class],
    value: slice.value_minor,
    fill: CHART_COLORS[index] ?? "var(--chart-5)",
  }));
  const summary = portfolio.allocation
    .map((s) => `${ASSET_LABEL[s.asset_class]} ${formatShareBps(s.share_bps)}`)
    .join(", ");
  return (
    <div className="insights-breakdown">
      <div
        role="region"
        aria-label="Asset allocation chart"
        className="insights-chart insights-chart--donut"
      >
        <ResponsiveContainer
          width="100%"
          height="100%"
          minWidth={0}
          initialDimension={{ width: 320, height: 260 }}
        >
          <PieChart title="Asset allocation" desc={summary}>
            <Tooltip
              {...TOOLTIP_STYLE}
              formatter={(value) => [formatMinor(Number(value), portfolio.currency), ""]}
            />
            <Pie
              rootTabIndex={-1}
              data={slices}
              dataKey="value"
              nameKey="name"
              innerRadius={60}
              outerRadius={90}
              paddingAngle={2}
              isAnimationActive={false}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>
      <ul className="insights-legend" aria-label="Asset allocation">
        {portfolio.allocation.map((slice, index) => (
          <li key={slice.asset_class} className="insights-legend__item">
            <span className="insights-legend__name">
              <span
                aria-hidden="true"
                className={`insights-swatch insights-swatch--${String(index + 1)}`}
              />
              <span className="insights-legend__label">{ASSET_LABEL[slice.asset_class]}</span>
            </span>
            <span className="insights-legend__figures">
              <strong>
                <Amount minor={slice.value_minor} currency={portfolio.currency} />
              </strong>
              <span className="badge badge--neutral insights-num">
                {formatShareBps(slice.share_bps)}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PortfolioSection() {
  const portfolio = useQuery({ queryKey: ["wealth-portfolio"], queryFn: api.portfolio });
  if (portfolio.isPending) return <LoadingState label="Loading your portfolio..." />;
  if (portfolio.isError) {
    return (
      <ErrorState
        message={errorMessage(portfolio.error, "Could not load your portfolio.")}
        onRetry={() => {
          void portfolio.refetch();
        }}
      />
    );
  }
  const p = portfolio.data;
  return (
    <>
      <section className="loan-card" aria-label="Portfolio summary">
        <dl className="details">
          <div className="details__row">
            <dt>Invested capital</dt>
            <dd>{formatMinor(p.invested_principal_minor, p.currency)}</dd>
          </div>
          <div className="details__row">
            <dt>Accrued returns</dt>
            <dd>{formatMinor(p.accrued_interest_minor, p.currency)}</dd>
          </div>
          <div className="details__row">
            <dt>Value at maturity</dt>
            <dd>{formatMinor(p.projected_maturity_value_minor, p.currency)}</dd>
          </div>
          <div className="details__row">
            <dt>Cash in your accounts</dt>
            <dd>{formatMinor(p.cash_minor, p.currency)}</dd>
          </div>
          <div className="details__row">
            <dt>Total</dt>
            <dd>{formatMinor(p.total_minor, p.currency)}</dd>
          </div>
        </dl>
        <p className="muted">
          {p.open_deposits === 1 ? "1 open deposit" : `${String(p.open_deposits)} open deposits`} ·
          figures as of {formatDate(p.as_of)}. Funds are shown at {formatMinor(0, p.currency)}: PBIP
          does not sell fund units online.
        </p>
      </section>
      {p.total_minor > 0 ? (
        <AllocationChart portfolio={p} />
      ) : (
        <EmptyState message="Nothing to allocate yet: add money to an account to get started." />
      )}
    </>
  );
}

// ------------------------------------------------------------------ holdings
function CloseDeposit({ deposit }: { deposit: FixedDepositOut }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());
  const matured = deposit.status === "MATURED";
  if (!deposit.can_close_now || deposit.payout_if_closed_today_minor === null) {
    return <span className="muted">Locked until {formatDate(deposit.lock_in_until)}</span>;
  }
  const payout = deposit.payout_if_closed_today_minor;

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api.closeDeposit(deposit.id, intentKey);
      setIntentKey(crypto.randomUUID());
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["wealth-deposits"] });
      await queryClient.invalidateQueries({ queryKey: ["wealth-portfolio"] });
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    } catch (caught) {
      setError(errorMessage(caught, "The deposit could not be closed. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button
        size="sm"
        variant={matured ? "primary" : "secondary"}
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
      >
        {matured ? "Withdraw" : "Close early"}
      </Button>
      <ConfirmDialog
        open={open}
        title={matured ? "Withdraw your matured deposit" : "Close this deposit early"}
        items={[
          { label: "Deposit", value: deposit.product_name },
          { label: "Principal", value: formatMinor(deposit.principal_minor, deposit.currency) },
          { label: "You receive today", value: formatMinor(payout, deposit.currency) },
          ...(matured
            ? []
            : [
                {
                  label: "At maturity instead",
                  value: `${formatMinor(deposit.maturity_amount_minor, deposit.currency)} on ${formatDate(deposit.maturity_date)}`,
                },
              ]),
        ]}
        confirmLabel={matured ? "Withdraw to my account" : "Close early and withdraw"}
        tone={matured ? "primary" : "danger"}
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        onCancel={() => {
          setOpen(false);
          setError(null);
        }}
      />
    </>
  );
}

function DepositsSection() {
  const deposits = useQuery({ queryKey: ["wealth-deposits"], queryFn: api.fixedDeposits });
  if (deposits.isPending) return <LoadingState label="Loading your fixed deposits..." />;
  if (deposits.isError) {
    return (
      <ErrorState
        message={errorMessage(deposits.error, "Could not load your fixed deposits.")}
        onRetry={() => {
          void deposits.refetch();
        }}
      />
    );
  }
  const held = deposits.data.filter((d) => d.status !== "CLOSED");
  if (held.length === 0) return <EmptyState message="You have no active fixed deposits." />;
  return (
    <div className="entries">
      {held.some((d) => d.status === "MATURED") && (
        <Alert tone="info">
          A matured deposit earns no further interest: withdraw it to your account.
        </Alert>
      )}
      <table>
        <caption className="sr-only">Active fixed deposits, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Deposit</th>
            <th scope="col" className="right">
              Principal
            </th>
            <th scope="col">Rate</th>
            <th scope="col">Matures</th>
            <th scope="col" className="right">
              Payout at maturity
            </th>
            <th scope="col" className="right">
              Earned so far
            </th>
            <th scope="col">Status</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {held.map((d) => (
            <tr key={d.id}>
              <td>{d.product_name}</td>
              <td className="right">{formatMinor(d.principal_minor, d.currency)}</td>
              <td>{formatRate(d.annual_rate_bps)}</td>
              <td>
                {formatDate(d.maturity_date)}
                <br />
                <span className="muted">{maturityCountdown(d)}</span>
              </td>
              <td className="right">
                {formatMinor(d.maturity_amount_minor, d.currency)}
                <br />
                <span className="muted">
                  incl. {formatMinor(d.maturity_interest_minor, d.currency)} interest
                </span>
              </td>
              <td className="right">{formatMinor(d.accrued_interest_minor, d.currency)}</td>
              <td>
                <StatusBadge status={DEPOSIT_STATUS[d.status]} />
              </td>
              <td>
                <CloseDeposit deposit={d} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ booking
function QuotePanel({
  productId,
  amountMinor,
  tenureMonths,
}: {
  productId: string;
  amountMinor: number;
  tenureMonths: number;
}) {
  const quote = useQuery({
    queryKey: ["wealth-quote", productId, amountMinor, tenureMonths],
    queryFn: () => api.quoteDeposit(productId, amountMinor, tenureMonths),
  });
  if (quote.isPending) return <LoadingState label="Calculating your maturity amount..." />;
  if (quote.isError) {
    return (
      <Alert tone="error">{errorMessage(quote.error, "Could not calculate the maturity.")}</Alert>
    );
  }
  const q = quote.data;
  return (
    <section className="loan-card" aria-label="Maturity preview">
      <dl className="details">
        <div className="details__row">
          <dt>Interest rate</dt>
          <dd>
            {formatRate(q.annual_rate_bps)} (APY {formatShareBps(q.apy_bps)})
          </dd>
        </div>
        <div className="details__row">
          <dt>Matures on</dt>
          <dd>{formatDate(q.maturity_date)}</dd>
        </div>
        <div className="details__row">
          <dt>Interest earned</dt>
          <dd>{formatMinor(q.maturity_interest_minor, "INR")}</dd>
        </div>
        <div className="details__row">
          <dt>You receive at maturity</dt>
          <dd>{formatMinor(q.maturity_amount_minor, "INR")}</dd>
        </div>
      </dl>
    </section>
  );
}

function depositChoiceLabel(product: WealthProductOut): string {
  return product.annual_rate_bps === null
    ? product.name
    : `${product.name} · ${formatRate(product.annual_rate_bps)}`;
}

/** Shown when the dialog was opened from a "book a fixed deposit" link: the link names no
 * product, so the customer chooses among the catalogue's bookable deposits here. */
function DepositChoice({
  product,
  choices,
  onChoose,
}: {
  product: WealthProductOut;
  choices: WealthProductOut[];
  onChoose: (product: WealthProductOut) => void;
}) {
  return (
    <Field label="Fixed deposit">
      {(control) => (
        <select
          {...control}
          value={product.id}
          onChange={(e) => {
            const chosen = choices.find((c) => c.id === e.target.value);
            if (chosen) onChoose(chosen);
          }}
        >
          {choices.map((c) => (
            <option key={c.id} value={c.id}>
              {depositChoiceLabel(c)}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}

function BookingDialog({
  product,
  accounts,
  choices,
  onChoose,
  onClose,
}: {
  product: WealthProductOut;
  accounts: AccountOut[];
  /** Deposits to choose from (deep link); empty when a product card's own button opened it. */
  choices: WealthProductOut[];
  onChoose: (product: WealthProductOut) => void;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [amountText, setAmountText] = useState("");
  const [tenureMonths, setTenureMonths] = useState(product.tenures_months[0] ?? 0);
  const [accountId, setAccountId] = useState(accounts[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [booked, setBooked] = useState<FixedDepositOut | null>(null);
  // ONE key per confirmed intent: retrying the same booking reuses it (never debits twice).
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());
  // Choosing another deposit is a new intent with that product's own tenures (reset in render).
  const [productId, setProductId] = useState(product.id);
  if (productId !== product.id) {
    setProductId(product.id);
    setTenureMonths(product.tenures_months[0] ?? 0);
    setIntentKey(crypto.randomUUID());
    setError(null);
  }
  const settledText = useDebounced(amountText, QUOTE_DEBOUNCE_MS);
  const amountMinor = parseToMinor(settledText);
  const min = product.min_amount_minor ?? 0;
  const max = product.max_amount_minor ?? 0;
  const inRange = amountMinor !== null && amountMinor >= min && amountMinor <= max;
  const amountError =
    amountText !== "" && amountText === settledText && !inRange
      ? `Enter an amount from ${formatMinor(min, "INR")} to ${formatMinor(max, "INR")}.`
      : null;
  const upToDate = amountText === settledText;

  async function book() {
    if (amountMinor === null) return;
    setBusy(true);
    setError(null);
    try {
      const deposit = await api.bookDeposit(
        {
          product_id: product.id,
          amount_minor: amountMinor,
          tenure_months: tenureMonths,
          account_id: accountId,
        },
        intentKey,
      );
      setIntentKey(crypto.randomUUID());
      setBooked(deposit);
      await queryClient.invalidateQueries({ queryKey: ["wealth-deposits"] });
      await queryClient.invalidateQueries({ queryKey: ["wealth-portfolio"] });
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    } catch (caught) {
      setError(errorMessage(caught, "The deposit could not be booked. Try again."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      title={booked ? "Fixed deposit booked" : `Book: ${product.name}`}
      onClose={onClose}
    >
      {booked ? (
        <section aria-label="Booking receipt">
          <dl className="details">
            <div className="details__row">
              <dt>Principal</dt>
              <dd>{formatMinor(booked.principal_minor, booked.currency)}</dd>
            </div>
            <div className="details__row">
              <dt>Matures on</dt>
              <dd>{formatDate(booked.maturity_date)}</dd>
            </div>
            <div className="details__row">
              <dt>You receive at maturity</dt>
              <dd>{formatMinor(booked.maturity_amount_minor, booked.currency)}</dd>
            </div>
            <div className="details__row">
              <dt>Reference</dt>
              <dd>{booked.booking_transaction_id}</dd>
            </div>
          </dl>
          <div className="dialog__actions">
            <Button onClick={onClose}>Done</Button>
          </div>
        </section>
      ) : (
        <>
          {choices.length > 0 && (
            <DepositChoice product={product} choices={choices} onChoose={onChoose} />
          )}
          <Field
            label="Amount"
            hint={`${formatMinor(min, "INR")} to ${formatMinor(max, "INR")}`}
            error={amountError}
          >
            {(control) => (
              <MoneyInput
                {...control}
                currency="INR"
                value={amountText}
                onValueChange={(value) => {
                  setAmountText(value);
                  setError(null);
                }}
              />
            )}
          </Field>
          <Field label="Tenure">
            {(control) => (
              <select
                {...control}
                value={tenureMonths}
                onChange={(e) => {
                  setTenureMonths(Number.parseInt(e.target.value, 10));
                }}
              >
                {product.tenures_months.map((months) => (
                  <option key={months} value={months}>
                    {tenureLabel(months)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Pay from and receive into">
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
                    {accountLabel(a)} · {formatMinor(a.available_minor, a.currency)} available
                  </option>
                ))}
              </select>
            )}
          </Field>
          {inRange && upToDate && (
            <QuotePanel
              productId={product.id}
              amountMinor={amountMinor}
              tenureMonths={tenureMonths}
            />
          )}
          {error && <Alert tone="error">{error}</Alert>}
          <div className="dialog__actions">
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button
              onClick={() => void book()}
              loading={busy}
              disabled={!inRange || !upToDate || accountId === ""}
            >
              Confirm and book
            </Button>
          </div>
        </>
      )}
    </Dialog>
  );
}

function ProductCard({ product, onBook }: { product: WealthProductOut; onBook: () => void }) {
  const headingId = useId();
  return (
    <section className="loan-card" aria-labelledby={headingId}>
      <h3 id={headingId}>{product.name}</h3>
      <p>
        <StatusBadge status={RISK[product.risk_level]} />{" "}
        <span className="muted">{PRODUCT_TYPE[product.product_type]}</span>
      </p>
      <p>{product.description}</p>
      <dl className="details">
        <div className="details__row">
          <dt>{product.yield_basis === "FIXED_COUPON" ? "Coupon" : "Interest rate"}</dt>
          <dd>
            {product.annual_rate_bps === null
              ? "Market-linked: no stated yield"
              : formatRate(product.annual_rate_bps)}
          </dd>
        </div>
        {product.apy_bps !== null && product.yield_basis === "GUARANTEED" && (
          <div className="details__row">
            <dt>Annual yield (APY)</dt>
            <dd>{formatShareBps(product.apy_bps)}</dd>
          </div>
        )}
        <div className="details__row">
          <dt>Tenure</dt>
          <dd>
            {product.tenures_months.length === 0
              ? "Open-ended"
              : product.tenures_months.map(tenureLabel).join(", ")}
          </dd>
        </div>
        <div className="details__row">
          <dt>Minimum lock-in</dt>
          <dd>{lockInLabel(product.min_lock_in_months)}</dd>
        </div>
        {product.min_amount_minor !== null && (
          <div className="details__row">
            <dt>Minimum amount</dt>
            <dd>{formatMinor(product.min_amount_minor, "INR")}</dd>
          </div>
        )}
      </dl>
      <p className="muted">{product.notes}</p>
      {product.bookable && (
        <Button size="sm" onClick={onBook}>
          Book this deposit
        </Button>
      )}
    </section>
  );
}

function CatalogueSection() {
  const [risk, setRisk] = useState<RiskLevel | "">("");
  const [booking, setBooking] = useState<WealthProductOut | null>(null);
  const [choices, setChoices] = useState<WealthProductOut[]>([]);
  const products = useQuery({
    queryKey: ["wealth-products", risk],
    queryFn: () => api.wealthProducts(risk === "" ? undefined : risk),
  });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const usable = (accounts.data ?? []).filter((a) => a.status === "ACTIVE" && a.currency === "INR");
  // `?book=fixed-deposit` (the fixed-deposit recommendation) opens the booking dialog once the
  // unfiltered catalogue has loaded, on its first bookable deposit with the others to choose from.
  const [params] = useSearchParams();
  const requested = params.get(BOOK_PARAM) === BOOK_FIXED_DEPOSIT;
  const linked = requested && risk === "";
  const bookableDeposits = linked
    ? (products.data ?? []).filter((p) => p.bookable && p.product_type === "FIXED_DEPOSIT")
    : [];
  // Handled once per link: closing the dialog or changing the filter does not reopen it.
  const [linkHandled, setLinkHandled] = useState(false);
  if (!requested && linkHandled) setLinkHandled(false);
  const first = bookableDeposits[0];
  if (linked && !linkHandled && first !== undefined) {
    setLinkHandled(true);
    setBooking(first);
    setChoices(bookableDeposits);
  }
  return (
    <>
      <p className="muted">
        Rates are indicative and simulated for this demo bank. Market-linked funds show no yield
        because PBIP has no market data.
      </p>
      <Field label="Risk level">
        {(control) => (
          <select
            {...control}
            value={risk}
            onChange={(e) => {
              const value = e.target.value;
              setRisk(value === "LOW" || value === "MODERATE" || value === "HIGH" ? value : "");
            }}
          >
            <option value="">All</option>
            <option value="LOW">Low</option>
            <option value="MODERATE">Moderate</option>
            <option value="HIGH">High</option>
          </select>
        )}
      </Field>
      {products.isPending && <LoadingState label="Loading wealth products..." />}
      {products.isError && (
        <ErrorState
          message={errorMessage(products.error, "Could not load the wealth products.")}
          onRetry={() => {
            void products.refetch();
          }}
        />
      )}
      {products.data?.length === 0 && <EmptyState message="No products match this risk level." />}
      {linked && products.data && bookableDeposits.length === 0 && (
        <Alert tone="info">No fixed deposit can be booked online right now.</Alert>
      )}
      {products.data && products.data.length > 0 && (
        <div className="loan-list">
          {products.data.map((p) => (
            <ProductCard
              key={p.id}
              product={p}
              onBook={() => {
                setChoices([]);
                setBooking(p);
              }}
            />
          ))}
        </div>
      )}
      {booking && usable.length > 0 && (
        <BookingDialog
          product={booking}
          accounts={usable}
          choices={choices}
          onChoose={setBooking}
          onClose={() => {
            setBooking(null);
          }}
        />
      )}
      {booking && accounts.data && usable.length === 0 && (
        <Alert tone="warning">Open an active INR account first to book a deposit.</Alert>
      )}
    </>
  );
}

/** TASK-065: portfolio summary with asset allocation, active fixed deposits with a maturity
 * countdown, and the wealth catalogue with booking (API: TASK-063/064, ADR-0025). */
export function Wealth() {
  return (
    <div className="page">
      <PageHeader
        title="Wealth"
        description="Your fixed deposits, what they will pay, and products to grow your savings."
      />
      <section aria-labelledby="portfolio-title">
        <h2 id="portfolio-title">Portfolio</h2>
        <PortfolioSection />
      </section>
      <section aria-labelledby="deposits-title">
        <h2 id="deposits-title">Your fixed deposits</h2>
        <DepositsSection />
      </section>
      <section aria-labelledby="catalogue-title">
        <h2 id="catalogue-title">Products</h2>
        <p>
          <Link to="/wealth/compare">Compare products side by side</Link>, including term life and
          health insurance.
        </p>
        <CatalogueSection />
      </section>
    </div>
  );
}

export default Wealth;
