import { Link } from "react-router";
import { PageHeader } from "../ui/PageHeader";

/** Shown for unknown paths AND for pages outside the user's role, mirroring the API, where a
 * resource you may not see and one that does not exist are both 404. */
export function NotFound({ homePath }: { homePath: string }) {
  return (
    <div className="page">
      <PageHeader
        title="Page not found"
        description="This page does not exist or is not available to you."
      />
      <Link to={homePath}>Go to your home page</Link>
    </div>
  );
}
