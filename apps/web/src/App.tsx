import { useState } from "react";
import { RouterProvider, createBrowserRouter } from "react-router";
import { useAuth } from "./auth/AuthContext";
import { Landing } from "./pages/Landing";
import { Login } from "./pages/Login";
import { Register } from "./pages/Register";
import { routes } from "./shell/routes";
import { ThemeProvider } from "./shell/theme";

export function App() {
  return (
    <ThemeProvider>
      <AuthenticatedOrLogin />
    </ThemeProvider>
  );
}

function AuthenticatedOrLogin() {
  const { status } = useAuth();
  if (status === "loading")
    return (
      <p className="center" role="status">
        Loading...
      </p>
    );
  // Signed out on any URL shows the sign-in page in place, so a deep link survives sign-in.
  return status === "authenticated" ? <AppRouter /> : <SignedOut />;
}

/** Sign-in and registration swap in place, so the deep link in the address bar survives both.
 * At "/" (no deep link to keep) the same forms sit in the hero of the landing page. */
function SignedOut() {
  const [registering, setRegistering] = useState(false);
  const atFrontDoor = window.location.pathname === "/";
  const form = registering ? (
    <Register
      embedded={atFrontDoor}
      onSignIn={() => {
        setRegistering(false);
      }}
    />
  ) : (
    <Login
      embedded={atFrontDoor}
      onRegister={() => {
        setRegistering(true);
      }}
    />
  );
  if (!atFrontDoor) return form;
  return (
    <Landing
      form={form}
      onOpenAccount={() => {
        setRegistering(true);
      }}
      onSignIn={() => {
        setRegistering(false);
      }}
    />
  );
}

/** A fresh router per signed-in session: no route state survives into the next person's. */
function AppRouter() {
  const [router] = useState(() => createBrowserRouter(routes));
  return <RouterProvider router={router} />;
}
