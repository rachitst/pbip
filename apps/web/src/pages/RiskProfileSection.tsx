import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api/client";
import type { RiskProfileOut, RiskQuestionnaire } from "../api/client";
import { errorMessage } from "../lib/errors";
import { formatDate } from "../lib/format";
import { Alert } from "../ui/Alert";
import { Button } from "../ui/Button";
import { EmptyState, ErrorState, LoadingState } from "../ui/States";
import "./ChoiceGroup.css";

type QuestionKey = keyof RiskQuestionnaire;
type Answers = { [K in QuestionKey]?: RiskQuestionnaire[K] };

interface Question<K extends QuestionKey> {
  key: K;
  text: string;
  /** Every enumerated answer the API accepts, in order from most to least cautious. */
  options: Record<RiskQuestionnaire[K], string>;
}

type AnyQuestion = { [K in QuestionKey]: Question<K> }[QuestionKey];

/** The wording of the server's RTQ-1 questionnaire (apps/api .../customers/risk.py). */
const QUESTIONS: AnyQuestion[] = [
  {
    key: "investment_horizon",
    text: "How long do you expect to keep money invested?",
    options: {
      UNDER_1_YEAR: "Less than a year",
      ONE_TO_5_YEARS: "One to five years",
      OVER_5_YEARS: "More than five years",
    },
  },
  {
    key: "loss_reaction",
    text: "If your investments fell 20% in a year, what would you do?",
    options: {
      SELL_EVERYTHING: "Sell everything to stop further losses",
      WAIT_AND_HOLD: "Wait and hold",
      INVEST_MORE: "Invest more while prices are low",
    },
  },
  {
    key: "investment_goal",
    text: "What matters most for your investments?",
    options: {
      PROTECT_SAVINGS: "Protecting what I have",
      STEADY_GROWTH: "Steady growth with some ups and downs",
      MAXIMUM_GROWTH: "The highest growth, accepting large swings",
    },
  },
  {
    key: "investment_experience",
    text: "How much experience do you have with market-linked investments?",
    options: {
      NONE: "None",
      SOME: "Some",
      EXTENSIVE: "A lot",
    },
  },
  {
    key: "emergency_fund",
    text: "Without touching investments, how many months of essential spending could you cover?",
    options: {
      UNDER_3_MONTHS: "Less than 3 months",
      THREE_TO_6_MONTHS: "3 to 6 months",
      OVER_6_MONTHS: "More than 6 months",
    },
  },
];

const BAND_TEXT: Record<NonNullable<RiskProfileOut["band"]>, string> = {
  LOW: "Low: you prefer to protect your money over chasing growth.",
  MODERATE: "Moderate: you accept some ups and downs for steady growth.",
  HIGH: "High: you accept large swings for the chance of higher growth.",
};

function complete(answers: Answers): RiskQuestionnaire | null {
  const {
    investment_horizon,
    loss_reaction,
    investment_goal,
    investment_experience,
    emergency_fund,
  } = answers;
  if (
    investment_horizon === undefined ||
    loss_reaction === undefined ||
    investment_goal === undefined ||
    investment_experience === undefined ||
    emergency_fund === undefined
  ) {
    return null;
  }
  return {
    investment_horizon,
    loss_reaction,
    investment_goal,
    investment_experience,
    emergency_fund,
  };
}

export function RiskProfileSection() {
  const profile = useQuery({ queryKey: ["risk-profile"], queryFn: api.riskProfile });
  return (
    <section aria-labelledby="risk-profile-heading">
      <h2 id="risk-profile-heading">Risk profile</h2>
      <p className="muted">
        Five questions about how you feel about investment risk. Your answers give a band we use
        when suggesting investments; answer again whenever your situation changes.
      </p>
      {profile.isPending && <LoadingState label="Loading your risk profile..." />}
      {profile.isError && (
        <ErrorState
          message="Could not load your risk profile."
          onRetry={() => {
            void profile.refetch();
          }}
        />
      )}
      {profile.data && <RiskProfileBody current={profile.data} />}
    </section>
  );
}

function RiskProfileBody({ current }: { current: RiskProfileOut }) {
  const queryClient = useQueryClient();
  const [answers, setAnswers] = useState<Answers>(current.answers ?? {});
  const [localError, setLocalError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: api.saveRiskProfile,
    onSuccess: (saved) => {
      queryClient.setQueryData(["risk-profile"], saved);
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    const filled = complete(answers);
    if (filled === null) {
      setLocalError("Answer all five questions to see your risk band.");
      return;
    }
    setLocalError(null);
    save.mutate(filled);
  }

  const message =
    localError ??
    (save.isError ? errorMessage(save.error, "Could not save your answers. Try again.") : null);
  return (
    <>
      {current.band === null || current.assessed_at === null ? (
        <EmptyState message="You have not answered the questionnaire yet." />
      ) : (
        <dl className="details">
          <dt>Your risk band</dt>
          <dd>{BAND_TEXT[current.band]}</dd>
          <dt>Last assessed</dt>
          <dd>{formatDate(current.assessed_at)}</dd>
        </dl>
      )}
      <form onSubmit={submit} noValidate>
        {QUESTIONS.map((question) => (
          <fieldset key={question.key} className="choice-group">
            <legend>{question.text}</legend>
            {Object.entries(question.options).map(([value, label]) => (
              <label key={value}>
                <input
                  type="radio"
                  name={question.key}
                  value={value}
                  checked={answers[question.key] === value}
                  onChange={() => {
                    setAnswers((previous) => ({ ...previous, [question.key]: value }));
                  }}
                />{" "}
                {label}
              </label>
            ))}
          </fieldset>
        ))}
        {message && <Alert tone="error">{message}</Alert>}
        {save.isSuccess && !message && (
          <Alert tone="success">Saved. Your risk band is {save.data.band?.toLowerCase()}.</Alert>
        )}
        <Button type="submit" loading={save.isPending}>
          {save.isPending ? "Saving..." : "Save my answers"}
        </Button>
      </form>
    </>
  );
}
