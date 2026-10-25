import { useState } from "react";
import { useAuth } from "./auth/AuthContext";
import { Accounts } from "./pages/Accounts";
import { Login } from "./pages/Login";
import { Profile } from "./pages/Profile";

type Page = "accounts" | "profile";

const PAGES: { id: Page; label: string }[] = [
  { id: "accounts", label: "Accounts" },
  { id: "profile", label: "Profile & security" },
];

export function App() {
  const { status } = useAuth();
  if (status === "loading") return <p className="center">Loading...</p>;
  return status === "authenticated" ? <Shell /> : <Login />;
}

function Shell() {
  const { logout } = useAuth();
  const [page, setPage] = useState<Page>("accounts");
  return (
    <div className="card">
      <nav className="row nav" aria-label="Main">
        <div className="tabs">
          {PAGES.map((item) => (
            <button
              key={item.id}
              type="button"
              className={page === item.id ? "tab active" : "tab"}
              aria-current={page === item.id ? "page" : undefined}
              onClick={() => {
                setPage(item.id);
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="secondary"
          onClick={() => {
            void logout();
          }}
        >
          Sign out
        </button>
      </nav>
      {page === "accounts" ? <Accounts /> : <Profile />}
    </div>
  );
}
