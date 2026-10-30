import { useEffect } from "react";
import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** One h1 per page, always first in <main>, so headings and focus-on-navigate are predictable;
 * it also sets the document title. */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  useEffect(() => {
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
