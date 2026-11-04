import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";
import { api } from "../api/client";
import type {
  InsuranceApplicationOut,
  InsuranceProductOut,
  InsuranceQuoteOut,
  InsuranceQuoteRequest,
} from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import { formatMinor, parseToMinor } from "../lib/money";
import { PRODUCT_PARAM } from "../lib/recommendations";
import type { StatusView } from "../lib/status";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { Field } from "../ui/Field";
import { MoneyInput } from "../ui/MoneyInput";
import { PageHeader } from "../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";

type AgeBand = InsuranceQuoteRequest["age_band"];
const AGE_BANDS: AgeBand[] = ["18-25", "26-35", "36-50", "51-59", "60+"];

export const STATUS: Record<InsuranceApplicationOut["status"], StatusView> = {
  APPROVED_SIMULATED: { label: "Approved (simulated)", tone: "success" },
  SUBMITTED: { label: "Referred", tone: "warning" },
  DECLINED: { label: "Declined", tone: "danger" },
};

export const QUOTE_NOTE =
  "Simulated quote (rule IQ-1): the plan's indicative premium scaled by cover, age band and term. " +
  "It is not an insurer's price, PBIP does no underwriting, no policy is issued and nothing is charged.";
export const REFERRED_NOTE =
  "A referred application stays referred: PBIP has no insurer partner to decide it.";

function QuoteResult({ quote }: { quote: InsuranceQuoteOut }) {
  return (
    <section className="loan-card" aria-label="Quote">
      <p>
        <StatusBadge status={STATUS[quote.outcome]} />
      </p>
      <dl className="details">
        <div className="details__row">
          <dt>Simulated annual premium</dt>
          <dd>
            {quote.annual_premium_minor === null
              ? "None: not eligible"
              : formatMinor(quote.annual_premium_minor, "INR")}
          </dd>
        </div>
        <div className="details__row">
          <dt>Why</dt>
          <dd>{quote.reason}</dd>
        </div>
      </dl>
    </section>
  );
}

function Applications() {
  const applications = useQuery({
    queryKey: ["insurance-applications"],
    queryFn: api.insuranceApplications,
  });
  if (applications.isPending) return <LoadingState label="Loading your applications..." />;
  if (applications.isError) {
    return (
      <ErrorState
        message={errorMessage(applications.error, "Could not load your applications.")}
        onRetry={() => {
          void applications.refetch();
        }}
      />
    );
  }
  if (applications.data.length === 0) {
    return <EmptyState message="You have not applied for any insurance yet." />;
  }
  return (
    <div className="entries">
      <table>
        <caption className="sr-only">My insurance applications, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Plan</th>
            <th scope="col" className="right">
              Cover
            </th>
            <th scope="col" className="right">
              Simulated premium
            </th>
            <th scope="col">Status</th>
            <th scope="col">Applied</th>
          </tr>
        </thead>
        <tbody>
          {applications.data.map((a) => (
            <tr key={a.id}>
              <td>{a.product_name}</td>
              <td className="right">{formatMinor(a.cover_minor, "INR")}</td>
              <td className="right">
                {a.annual_premium_minor === null ? "-" : formatMinor(a.annual_premium_minor, "INR")}
              </td>
              <td>
                <StatusBadge status={STATUS[a.status]} />
                <br />
                <span className="muted">{a.decision_reason}</span>
              </td>
              <td>{formatDate(a.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QuoteForm({ products }: { products: InsuranceProductOut[] }) {
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const initial = products.find((p) => p.id === params.get(PRODUCT_PARAM)) ?? products[0];
  const [productId, setProductId] = useState(initial?.id ?? "");
  const product = products.find((p) => p.id === productId) ?? initial;
  const [coverText, setCoverText] = useState("");
  const [term, setTerm] = useState(product?.indicative_premium.reference_term_years ?? 1);
  const [band, setBand] = useState<AgeBand | "">("");
  const [consent, setConsent] = useState(false);
  const [quote, setQuote] = useState<InsuranceQuoteOut | null>(null);
  const [applied, setApplied] = useState<InsuranceApplicationOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // ONE key per confirmed application: a retried submit never records a second one.
  const [intentKey, setIntentKey] = useState(() => crypto.randomUUID());
  if (product === undefined) return <EmptyState message="No insurance plans are available." />;

  const cover = parseToMinor(coverText);
  const coverOk =
    cover !== null && cover >= product.cover_min_minor && cover <= product.cover_max_minor;
  const coverError =
    coverText !== "" && !coverOk
      ? `Enter cover from ${formatMinor(product.cover_min_minor, "INR")} to ${formatMinor(product.cover_max_minor, "INR")}.`
      : null;
  const ready = coverOk && band !== "" && consent;

  function request(): InsuranceQuoteRequest | null {
    if (!ready) return null;
    return {
      product_id: product?.id ?? "",
      cover_minor: cover,
      term_years: term,
      age_band: band,
      age_band_consent: true,
    };
  }

  function changed() {
    setQuote(null);
    setError(null);
    setIntentKey(crypto.randomUUID());
  }

  async function run(apply: boolean) {
    const body = request();
    if (body === null) return;
    setBusy(true);
    setError(null);
    try {
      if (apply) {
        setApplied(await api.applyForInsurance(body, intentKey));
        setIntentKey(crypto.randomUUID());
        await queryClient.invalidateQueries({ queryKey: ["insurance-applications"] });
      } else {
        setQuote(await api.quoteInsurance(body));
      }
    } catch (caught) {
      setError(errorMessage(caught, "The quote could not be calculated. Try again."));
    } finally {
      setBusy(false);
    }
  }

  if (applied) {
    return (
      <section className="loan-card" aria-label="Application received">
        <h2>Application recorded</h2>
        <p>
          <StatusBadge status={STATUS[applied.status]} /> {applied.decision_reason}
        </p>
        {applied.status === "SUBMITTED" && <p className="muted">{REFERRED_NOTE}</p>}
        <Button
          variant="secondary"
          onClick={() => {
            setApplied(null);
            changed();
          }}
        >
          Get another quote
        </Button>
      </section>
    );
  }

  return (
    <section className="loan-card" aria-label="Insurance quote">
      <Field label="Plan">
        {(control) => (
          <select
            {...control}
            value={product.id}
            onChange={(e) => {
              const next = products.find((p) => p.id === e.target.value);
              setProductId(e.target.value);
              setTerm(next?.indicative_premium.reference_term_years ?? 1);
              changed();
            }}
          >
            {products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field
        label="Cover"
        hint={`${formatMinor(product.cover_min_minor, "INR")} to ${formatMinor(product.cover_max_minor, "INR")}`}
        error={coverError}
      >
        {(control) => (
          <MoneyInput
            {...control}
            currency="INR"
            value={coverText}
            onValueChange={(value) => {
              setCoverText(value);
              changed();
            }}
          />
        )}
      </Field>
      <Field label="Policy term">
        {(control) => (
          <select
            {...control}
            value={term}
            onChange={(e) => {
              setTerm(Number.parseInt(e.target.value, 10));
              changed();
            }}
          >
            {product.policy_terms_years.map((years) => (
              <option key={years} value={years}>
                {years === 1 ? "1 year (renewed yearly)" : `${String(years)} years`}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="Your age band" hint="Used only for this quote and application">
        {(control) => (
          <select
            {...control}
            value={band}
            onChange={(e) => {
              const value = AGE_BANDS.find((b) => b === e.target.value);
              setBand(value ?? "");
              changed();
            }}
          >
            <option value="">Choose your age band</option>
            {AGE_BANDS.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        )}
      </Field>
      <label>
        <input
          type="checkbox"
          checked={consent}
          onChange={(e) => {
            setConsent(e.target.checked);
            changed();
          }}
        />{" "}
        I agree to my age band being used for this quote and application
      </label>
      <p className="muted">{QUOTE_NOTE}</p>
      {error && <Alert tone="error">{error}</Alert>}
      {quote && <QuoteResult quote={quote} />}
      <div className="dialog__actions">
        <Button
          variant="secondary"
          disabled={!ready}
          loading={busy && quote === null}
          onClick={() => void run(false)}
        >
          Get a quote
        </Button>
        <Button
          disabled={quote === null || quote.outcome === "DECLINED"}
          loading={busy && quote !== null}
          onClick={() => void run(true)}
        >
          Apply
        </Button>
      </div>
    </section>
  );
}

/** ADR-0058 section 8: a simulated insurance quote from a declared age band, and an application
 * with a status by documented rules. No underwriting, no policy and no premium debit. */
export default function InsuranceQuote() {
  const products = useQuery({ queryKey: ["insurance-products"], queryFn: api.insuranceProducts });
  return (
    <div className="page">
      <PageHeader
        title="Insurance quote"
        description="A simulated quote for term life or health cover, and your applications."
      />
      {products.isPending && <LoadingState label="Loading insurance plans..." />}
      {products.isError && (
        <ErrorState
          message={errorMessage(products.error, "Could not load the insurance plans.")}
          onRetry={() => {
            void products.refetch();
          }}
        />
      )}
      {products.data && <QuoteForm products={products.data} />}
      <section aria-labelledby="applications-title">
        <h2 id="applications-title">Your applications</h2>
        <Applications />
      </section>
    </div>
  );
}
