import { useAuth } from "./auth/AuthContext";
import { Accounts } from "./pages/Accounts";
import { Login } from "./pages/Login";

export function App() {
  const { status } = useAuth();
  if (status === "loading") return <p className="center">Loading...</p>;
  return status === "authenticated" ? <Accounts /> : <Login />;
}
