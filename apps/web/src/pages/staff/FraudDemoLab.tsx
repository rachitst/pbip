import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { errorMessage } from "../../lib/errors";
import { transferStatus } from "../../lib/status";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { Field } from "../../ui/Field";
import { PageHeader } from "../../ui/PageHeader";
import { ErrorState, LoadingState } from "../../ui/States";
import { StatusBadge } from "../../ui/StatusBadge";
import {
  DECISION_LABEL,
  SOURCE_LABEL,
  canReviewFraud,
  contributionLabel,
  parseReasons,
  plainMessage,
  riskTone,
} from "./fraudFormat";
import { LabError, SCENARIOS, isRunnable, runScenario, useDemoLabStatus } from "./fraudLab";
import type { LabStep, LabTransfer, Scenario, ScenarioCode, StepState } from "./fraudLab";

const STEP_BADGE: Record<StepState, { label: string; tone: string }> = {
  running: { label: "Running", tone: "info" },
  done: { label: "Done", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

/** TASK-081: staff-only lab that drives the real API with demo customers (see fraudLab.ts). */
export function FraudDemoLab() {
  const me = useCurrentUser();
  const allowed = canReviewFraud(me);
  const status = useDemoLabStatus(allowed && me.mfa_verified_session);

  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Fraud demo lab" />
        <Alert tone="error">
          Access restricted: the fraud demo lab needs the Fraud Analyst or Administrator role.
        </Alert>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Fraud demo lab"
        description="Run fraud scenarios through the real transfer API as a demo customer, then see what the fraud engine decided and why."
      />
      {status.isPending && <LoadingState label="Checking whether the lab may run here..." />}
      {status.isError && (
        <ErrorState
          message={errorMessage(status.error, "Could not check whether the lab may run here.")}
          onRetry={() => {
            void status.refetch();
          }}
        />
      )}
      {status.data?.enabled === false && (
        <Alert tone="warning">
          The fraud demo lab is switched off in this environment. It only runs where the server is
          not in production (APP_ENV), because it moves money as a customer.
        </Alert>
      )}
      {status.data?.enabled === true && <Lab />}
    </div>
  );
}

function Lab() {
  const [customerEmail, setCustomerEmail] = useState("alice@demo.pbip");
  const [customerPassword, setCustomerPassword] = useState("");
  const [senderEmail, setSenderEmail] = useState("bob@demo.pbip");
  const [senderPassword, setSenderPassword] = useState("");
  const [steps, setSteps] = useState<LabStep[]>([]);
  const [active, setActive] = useState<ScenarioCode | null>(null);

  const run = useMutation({
    mutationFn: (code: ScenarioCode) =>
      runScenario({
        code,
        customer: { email: customerEmail.trim(), password: customerPassword },
        sender: { email: senderEmail.trim(), password: senderPassword || customerPassword },
        onProgress: setSteps,
      }),
  });
  const ready = customerEmail.trim() !== "" && customerPassword !== "";
  const activeScenario = SCENARIOS.find((s) => s.code === active);

  const start = (code: ScenarioCode) => {
    setActive(code);
    setSteps([]);
    run.mutate(code);
  };

  return (
    <>
      <Alert tone="info">
        Every step is a real API call: transfers go through limits, idempotency, the ledger and the
        fraud engine, and money really moves between the demo accounts. Held transfers wait in the{" "}
        <Link to="/staff/fraud">fraud alert queue</Link> for a decision.
      </Alert>

      <section className="card fraud-panel" aria-labelledby="lab-customers">
        <h2 id="lab-customers">Demo customers</h2>
        <p className="muted">
          The lab signs in as these seeded customers (no authenticator). Passwords stay in this page
          and are sent only to the sign-in endpoint.
        </p>
        <form
          className="lab-form"
          onSubmit={(event) => {
            event.preventDefault();
          }}
        >
          <Field label="Customer email" hint="The victim (F1-F5) or the mule (F6)">
            {(control) => (
              <input
                {...control}
                type="email"
                autoComplete="off"
                value={customerEmail}
                disabled={run.isPending}
                onChange={(event) => {
                  setCustomerEmail(event.target.value);
                }}
              />
            )}
          </Field>
          <Field label="Customer password" hint="The demo password from scripts/seed_dev.py">
            {(control) => (
              <input
                {...control}
                type="password"
                autoComplete="off"
                value={customerPassword}
                disabled={run.isPending}
                onChange={(event) => {
                  setCustomerPassword(event.target.value);
                }}
              />
            )}
          </Field>
          <Field label="Sender email (F6)" hint="Pays the mule before the money is forwarded">
            {(control) => (
              <input
                {...control}
                type="email"
                autoComplete="off"
                value={senderEmail}
                disabled={run.isPending}
                onChange={(event) => {
                  setSenderEmail(event.target.value);
                }}
              />
            )}
          </Field>
          <Field label="Sender password (F6)" hint="Leave empty to use the customer password">
            {(control) => (
              <input
                {...control}
                type="password"
                autoComplete="off"
                value={senderPassword}
                disabled={run.isPending}
                onChange={(event) => {
                  setSenderPassword(event.target.value);
                }}
              />
            )}
          </Field>
        </form>
      </section>

      {active !== null && (
        <section className="card fraud-panel" aria-labelledby="lab-run" aria-live="polite">
          <h2 id="lab-run">
            {activeScenario ? `${activeScenario.code} ${activeScenario.title}` : active}:{" "}
            {run.isPending ? "running" : run.isError ? "stopped" : "finished"}
          </h2>
          <ol className="lab-steps" aria-label="Steps">
            {steps.map((step) => (
              <li key={step.id}>
                <span className={`badge badge--${STEP_BADGE[step.state].tone}`}>
                  {STEP_BADGE[step.state].label}
                </span>{" "}
                {step.label}
                {step.detail && <span className="field__error"> {step.detail}</span>}
              </li>
            ))}
          </ol>
          {run.isError && (
            <Alert tone="error">
              {run.error instanceof LabError
                ? run.error.message
                : errorMessage(run.error, "The scenario stopped because a request failed.")}
            </Alert>
          )}
          {run.data?.transfers.map((t) => (
            <TransferOutcome key={t.transfer.id} result={t} />
          ))}
        </section>
      )}

      <section aria-labelledby="lab-scenarios">
        <h2 id="lab-scenarios">Scenarios</h2>
        <ul className="lab-grid">
          {SCENARIOS.map((scenario) => (
            <ScenarioCard
              key={scenario.code}
              scenario={scenario}
              busy={run.isPending}
              running={run.isPending && active === scenario.code}
              ready={ready}
              onRun={start}
            />
          ))}
        </ul>
      </section>
    </>
  );
}

interface CardProps {
  scenario: Scenario;
  busy: boolean;
  running: boolean;
  ready: boolean;
  onRun: (code: ScenarioCode) => void;
}

function ScenarioCard({ scenario, busy, running, ready, onRun }: CardProps) {
  const titleId = `lab-${scenario.code}`;
  return (
    <li className="card fraud-panel lab-card" aria-labelledby={titleId}>
      <h3 id={titleId}>
        <span className="badge badge--info">{scenario.code}</span> {scenario.title}
      </h3>
      <p>{scenario.summary}</p>
      {scenario.sequence.length > 0 && (
        <ol className="lab-sequence">
          {scenario.sequence.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
      )}
      <p className="text-sm muted">
        <strong>Engine signals:</strong> {scenario.signals}
      </p>
      {isRunnable(scenario.code) ? (
        <div className="lab-card__action">
          <Button
            variant="danger"
            size="sm"
            loading={running}
            disabled={busy || !ready}
            aria-label={`Run ${scenario.code} ${scenario.title}`}
            onClick={() => {
              onRun(scenario.code);
            }}
          >
            {running ? "Running..." : "Run scenario"}
          </Button>
          {!ready && <span className="text-sm muted">Enter the customer password first.</span>}
        </div>
      ) : (
        <p className="text-sm">
          <strong>Not runnable here.</strong> {scenario.notRunnable}
        </p>
      )}
    </li>
  );
}

function TransferOutcome({ result }: { result: LabTransfer }) {
  const { transfer, alert } = result;
  const reasons = alert ? parseReasons(alert.reasons) : [];
  return (
    <article className="lab-outcome" aria-label={result.label}>
      <h3>{result.label}</h3>
      <dl className="details">
        <dt>Customer sees</dt>
        <dd>
          <StatusBadge status={transferStatus(transfer.status)} /> {transfer.message}
        </dd>
        <dt>Amount</dt>
        <dd>
          <Amount minor={transfer.amount_minor} currency={transfer.currency} />
        </dd>
      </dl>
      {alert ? (
        <>
          <p className="lab-outcome__verdict">
            <span className={`badge badge--${riskTone(alert.risk_score)}`}>
              Risk {alert.risk_score}/100
            </span>{" "}
            <span className="badge badge--warning">
              {DECISION_LABEL[alert.decision] ?? alert.decision}
            </span>{" "}
            <span className="text-sm muted">Model {alert.model_version}</span>{" "}
            <Link to={`/staff/fraud/${alert.id}`}>Open the alert</Link>
          </p>
          {reasons.length > 0 ? (
            <ul className="reason-list" aria-label="Reasons">
              {reasons.map((r) => (
                <li key={`${r.source}-${r.code}`}>
                  <div className="reason__head">
                    <span className="badge badge--neutral">{SOURCE_LABEL[r.source]}</span>
                    <span>{plainMessage(r)}</span>
                    {r.contribution !== null && (
                      <span className="text-sm muted">{contributionLabel(r)}</span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">The alert carries no reasons.</p>
          )}
        </>
      ) : (
        <p className="muted">
          No alert: the engine did not hold this transfer, so it never reached the analyst queue.
        </p>
      )}
    </article>
  );
}
