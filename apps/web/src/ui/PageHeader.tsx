import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

/** One h1 per page, always first in <main>, so headings and focus-on-navigate are predictable. */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="page-header">
      <div>
        <h1 tabIndex={-1}>{title}</h1>
        {description && <p className="page-header__description">{description}</p>}
      </div>
      {actions}
    </header>
  );
}
