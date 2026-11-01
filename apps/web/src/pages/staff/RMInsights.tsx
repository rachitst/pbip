import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api/client";
import type { CustomerInsightItem } from "../../api/client";
import { errorMessage } from "../../lib/errors";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { PageHeader } from "../../ui/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import { CustomerFlyout } from "./CustomerFlyout";
import {
  canAccessRMInsights,
  churnRiskTone,
  formatProductName,
  formatSegment,
} from "./rmInsightsFormat";

const SEGMENT_OPTIONS = [
  { value: "", label: "All Segments" },
  { value: "HIGH_NET_WORTH", label: "High Net Worth" },
  { value: "SALARIED", label: "Salaried" },
  { value: "STUDENT", label: "Student" },
  { value: "RETIREE", label: "Retiree" },
  { value: "YOUNG_PROFESSIONAL", label: "Young Professional" },
] as const;

const PROPENSITY_OPTIONS = [
  { value: "0", label: "All Propensities" },
  { value: "0.5", label: ">= 50% Propensity" },
  { value: "0.7", label: ">= 70% Propensity" },
  { value: "0.85", label: ">= 85% Propensity" },
] as const;

const CHURN_OPTIONS = [
  { value: "0", label: "All Churn Levels" },
  { value: "0.4", label: "Medium & High Risk" },
  { value: "0.7", label: "High Risk Only" },
] as const;

export function RMInsights() {
  const me = useCurrentUser();
  const allowed = canAccessRMInsights(me);

  const [segmentFilter, setSegmentFilter] = useState<string>("");
  const [minPropensity, setMinPropensity] = useState<string>("0");
  const [minChurn, setMinChurn] = useState<string>("0");
  const [selectedCustomer, setSelectedCustomer] = useState<CustomerInsightItem | null>(null);

  const churnRiskNumber = parseFloat(minChurn);

  const insightsQuery = useQuery({
    queryKey: ["insights", "customers", segmentFilter, churnRiskNumber],
    queryFn: () =>
      api.customerInsights({
        segment: segmentFilter || undefined,
        min_churn_risk: churnRiskNumber > 0 ? churnRiskNumber : undefined,
        limit: 25,
      }),
    enabled: allowed,
  });

  // Client-side filtering by loan propensity score & product matching
  const rawItems = insightsQuery.data?.items ?? [];
  const minScore = parseFloat(minPropensity);
  const filteredItems =
    minScore > 0 ? rawItems.filter((item) => (item.top_product_score ?? 0) >= minScore) : rawItems;

  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Relationship Manager Insights" />
        <Alert tone="error">
          Access restricted: Customer leads and behavioral insights require the Relationship Manager
          or Administrator role.
        </Alert>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Relationship Manager Insights"
        description="Prioritized customer leads by churn risk and product propensity. Review behavioral indicators and track client outreach."
      />

      {/* Filter Controls */}
      <section className="filter-panel" aria-label="Lead filters">
        <div className="filters-grid">
          <div className="filter-item">
            <label htmlFor="filter-segment" className="form-label">
              Customer Segment
            </label>
            <select
              id="filter-segment"
              className="input select"
              value={segmentFilter}
              onChange={(e) => {
                setSegmentFilter(e.target.value);
              }}
            >
              {SEGMENT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div className="filter-item">
            <label htmlFor="filter-propensity" className="form-label">
              Loan / Product Propensity
            </label>
            <select
              id="filter-propensity"
              className="input select"
              value={minPropensity}
              onChange={(e) => {
                setMinPropensity(e.target.value);
              }}
            >
              {PROPENSITY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div className="filter-item">
            <label htmlFor="filter-churn" className="form-label">
              Churn Risk Level
            </label>
            <select
              id="filter-churn"
              className="input select"
              value={minChurn}
              onChange={(e) => {
                setMinChurn(e.target.value);
              }}
            >
              {CHURN_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </section>

      {/* Status Views */}
      {insightsQuery.isPending && <LoadingState label="Loading customer leads..." />}

      {insightsQuery.isError && (
        <ErrorState
          message={errorMessage(insightsQuery.error, "Could not load customer insights.")}
          onRetry={() => {
            void insightsQuery.refetch();
          }}
        />
      )}

      {/* Table of Leads */}
      {!insightsQuery.isPending && !insightsQuery.isError && (
        <>
          {filteredItems.length === 0 ? (
            <EmptyState message="No customer leads match the chosen criteria." />
          ) : (
            <div className="entries leads-table-container">
              <table>
                <caption className="sr-only">
                  Customer leads prioritized by churn risk and loan propensity
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Customer</th>
                    <th scope="col">Segment</th>
                    <th scope="col" className="right">
                      Spendable Balance
                    </th>
                    <th scope="col">Churn Risk</th>
                    <th scope="col">Top Recommendation</th>
                    <th scope="col">Propensity</th>
                    <th scope="col" className="right">
                      Action
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filteredItems.map((item) => {
                    const propensity =
                      item.top_product_score !== null && item.top_product_score !== undefined
                        ? `${Math.round(item.top_product_score * 100)}%`
                        : "—";

                    return (
                      <tr key={item.customer_id} className="lead-row">
                        <td>
                          <div className="lead-name-cell">
                            <span className="lead-name">{item.full_name}</span>
                            {item.high_value && (
                              <span className="badge badge--success">High Value</span>
                            )}
                          </div>
                        </td>
                        <td>{formatSegment(item.segment)}</td>
                        <td className="right">
                          <Amount minor={item.total_balance_minor} currency="INR" />
                        </td>
                        <td>
                          <StatusBadge
                            status={{
                              label: `${item.churn_risk_level} (${Math.round(item.churn_risk_score * 100)}%)`,
                              tone: churnRiskTone(item.churn_risk_level),
                            }}
                          />
                        </td>
                        <td>
                          <span className="recommended-product-name">
                            {formatProductName(item.top_recommended_product)}
                          </span>
                        </td>
                        <td>
                          <span className="propensity-score-badge">{propensity}</span>
                        </td>
                        <td className="right">
                          <Button
                            variant="secondary"
                            onClick={() => {
                              setSelectedCustomer(item);
                            }}
                            aria-label={`View details for ${item.full_name}`}
                          >
                            View Details
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {/* Detail Flyout */}
      {selectedCustomer && (
        <CustomerFlyout
          key={selectedCustomer.customer_id}
          customer={selectedCustomer}
          onClose={() => {
            setSelectedCustomer(null);
          }}
          currentUserEmail={me.email}
        />
      )}
    </div>
  );
}
