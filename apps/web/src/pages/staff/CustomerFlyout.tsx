import { useEffect, useState } from "react";
import type { SyntheticEvent } from "react";
import type { CustomerInsightItem } from "../../api/client";
import { formatDateTime } from "../../lib/format";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { StatusBadge } from "../../ui/StatusBadge";
import {
  churnRiskTone,
  deriveHealthLabel,
  formatProductName,
  formatSegment,
  loadCustomerActions,
  saveCustomerAction,
} from "./rmInsightsFormat";
import type { RMActionItem } from "./rmInsightsFormat";

interface CustomerFlyoutProps {
  customer: CustomerInsightItem;
  onClose: () => void;
  currentUserEmail: string;
}

const ACTION_TYPES = [
  { value: "CALL", label: "Phone Call" },
  { value: "MEETING", label: "In-Person / Video Meeting" },
  { value: "EMAIL", label: "Email Outreach" },
  { value: "NOTE", label: "General Note" },
] as const;

const OUTCOMES = [
  "Connected - Interested in Offer",
  "Connected - Not Interested",
  "Follow-up Call Scheduled",
  "Left Voicemail / Message",
  "Sent Product Brochure / Details",
  "General Account Review",
] as const;

export function CustomerFlyout({ customer, onClose, currentUserEmail }: CustomerFlyoutProps) {
  const [actions, setActions] = useState<RMActionItem[]>(() =>
    loadCustomerActions(customer.customer_id),
  );
  const [actionType, setActionType] = useState<"CALL" | "MEETING" | "EMAIL" | "NOTE">("CALL");
  const [outcome, setOutcome] = useState<string>(OUTCOMES[0]);
  const [notes, setNotes] = useState("");
  const [feedback, setFeedback] = useState<string | null>(null);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  const health = deriveHealthLabel(customer);
  const propensityPercent =
    customer.top_product_score !== null && customer.top_product_score !== undefined
      ? `${Math.round(customer.top_product_score * 100)}%`
      : "—";

  function handleLogAction(e: SyntheticEvent) {
    e.preventDefault();
    const trimmedNotes = notes.trim();
    if (!trimmedNotes) {
      setFeedback("Please enter notes for this outreach action.");
      return;
    }

    const saved = saveCustomerAction({
      customer_id: customer.customer_id,
      action_type: actionType,
      outcome,
      notes: trimmedNotes,
      logged_by: currentUserEmail,
    });

    setActions((prev) => [saved, ...prev]);
    setNotes("");
    setFeedback("Action recorded successfully.");
  }

  return (
    <div className="flyout-overlay" onClick={onClose} data-testid="flyout-overlay">
      <div
        className="flyout-panel"
        role="dialog"
        aria-modal="true"
        aria-label={`Customer details: ${customer.full_name}`}
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <header className="flyout-header">
          <div>
            <div className="flyout-title-row">
              <h2 className="flyout-title">{customer.full_name}</h2>
              {customer.high_value && <span className="badge badge--success">High Value</span>}
            </div>
            <p className="flyout-subtitle" data-testid="customer-id">
              ID: {customer.customer_id} &bull; {formatSegment(customer.segment)}
            </p>
          </div>
          <button
            type="button"
            className="btn btn--sm btn--secondary"
            aria-label="Close details"
            onClick={onClose}
          >
            &times;
          </button>
        </header>

        <div className="flyout-body">
          {/* Key Indicators Card */}
          <section className="card flyout-card" aria-label="Key financial indicators">
            <h3 className="section-title">Indicators & Health Overview</h3>
            <div className="indicator-grid">
              <div className="indicator-cell">
                <span className="indicator-label">Total Spendable Balance</span>
                <span className="indicator-value">
                  <Amount minor={customer.total_balance_minor} currency="INR" />
                </span>
              </div>
              <div className="indicator-cell">
                <span className="indicator-label">Churn Risk & Probability</span>
                <div className="indicator-badge-row">
                  <StatusBadge
                    status={{
                      label: `${customer.churn_risk_level} (${Math.round(customer.churn_risk_score * 100)}%)`,
                      tone: churnRiskTone(customer.churn_risk_level),
                    }}
                  />
                </div>
              </div>
              <div className="indicator-cell">
                <span className="indicator-label">30-Day Balance Decline</span>
                <span className="indicator-value">
                  {(customer.balance_decline_rate * 100).toFixed(1)}%
                </span>
              </div>
              <div className="indicator-cell">
                <span className="indicator-label">Recent Activity Gap</span>
                <span className="indicator-value">
                  {customer.inactivity_days === 0
                    ? "Active today"
                    : `${customer.inactivity_days} days inactive`}
                </span>
              </div>
            </div>

            <div className="indicator-summary">
              <span className="indicator-label">Financial Health Status</span>
              <div className="indicator-badge-row">
                <StatusBadge status={{ label: health.label, tone: health.tone }} />
              </div>
              <p className="muted small-text">{health.description}</p>
            </div>
          </section>

          {/* Propensity & Recommendations */}
          <section className="card flyout-card" aria-label="Product recommendations">
            <h3 className="section-title">Recommended Product & Propensity</h3>
            <div className="recommendation-box">
              <div className="rec-info">
                <span className="rec-name">
                  {formatProductName(customer.top_recommended_product)}
                </span>
                <span className="rec-score">Propensity score: {propensityPercent}</span>
              </div>
              <p className="muted small-text">
                Eligible based on transaction trends, category velocity, and customer segment.
              </p>
            </div>
          </section>

          {/* Action Logger */}
          <section className="card flyout-card" aria-label="Action logger">
            <h3 className="section-title">Action Logger</h3>
            <p className="muted small-text">
              Record calls, meetings, or client advisory notes for this customer.
            </p>

            {feedback && (
              <div
                className={`notice ${feedback.includes("successfully") ? "notice--success" : "notice--error"}`}
                role="status"
              >
                {feedback}
              </div>
            )}

            <form onSubmit={handleLogAction} className="action-form">
              <div className="form-row">
                <label htmlFor="action-type" className="form-label">
                  Action Type
                </label>
                <select
                  id="action-type"
                  className="input select"
                  value={actionType}
                  onChange={(e) => {
                    setActionType(e.target.value as "CALL" | "MEETING" | "EMAIL" | "NOTE");
                  }}
                >
                  {ACTION_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="form-row">
                <label htmlFor="action-outcome" className="form-label">
                  Outcome
                </label>
                <select
                  id="action-outcome"
                  className="input select"
                  value={outcome}
                  onChange={(e) => {
                    setOutcome(e.target.value);
                  }}
                >
                  {OUTCOMES.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              </div>

              <div className="form-row">
                <label htmlFor="action-notes" className="form-label">
                  Meeting / Call Notes
                </label>
                <textarea
                  id="action-notes"
                  className="input textarea"
                  rows={3}
                  value={notes}
                  placeholder="Record customer reaction, loan willingness, next steps..."
                  onChange={(e) => {
                    setNotes(e.target.value);
                  }}
                />
              </div>

              <div className="form-actions">
                <Button type="submit">Log Action</Button>
              </div>
            </form>

            {/* Action History */}
            <div className="action-history" aria-label="Logged outreach history">
              <h4 className="history-heading">Outreach History ({actions.length})</h4>
              {actions.length === 0 ? (
                <p className="muted small-text">No outreach recorded yet for this customer.</p>
              ) : (
                <ul className="action-list">
                  {actions.map((act) => (
                    <li key={act.id} className="action-item">
                      <div className="action-item-header">
                        <span className="badge badge--neutral">{act.action_type}</span>
                        <span className="action-outcome-label">{act.outcome}</span>
                        <span className="action-timestamp">{formatDateTime(act.created_at)}</span>
                      </div>
                      <p className="action-notes-body">{act.notes}</p>
                      <span className="action-logged-by">By {act.logged_by}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
