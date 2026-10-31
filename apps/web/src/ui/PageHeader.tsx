import { useLayoutEffect } from "react";
import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** One h1 per page, always first in <main>, so headings and focus-on-navigate are predictable;
 * it also sets the document title. A layout effect runs in the same commit that adds the h1, so
 * the tab title never lags the heading (a passive effect could, and a test caught it under load). */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  useLayoutEffect(() => {
    document.title = `${title} - PBIP`; // each page names the browser tab (WCAG 2.4.2)
  }, [title]);
  return (
    <div className="page-header">
      <div>
        <h1 tabIndex={-1}>{title}</h1>
        {description && <p className="page-header__description">{description}</p>}
      </div>
      {actions}
    </div>
  );
}
