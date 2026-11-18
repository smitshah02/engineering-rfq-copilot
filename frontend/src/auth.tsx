import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, apiRequest } from "./api";

// DEVELOPMENT ONLY: a manually pasted, short-lived JWT held in React state. Under the Vite dev
// server (import.meta.env.DEV) it is also kept in this tab's sessionStorage so a reload stays
// signed in; production builds keep it in memory only. Never localStorage, cookies or logs.
// To adopt a real identity provider, replace this provider: consumers only use
// `useAuth().token/signOut` and `useApi()`.
export const DEV_TOKEN_KEY = "rfq_copilot.dev_token";

/** Client-side convenience only: FastAPI verifies every token. Malformed or exp-less = unusable. */
export function tokenUsable(token: string, nowSeconds = Date.now() / 1000): boolean {
  try {
    const part = token.split(".")[1];
    if (!part) return false;
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "="));
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp === "number" && exp > nowSeconds;
  } catch {
    return false;
  }
}

function loadDevToken(): string | null {
  if (!import.meta.env.DEV) return null;
  try {
    const t = sessionStorage.getItem(DEV_TOKEN_KEY);
    if (t && tokenUsable(t)) return t;
    sessionStorage.removeItem(DEV_TOKEN_KEY);
  } catch { /* storage blocked: memory only */ }
  return null;
}

function saveDevToken(t: string | null) {
  if (!import.meta.env.DEV) return;
  try {
    if (t) sessionStorage.setItem(DEV_TOKEN_KEY, t);
    else sessionStorage.removeItem(DEV_TOKEN_KEY);
  } catch { /* storage blocked: memory only */ }
}

interface Auth { token: string | null; signIn: (t: string) => void; signOut: () => void }
const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(loadDevToken);
  const queryClient = useQueryClient();
  // Clearing the cache on every identity change keeps one customer's data from ever
  // rendering under another customer's token.
  const value = useMemo<Auth>(() => {
    const swap = (t: string | null) => { queryClient.clear(); saveDevToken(t); setToken(t); };
    return { token, signIn: (t) => swap(t.trim()), signOut: () => swap(null) };
  }, [token, queryClient]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** API caller bound to the current token; a 401 drops the token and returns to sign-in. */
export function useApi() {
  const { token, signOut } = useAuth();
  return useCallback(
    async <T,>(path: string, init?: RequestInit) => {
      if (!token) throw new ApiError(401, "Not signed in.");
      try {
        return await apiRequest<T>(token, path, init);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) signOut();
        throw e;
      }
    },
    [token, signOut],
  );
}
