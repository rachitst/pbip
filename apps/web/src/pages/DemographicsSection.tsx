import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { DemographicsOut } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { Field } from "../ui/Field";
import { ErrorState, LoadingState } from "../ui/States";

const QUERY_KEY = ["demographics"];

/** A calendar date (YYYY-MM-DD) shown as written: formatted in UTC so no time zone can move it
 * to the previous or next day. */
const calendarDate = new Intl.DateTimeFormat("en-IN", { dateStyle: "long", timeZone: "UTC" });
function formatBirthDate(isoDate: string): string {
  return calendarDate.format(new Date(`${isoDate}T00:00:00Z`));
}

const BAND_TEXT: Record<DemographicsOut["age_band"], string> = {
  "18-25": "18 to 25",
  "26-35": "26 to 35",
  "36-50": "36 to 50",
  "51-59": "51 to 59",
  "60+": "60 or over",
  unknown: "Unknown",
};

export function DemographicsSection() {
  const demographics = useQuery({ queryKey: QUERY_KEY, queryFn: api.demographics });
  return (
    <section aria-labelledby="demographics-heading">
      <h2 id="demographics-heading">Date of birth and recommendations consent</h2>
      <p className="muted">
        Optional. If you give your date of birth and agree below, we use only your age band (for
        example 60 or over) to offer products you are eligible for, such as senior-citizen deposit
        rates. We never use it to price credit, and our staff and the assistant never see the date.
      </p>
      {demographics.isPending && <LoadingState label="Loading your date of birth..." />}
      {demographics.isError && (
        <ErrorState
          message="Could not load your date of birth and consent."
          onRetry={() => {
            void demographics.refetch();
          }}
        />
      )}
      {demographics.data && <DemographicsBody current={demographics.data} />}
    </section>
  );
}

function DemographicsBody({ current }: { current: DemographicsOut }) {
  return (
    <>
      <dl className="details">
        <dt>Date of birth</dt>
        <dd>{current.date_of_birth ? formatBirthDate(current.date_of_birth) : "Not provided"}</dd>
        <dt>Age band</dt>
        <dd>{BAND_TEXT[current.age_band]}</dd>
        <dt>Use for recommendations</dt>
        <dd>
          {current.recommendations_consent && current.consent_granted_at
            ? `Yes, since ${formatDate(current.consent_granted_at)}`
            : "No"}
        </dd>
      </dl>
      <DateOfBirthForm current={current} />
      <ConsentControl current={current} />
    </>
  );
}

function DateOfBirthForm({ current }: { current: DemographicsOut }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(current.date_of_birth ?? "");
  const [confirming, setConfirming] = useState(false);
  const save = useMutation({
    mutationFn: api.setDateOfBirth,
    onSuccess: (saved) => {
      queryClient.setQueryData(QUERY_KEY, saved);
    },
  });
  const remove = useMutation({
    mutationFn: api.deleteDateOfBirth,
    onSuccess: async () => {
      setConfirming(false);
      setValue("");
      await queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (value) save.mutate(value);
  }

  return (
    <form onSubmit={submit}>
      <Field label="Date of birth" hint="You must be between 18 and 120 years old.">
        {(control) => (
          <input
            {...control}
            type="date"
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
            }}
            required
          />
        )}
      </Field>
      {save.isError && (
        <Alert tone="error">{errorMessage(save.error, "Could not save your date of birth.")}</Alert>
      )}
      {save.isSuccess && <Alert tone="success">Date of birth saved.</Alert>}
      {remove.isSuccess && <Alert tone="success">Date of birth deleted.</Alert>}
      <div className="form-actions">
        <Button type="submit" loading={save.isPending} disabled={!value}>
          {save.isPending ? "Saving..." : "Save date of birth"}
        </Button>
        {current.date_of_birth && (
          <Button
            variant="danger"
            onClick={() => {
              setConfirming(true);
            }}
          >
            Delete date of birth
          </Button>
        )}
      </div>
      <ConfirmDialog
        open={confirming}
        title="Delete your date of birth?"
        items={[{ label: "Effect", value: "Recommendations stop using your age band at once." }]}
        confirmLabel="Delete"
        tone="danger"
        busy={remove.isPending}
        error={
          remove.isError ? errorMessage(remove.error, "Could not delete your date of birth.") : null
        }
        onConfirm={() => {
          remove.mutate();
        }}
        onCancel={() => {
          setConfirming(false);
        }}
      />
    </form>
  );
}

function ConsentControl({ current }: { current: DemographicsOut }) {
  const queryClient = useQueryClient();
  const change = useMutation({
    mutationFn: api.setRecommendationsConsent,
    onSuccess: (saved) => {
      queryClient.setQueryData(QUERY_KEY, saved);
    },
  });
  const granted = current.recommendations_consent;
  return (
    <div>
      <p>
        {granted
          ? "You agreed that we may use your date of birth to tailor product recommendations. You can withdraw at any time; we stop using it at once."
          : "Use my date of birth to tailor product recommendations. This is off unless you turn it on."}
      </p>
      {change.isError && (
        <Alert tone="error">{errorMessage(change.error, "Could not update your consent.")}</Alert>
      )}
      <Button
        variant={granted ? "secondary" : "primary"}
        loading={change.isPending}
        onClick={() => {
          change.mutate(!granted);
        }}
      >
        {granted ? "Withdraw consent" : "Give consent"}
      </Button>
      <p className="muted">Policy version {current.current_policy_version}</p>
    </div>
  );
}
