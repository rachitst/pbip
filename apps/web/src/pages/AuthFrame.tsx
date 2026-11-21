import type { ReactNode } from "react";
import { Logo, TAGLINE } from "../brand/Logo";
import { ThemeToggle } from "../shell/theme";
import { PageHeader } from "../ui/PageHeader";
import "./Auth.css";

/** The id the landing page's "Sign in" link jumps to (the form card in its hero). */
export const AUTH_CARD_ID = "sign-in";

/**
 * The frame around the sign-in and registration forms. On its own (a deep link, signed out) it is
 * the whole page: logo, one centred card with the page's h1. Embedded in the landing page's hero
 * it is a card section with an h2, because the landing page owns the h1 and the document title.
 * Presentation only: the form inside is the caller's, unchanged.
 */
export function AuthFrame({
  title,
  embedded = false,
  children,
}: {
  title: string;
  embedded?: boolean;
  children: ReactNode;
}) {
  if (embedded) {
    return (
      <section id={AUTH_CARD_ID} className="auth-card" aria-labelledby="auth-card-title">
        <h2 id="auth-card-title" className="auth-card__title" tabIndex={-1}>
          {title}
        </h2>
        {children}
      </section>
    );
  }
  return (
    <div className="auth-page">
      <header className="auth-page__top">
        <Logo />
        <ThemeToggle />
      </header>
      <main className="auth-page__main">
        <div className="auth-card">
          <PageHeader title={title} description={TAGLINE} />
          {children}
        </div>
      </main>
      <p className="auth-page__note">
        A simulated bank built for a student capstone project. No real money moves.
      </p>
    </div>
  );
}
