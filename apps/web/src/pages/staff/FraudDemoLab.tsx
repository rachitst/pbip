import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../api/client";
import { useCurrentUser } from "../../shell/MeContext";
import { Alert } from "../../ui/Alert";
import { Amount } from "../../ui/Amount";
import { Button } from "../../ui/Button";
import { PageHeader } from "../../ui/PageHeader";
import { StatusBadge } from "../../ui/StatusBadge";
import { transactionStatus } from "../../lib/status";
import {
  DECISION_LABEL,
  SOURCE_LABEL,
  canReviewFraud,
  contributionLabel,
  plainMessage,
  riskTone,
} from "./fraudFormat";
import { SCENARIOS, executeAttackScenario, isProductionEnv } from "./fraudLabConfig";
import type { AttackResult, AttackStep, ScenarioCode } from "./fraudLabConfig";

export function FraudDemoLab() {
  const me = useCurrentUser();
  const allowed = canReviewFraud(me);
  const isProd = isProductionEnv();

  const [victimEmail, setVictimEmail] = useState("alice@demo.pbip");
  const [victimPassword, setVictimPassword] = useState("Demo-Password-2026!");
  const [activeScenario, setActiveScenario] = useState<ScenarioCode | null>(null);
  const [isExecuting, setIsExecuting] = useState(false);
  const [steps, setSteps] = useState<AttackStep[]>([]);
  const [lastResult, setLastResult] = useState<AttackResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!allowed) {
    return (
      <div className="page">
        <PageHeader title="Fraud attack simulator" />
        <Alert tone="error">
          Access restricted: the attack simulator requires the Fraud Analyst or Administrator role.
        </Alert>
      </div>
    );
  }

  const handleExecute = async (scenarioCode: ScenarioCode) => {
    if (isProd) return;
    setIsExecuting(true);
    setActiveScenario(scenarioCode);
    setSteps([]);
    setError(null);

    try {
      const res = await executeAttackScenario({
        scenarioCode,
        victimEmail,
        victimPassword,
        onStepUpdate: (s) => {
          setSteps([...s]);
        },
        staffAlertsQuery: () => api.fraudAlerts("OPEN"),
        staffAlertDetailQuery: (id) => api.fraudAlert(id),
      });
      setLastResult(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Attack simulation failed");
    } finally {
      setIsExecuting(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        title="Fraud attack simulator"
        description="Interactive demonstration lab allowing staff to trigger realistic fraud scenarios (F1-F6) and inspect detection responses."
      />

      {isProd && (
        <Alert tone="warning">
          Interactive attack simulation is disabled in production environments (APP_ENV=production).
          This tool is restricted to local development and test staging environments.
        </Alert>
      )}

      <section className="card fraud-panel" aria-labelledby="lab-config-heading">
        <h2 id="lab-config-heading" className="text-lg">
          Simulation configuration
        </h2>
        <p className="muted">
          Attacks are dispatched against the target customer account using non-credentialed API
          requests, preserving staff analyst session state.
        </p>
        <div className="grid grid--2" style={{ marginTop: "var(--space-2)" }}>
          <div className="field">
            <label className="field__label" htmlFor="victim-email-input">
              Target customer email
            </label>
            <input
              id="victim-email-input"
              type="email"
              className="input"
              value={victimEmail}
              disabled={isExecuting || isProd}
              onChange={(e) => {
                setVictimEmail(e.target.value);
              }}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="victim-password-input">
              Target password
            </label>
            <input
              id="victim-password-input"
              type="password"
              className="input"
              value={victimPassword}
              disabled={isExecuting || isProd}
              onChange={(e) => {
                setVictimPassword(e.target.value);
              }}
            />
          </div>
        </div>
      </section>

      {/* Live execution feedback section */}
      {(isExecuting || steps.length > 0 || lastResult || error) && (
        <section className="card fraud-panel" aria-labelledby="feedback-heading" aria-live="polite">
          <h2 id="feedback-heading" className="text-lg">
            {isExecuting ? "Attack execution in progress..." : "Simulation outcome"}
          </h2>

          {steps.length > 0 && (
            <div style={{ marginBottom: "var(--space-3)" }}>
              <ol className="reason-list" aria-label="Execution steps">
                {steps.map((st) => (
                  <li key={st.step} className="reason">
                    <div className="reason__head">
                      <span
                        className={`badge badge--${
                          st.status === "completed"
                            ? "success"
                            : st.status === "running"
                              ? "info"
                              : st.status === "failed"
                                ? "danger"
                                : "neutral"
                        }`}
                      >
                        {st.status === "completed"
                          ? "Done"
                          : st.status === "running"
                            ? "In progress"
                            : st.status === "failed"
                              ? "Failed"
                              : "Queued"}
                      </span>
                      <span className="reason__text">{st.label}</span>
                    </div>
                    {st.error && (
                      <p className="muted" style={{ color: "var(--danger)" }}>
                        {st.error}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {error && <Alert tone="error">{error}</Alert>}

          {lastResult && !isExecuting && (
            <div className="fraud-decision" data-testid="attack-outcome-card">
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: "var(--space-2)",
                  marginBottom: "var(--space-3)",
                }}
              >
                <div>
                  <span className="badge badge--neutral">{lastResult.scenarioCode}</span>
                  <strong style={{ marginLeft: "var(--space-2)" }}>
                    {SCENARIOS.find((s) => s.code === lastResult.scenarioCode)?.title ??
                      lastResult.scenarioCode}
                  </strong>
                </div>
                <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                  {lastResult.transferStatus && (
                    <StatusBadge status={transactionStatus(lastResult.transferStatus)} />
                  )}
                  {lastResult.riskScore !== undefined && (
                    <span className={`badge badge--${riskTone(lastResult.riskScore)}`}>
                      Risk score: {lastResult.riskScore}/100
                    </span>
                  )}
                  {lastResult.decision && (
                    <span className="badge badge--warning">
                      Decision: {DECISION_LABEL[lastResult.decision] ?? lastResult.decision}
                    </span>
                  )}
                </div>
              </div>

              <dl className="details">
                {lastResult.transferId && (
                  <>
                    <dt>Transfer ID</dt>
                    <dd>{lastResult.transferId}</dd>
                  </>
                )}
                {lastResult.amountMinor !== undefined && (
                  <>
                    <dt>Amount</dt>
                    <dd>
                      <Amount minor={lastResult.amountMinor} currency="INR" />
                    </dd>
                  </>
                )}
                {lastResult.alertId && (
                  <>
                    <dt>Staff alert</dt>
                    <dd>
                      <Link
                        to={`/staff/fraud/${lastResult.alertId}`}
                        className="btn btn--sm btn--primary"
                      >
                        View alert in staff queue &rarr;
                      </Link>
                    </dd>
                  </>
                )}
              </dl>

              <h3 className="text-md" style={{ marginTop: "var(--space-3)" }}>
                Fraud engine reasons
              </h3>
              {lastResult.reasons.length > 0 ? (
                <ul className="reason-list" aria-label="Engine reasons">
                  {lastResult.reasons.map((r) => (
                    <li key={`${r.source}-${r.code}`} className="reason">
                      <div className="reason__head">
                        <span
                          className={`badge badge--${r.source === "RULE" ? "info" : "neutral"}`}
                        >
                          {SOURCE_LABEL[r.source]}
                        </span>
                        <span className="reason__text">{plainMessage(r)}</span>
                      </div>
                      {r.contribution !== null && (
                        <div className="reason__bar">
                          <span className="muted">{contributionLabel(r)}</span>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No engine reasons were returned.</p>
              )}
            </div>
          )}
        </section>
      )}

      {/* Scenario cards grid */}
      <section aria-labelledby="scenarios-heading">
        <h2 id="scenarios-heading" className="text-lg" style={{ marginBottom: "var(--space-3)" }}>
          Attack scenarios (F1-F6)
        </h2>
        <div
          className="fraud-grid"
          style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}
        >
          {SCENARIOS.map((scenario) => {
            const isRunning = isExecuting && activeScenario === scenario.code;
            return (
              <div
                key={scenario.code}
                className="card fraud-panel"
                data-testid={`scenario-card-${scenario.code}`}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  justifyContent: "space-between",
                }}
              >
                <div>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "baseline",
                      marginBottom: "var(--space-2)",
                    }}
                  >
                    <span className="badge badge--info">{scenario.code}</span>
                    <span className="muted text-xs">{scenario.subtitle}</span>
                  </div>
                  <h3 className="text-md" style={{ marginTop: 0 }}>
                    {scenario.title}
                  </h3>
                  <p className="text-sm" style={{ marginBottom: "var(--space-2)" }}>
                    {scenario.description}
                  </p>

                  <div style={{ margin: "var(--space-2) 0" }}>
                    <div className="text-xs muted">
                      <strong>Signature:</strong> {scenario.attackSignature}
                    </div>
                    <div className="text-xs muted" style={{ marginTop: "var(--space-1)" }}>
                      <strong>Detection:</strong> {scenario.detectionVector}
                    </div>
                  </div>

                  <dl className="details" style={{ marginTop: "var(--space-2)" }}>
                    {Object.entries(scenario.parameters).map(([key, value]) => (
                      <div
                        key={key}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          fontSize: "var(--text-xs)",
                        }}
                      >
                        <dt className="muted">{key}</dt>
                        <dd>{value}</dd>
                      </div>
                    ))}
                  </dl>
                </div>

                <div style={{ marginTop: "var(--space-4)" }}>
                  <Button
                    variant="danger"
                    size="sm"
                    loading={isRunning}
                    disabled={isExecuting || isProd}
                    aria-label={`Execute Attack for ${scenario.title}`}
                    onClick={() => {
                      void handleExecute(scenario.code);
                    }}
                  >
                    Execute Attack
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
