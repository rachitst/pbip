import { createContext, useContext } from "react";
import type { UserOut } from "../api/client";

export const MeContext = createContext<UserOut | null>(null);

/** Only valid below SessionRoot, which renders its children once /auth/me has loaded. */
export function useCurrentUser(): UserOut {
  const me = useContext(MeContext);
  if (!me) throw new Error("useCurrentUser must be used below SessionRoot");
  return me;
}
