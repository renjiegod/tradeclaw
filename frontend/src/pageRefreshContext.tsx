import { createContext, useContext } from "react";

export const PageRefreshContext = createContext(0);

export function usePageRefreshToken(): number {
  return useContext(PageRefreshContext);
}
