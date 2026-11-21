import { useLayoutEffect } from "react";
import type { ReactNode } from "react";
import {
  ArrowRight,
  BookOpen,
  ChartLine,
  Code,
  KeyRound,
  Landmark,
  LayoutDashboard,
  ListChecks,
  Lock,
  MessageCircle,
  ScrollText,
  SearchCheck,
  ShieldCheck,
  Wallet,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import accountsShot from "../assets/landing/accounts.webp";
import assistantShot from "../assets/landing/assistant.webp";
import fraudAlertShot from "../assets/landing/fraud-alert.webp";
import insightsShot from "../assets/landing/insights.webp";
import staffDashboardShot from "../assets/landing/staff-dashboard.webp";
import { ASSISTANT_NAME, BRAND, Logo, TAGLINE } from "../brand/Logo";
import { ThemeToggle } from "../shell/theme";
import { Button } from "../ui/Button";
import { AUTH_CARD_ID } from "./AuthFrame";
import "./Landing.css";

const REPO_URL = "https://github.com/rachitst/pbip";
const DOCS_URL = `${REPO_URL}/tree/main/docs`;
const TEST_REPORT_URL = `${REPO_URL}/blob/main/docs/testing/test-strategy-and-report.md`;
const MODEL_REPORT_URL = `${REPO_URL}/blob/main/docs/ml/model-evaluation-report-v1.md`;

interface Feature {
  icon: LucideIcon;
  title: string;
  text: string;
}

const FEATURES: Feature[] = [
  {
    icon: Wallet,
    title: "Accounts and payments",
    text: "Savings and current accounts, payees, bill payments, scheduled transfers and statements, on a double-entry ledger.",
  },
  {
    icon: ShieldCheck,
    title: "Fraud protection with explained alerts",
    text: "Every transfer is risk-scored before money moves. Each alert carries a score, a decision and plain-language reasons for the analyst who reviews it.",
  },
  {
    icon: MessageCircle,
    title: ASSISTANT_NAME,
    text: "Ask about your balance, your spending or a loan. The assistant reads only your own accounts and cannot move money.",
  },
  {
    icon: ChartLine,
    title: "Insights and recommendations",
    text: "Spending by category, a financial health score, a balance forecast and product suggestions that say why they were made.",
  },
  {
    icon: Lock,
    title: "Security",
    text: "Two-step sign-in with an authenticator app, recovery codes, a list of your devices and a password change that signs out every session.",
  },
  {
    icon: LayoutDashboard,
    title: "Staff dashboards",
    text: "Fraud alerts and a fraud dashboard, compliance and risk, an audit log, customer insights for relationship managers and administration.",
  },
];

interface Showcase {
  id: string;
  eyebrow: string;
  title: string;
  text: string;
  points: string[];
  image: string;
  alt: string;
}

const SHOWCASES: Showcase[] = [
  {
    id: "showcase-fraud",
    eyebrow: "Fraud protection",
    title: "Alerts that explain themselves",
    text: "A transfer that looks wrong is held or asks for a second step before it posts. The analyst sees why, not just a number.",
    points: [
      "A risk score from 0 to 100 and a decision: allow, step up, review or block",
      "Human-readable reasons, each marked as a rule, the model or an anomaly",
      "If the model is unavailable, the rules still decide: it never falls back to allowing everything",
    ],
    image: fraudAlertShot,
    alt: "A fraud alert in the staff workspace: the risk score, the decision and the list of reasons for one held transfer (demo data).",
  },
  {
    id: "showcase-assistant",
    eyebrow: ASSISTANT_NAME,
    title: "Answers about your own money",
    text: "The assistant answers in plain words and links to the page that has the detail.",
    points: [
      "Read-only: it looks things up through approved tools and cannot move money",
      "It only ever sees the signed-in customer's own data",
      "Every tool call is recorded in the audit trail",
    ],
    image: assistantShot,
    alt: "The Ask Finora assistant drawer open beside the accounts page, with suggested questions (demo data).",
  },
  {
    id: "showcase-insights",
    eyebrow: "Insights",
    title: "See where your money goes",
    text: "Spending by category, month-over-month trends, a financial health score and a forecast of your balance.",
    points: [
      "Charts drawn from your own transactions",
      "Recommendations that state the reason they were shown",
      "Usage analytics only with your consent, which you can change on your profile",
    ],
    image: insightsShot,
    alt: "The spending and financial health page: a health score and spending charts by category (demo data).",
  },
  {
    id: "showcase-staff",
    eyebrow: "Staff dashboards",
    title: "Tools for the people who run the bank",
    text: "Fraud analysts, compliance officers, relationship managers and administrators each get their own workspace, behind a second sign-in step.",
    points: [
      "Headline figures, each with its definition visible",
      "An audit log that shows if an entry was tampered with",
      "Roles are checked on every request, not only in the menu",
    ],
    image: staffDashboardShot,
    alt: "The fraud dashboard in the staff workspace: headline figures and charts of recent alerts (demo data).",
  },
];

const SAFETY: Feature[] = [
  {
    icon: KeyRound,
    title: "Two-step sign-in",
    text: "A code from an authenticator app after your password, with single-use recovery codes. Codes cannot be replayed, and repeated wrong passwords lock the account for a while.",
  },
  {
    icon: SearchCheck,
    title: "Every transfer checked first",
    text: "Rules and a machine-learning model score a transfer before the ledger posts it. Risky ones wait for a second step or an analyst.",
  },
  {
    icon: Landmark,
    title: "A ledger that cannot be quietly edited",
    text: "Money is stored in whole paise. Every posting balances, entries are never changed, and corrections are new, reversing entries.",
  },
  {
    icon: ListChecks,
    title: "Only your own data",
    text: "Every request is checked against the signed-in person. Someone else's account and an account that does not exist look the same: not found.",
  },
  {
    icon: ScrollText,
    title: "An audit trail",
    text: "Security-relevant changes are recorded in the same database transaction as the change itself.",
  },
  {
    icon: ShieldCheck,
    title: "Sessions that end safely",
    text: "Sign-in tokens rotate, a reused token signs the whole session out, and changing your password signs out every device.",
  },
];

function FeatureCard({ feature }: { feature: Feature }) {
  const Glyph = feature.icon;
  return (
    <li className="landing-card">
      <span className="landing-card__icon">
        <Glyph size={20} strokeWidth={2} aria-hidden="true" focusable="false" />
      </span>
      <h3 className="landing-card__title">{feature.title}</h3>
      <p className="landing-card__text">{feature.text}</p>
    </li>
  );
}

function Screenshot({ src, alt }: { src: string; alt: string }) {
  return (
    <figure className="landing-shot">
      <img src={src} alt={alt} width={1200} height={750} />
    </figure>
  );
}

/**
 * The front door for signed-out visitors at "/" only (every other signed-out URL shows the plain
 * sign-in page, so a deep link survives sign-in). The real sign-in form, or the registration form
 * after "Open an account", sits in the hero: ``form`` is that form, unchanged.
 */
export function Landing({
  form,
  onOpenAccount,
  onSignIn,
}: {
  form: ReactNode;
  onOpenAccount: () => void;
  onSignIn: () => void;
}) {
  useLayoutEffect(() => {
    document.title = `${BRAND} - ${TAGLINE}`;
  }, []);

  function focusForm() {
    // The card's heading takes focus (and scrolls into view) once the swapped form has rendered.
    window.requestAnimationFrame(() => {
      document.getElementById("auth-card-title")?.focus();
    });
  }

  return (
    <div className="landing">
      <header className="landing-top">
        <div className="landing-top__inner">
          <Logo />
          <nav className="landing-top__nav" aria-label="Landing page">
            <a href="#features">Features</a>
            <a href="#safety">Safety</a>
            <a href={DOCS_URL}>Docs</a>
          </nav>
          <ThemeToggle />
        </div>
      </header>

      <main className="landing-main">
        <section className="landing-hero" aria-labelledby="landing-title">
          <div className="landing-hero__inner">
            <div className="landing-hero__copy">
              <p className="landing-eyebrow">{TAGLINE}</p>
              <h1 id="landing-title" className="landing-hero__title">
                Everyday banking, with fraud checks that explain themselves.
              </h1>
              <p className="landing-hero__lead">
                {BRAND} brings your accounts, payments, insights and an assistant into one place,
                and checks every transfer for fraud before your money moves.
              </p>
              <div className="landing-hero__actions">
                <a
                  className="btn btn--primary"
                  href={`#${AUTH_CARD_ID}`}
                  onClick={(event) => {
                    event.preventDefault();
                    onSignIn();
                    focusForm();
                  }}
                >
                  Sign in
                  <ArrowRight size={16} aria-hidden="true" focusable="false" />
                </a>
                <Button
                  variant="secondary"
                  onClick={() => {
                    onOpenAccount();
                    focusForm();
                  }}
                >
                  Open an account
                </Button>
              </div>
              <p className="landing-hero__note">
                A simulated bank built for a student capstone project, with demo data only. No real
                money moves.
              </p>
            </div>
            <div className="landing-hero__form">{form}</div>
          </div>
          <div className="landing-hero__shot">
            <Screenshot
              src={accountsShot}
              alt="The Finora accounts page: total balance and a list of accounts with their balances (demo data)."
            />
          </div>
        </section>

        <section id="features" className="landing-section" aria-labelledby="features-title">
          <div className="landing-section__inner">
            <p className="landing-eyebrow">Features</p>
            <h2 id="features-title" className="landing-section__title">
              Everything a personal bank account needs
            </h2>
            <ul className="landing-grid">
              {FEATURES.map((feature) => (
                <FeatureCard key={feature.title} feature={feature} />
              ))}
            </ul>
          </div>
        </section>

        {SHOWCASES.map((showcase, index) => (
          <section
            key={showcase.id}
            className="landing-section landing-showcase"
            data-flip={index % 2 === 1 ? "true" : undefined}
            aria-labelledby={showcase.id}
          >
            <div className="landing-section__inner landing-showcase__inner">
              <div className="landing-showcase__copy">
                <p className="landing-eyebrow">{showcase.eyebrow}</p>
                <h2 id={showcase.id} className="landing-section__title">
                  {showcase.title}
                </h2>
                <p>{showcase.text}</p>
                <ul className="landing-checks">
                  {showcase.points.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              </div>
              <Screenshot src={showcase.image} alt={showcase.alt} />
            </div>
          </section>
        ))}

        <section
          id="safety"
          className="landing-section landing-safety"
          aria-labelledby="safety-title"
        >
          <div className="landing-section__inner">
            <p className="landing-eyebrow">Safety</p>
            <h2 id="safety-title" className="landing-section__title">
              How we keep you safe
            </h2>
            <ul className="landing-grid">
              {SAFETY.map((feature) => (
                <FeatureCard key={feature.title} feature={feature} />
              ))}
            </ul>
          </div>
        </section>

        <section className="landing-section" aria-labelledby="evidence-title">
          <div className="landing-section__inner">
            <p className="landing-eyebrow">Built in the open</p>
            <h2 id="evidence-title" className="landing-section__title">
              Tested, measured and written down
            </h2>
            <ul className="landing-facts">
              <li className="stat-tile">
                <span className="stat-tile__label">Automated tests passed</span>
                <span className="stat-tile__value">2,345</span>
                <span className="stat-tile__note">
                  Unit, integration and end-to-end, in the CI run of 3 Nov 2026.{" "}
                  <a href={TEST_REPORT_URL}>Test report, section 2</a>
                </span>
              </li>
              <li className="stat-tile">
                <span className="stat-tile__label">Fraud recall at the review threshold</span>
                <span className="stat-tile__value">0.923</span>
                <span className="stat-tile__note">
                  The served model on its synthetic test period (PR-AUC 0.945).{" "}
                  <a href={MODEL_REPORT_URL}>Model report, section 10</a>
                </span>
              </li>
              <li className="stat-tile">
                <span className="stat-tile__label">Fraud scoring latency, p95</span>
                <span className="stat-tile__value">20.2 ms</span>
                <span className="stat-tile__note">
                  One request at a time, measured by the model promotion gate.{" "}
                  <a href={MODEL_REPORT_URL}>Model report, section 10</a>
                </span>
              </li>
            </ul>
            <p className="landing-small">
              The fraud figures come from synthetic data generated for this project. They show the
              pipeline works; they are not an estimate of performance on real bank traffic.
            </p>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <div className="landing-footer__inner">
          <div>
            <Logo size={28} />
            <p className="landing-small">
              {TAGLINE}. A simulated bank built for a student capstone project. No real money,
              accounts or customers.
            </p>
          </div>
          <ul className="landing-footer__links">
            <li>
              <a href={REPO_URL}>
                <Code size={16} aria-hidden="true" focusable="false" />
                Source code on GitHub
              </a>
            </li>
            <li>
              <a href={DOCS_URL}>
                <BookOpen size={16} aria-hidden="true" focusable="false" />
                Project documentation
              </a>
            </li>
          </ul>
        </div>
      </footer>
    </div>
  );
}
