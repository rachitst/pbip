import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { AnalyticsConsentOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import { ANALYTICS_CONSENT_KEY } from "../telemetry/PageViews";
import { browserAllowsAnalytics, clickstream } from "../telemetry/tracker";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { ErrorState, LoadingState } from "../ui/States";

/** ADR-0057: the customer's analytics consent. Off unless turned on; turning it off erases the
 * activity already recorded and the behavioural profile built from it. */
export function AnalyticsConsentSection() {
  const consent = useQuery({ queryKey: ANALYTICS_CONSENT_KEY, queryFn: api.analyticsConsent });
  return (
    <section aria-labelledby="analytics-consent-heading">
      <h2 id="analytics-consent-heading">App usage analytics</h2>
      <p className="muted">
        Optional. If you turn this on, we record how you use this app (which pages you open, how
        long a visit lasts, which products you look at) and build a behavioural profile from it,
        together with your spending pattern, to understand which products interest you. We never
        record what you type, amounts, account numbers or names. Your relationship manager can see
        the profile. It is off unless you turn it on.
      </p>
      {consent.isPending && <LoadingState label="Loading your analytics setting..." />}
      {consent.isError && (
        <ErrorState
          message="Could not load your analytics setting."
          onRetry={() => {
            void consent.refetch();
          }}
        />
      )}
      {consent.data && <ConsentBody current={consent.data} />}
    </section>
  );
}

function ConsentBody({ current }: { current: AnalyticsConsentOut }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const change = useMutation({
    mutationFn: api.setAnalyticsConsent,
    onSuccess: (saved) => {
      setConfirming(false);
      queryClient.setQueryData(ANALYTICS_CONSENT_KEY, saved);
      clickstream.setConsent(saved.granted && browserAllowsAnalytics());
    },
  });
  const granted = current.granted;
  const decided = current.updated_at ? ` since ${formatDate(current.updated_at)}` : "";
  return (
    <div>
      <dl className="details">
        <dt>Usage analytics</dt>
        <dd>{granted ? `On${decided}` : `Off${decided}`}</dd>
      </dl>
      {granted && !browserAllowsAnalytics() && (
        <Alert tone="info">
          Your browser asks sites not to share your data (Global Privacy Control), so this browser
          records nothing even though the setting is on.
        </Alert>
      )}
      {change.isError && !confirming && (
        <Alert tone="error">{errorMessage(change.error, "Could not update the setting.")}</Alert>
      )}
      <Button
        variant={granted ? "secondary" : "primary"}
        loading={change.isPending && !confirming}
        onClick={() => {
          if (granted) setConfirming(true);
          else change.mutate(true);
        }}
      >
        {granted ? "Turn off and erase" : "Turn on usage analytics"}
      </Button>
      <p className="muted">Policy version {current.current_policy_version}</p>
      <ConfirmDialog
        open={confirming}
        title="Turn off usage analytics?"
        items={[
          {
            label: "Effect",
            value: "We stop recording at once and erase the activity and profile we hold.",
          },
        ]}
        confirmLabel="Turn off and erase"
        tone="danger"
        busy={change.isPending}
        error={change.isError ? errorMessage(change.error, "Could not update the setting.") : null}
        onConfirm={() => {
          change.mutate(false);
        }}
        onCancel={() => {
          setConfirming(false);
        }}
      />
    </div>
  );
}
