import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api/client";
import type { ProductRecommendationItem, RecommendedLoanOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatMinor } from "../lib/money";
import {
  SIMULATED_ORDER_NOTE,
  productTypeLabel,
  recommendationAction,
} from "../lib/recommendations";
import { Button } from "../ui/Button";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import { StatusBadge } from "../ui/StatusBadge";
import { formatRate } from "./Loans";

interface ProductRecommendationsProps {
  /** Called when the customer follows a product's action, before navigating. The clickstream
   * `product_clicked` event (TASK-112) attaches here; the carousel itself records nothing. */
  onProductAction?: (item: ProductRecommendationItem) => void;
}

/** UC-12 / US-07: the products GET /recommendations returns for the signed-in customer, each with
 * the API's own reasons. A horizontally scrollable list (no autoplay) that wraps into a grid on
 * wide screens. Nothing here is computed in the browser: titles, descriptions and reasons are
 * shown exactly as the API sends them, as text. */
export function ProductRecommendations({ onProductAction }: ProductRecommendationsProps) {
  const headingId = useId();
  const recommendations = useQuery({
    queryKey: ["recommendations"],
    queryFn: api.recommendations,
  });
  const items = recommendations.data?.recommendations ?? [];

  return (
    <section className="recommendations" aria-labelledby={headingId}>
      <h2 id={headingId}>Recommended for you</h2>
      {recommendations.isPending && <LoadingState label="Loading recommendations..." />}
      {recommendations.isError && (
        <ErrorState
          message={errorMessage(recommendations.error, "Could not load recommendations.")}
          onRetry={() => {
            void recommendations.refetch();
          }}
        />
      )}
      {recommendations.isSuccess && items.length === 0 && (
        <EmptyState message="No product recommendations for you right now." />
      )}
      {items.length > 0 && (
        <ul className="recommendations__list">
          {items.map((item) => (
            <RecommendationCard
              key={item.product_id}
              item={item}
              onProductAction={onProductAction}
            />
          ))}
        </ul>
      )}
      {items.length > 0 && recommendations.data?.score_from_simulated_data === true && (
        // ADR-0049: the order came from a model trained on simulated uptake; say so plainly.
        <p className="muted">{SIMULATED_ORDER_NOTE}</p>
      )}
    </section>
  );
}

interface RecommendationCardProps {
  item: ProductRecommendationItem;
  onProductAction: ((item: ProductRecommendationItem) => void) | undefined;
}

function RecommendationCard({ item, onProductAction }: RecommendationCardProps) {
  const navigate = useNavigate();
  const [showReasons, setShowReasons] = useState(false);
  const titleId = useId();
  const reasonsId = useId();
  const action = recommendationAction(item);

  return (
    <li className="recommendation">
      <p className="recommendation__type muted">{productTypeLabel(item.product_type)}</p>
      <h3 id={titleId} className="recommendation__title">
        {item.title}
      </h3>
      <p className="recommendation__description">{item.description}</p>
      {item.loan && <LoanFigures loan={item.loan} />}
      <Button
        variant="link"
        size="sm"
        aria-expanded={showReasons}
        aria-controls={reasonsId}
        onClick={() => {
          setShowReasons(!showReasons);
        }}
      >
        Why this was recommended for you
      </Button>
      <ul id={reasonsId} className="recommendation__reasons" hidden={!showReasons}>
        {item.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <div className="recommendation__action">
        {action.kind === "navigate" ? (
          <Button
            variant="secondary"
            size="sm"
            // "Open account" also names the open-account form's button; the product title keeps
            // each card's action unique for screen readers and for tests that query by name.
            aria-label={`${action.label}: ${item.title}`}
            onClick={() => {
              onProductAction?.(item);
              void navigate(action.to);
            }}
          >
            {action.label}
          </Button>
        ) : (
          <p className="muted">{action.note}</p>
        )}
      </div>
    </li>
  );
}

/** UC-06 (ADR-0056): a recommended catalogue loan's own assessment, exactly as the API sent it.
 * The engine recommends only loans the eligibility rules allow, so the badge is always eligible. */
function LoanFigures({ loan }: { loan: RecommendedLoanOut }) {
  return (
    <>
      <StatusBadge status={{ label: "Eligible", tone: "success" }} />
      <dl className="details">
        <div className="details__row">
          <dt>You could borrow</dt>
          <dd>
            {formatMinor(loan.suggested_min_amount_minor, "INR")} to{" "}
            {formatMinor(loan.suggested_max_amount_minor, "INR")}
          </dd>
        </div>
        <div className="details__row">
          <dt>EMI at the top of the range</dt>
          <dd>
            {formatMinor(loan.emi_at_max_minor, "INR")} over {loan.tenure_months} months
          </dd>
        </div>
        <div className="details__row">
          <dt>Interest rate</dt>
          <dd>from {formatRate(loan.annual_rate_bps)}</dd>
        </div>
      </dl>
    </>
  );
}
