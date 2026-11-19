import { Fragment, createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
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

interface Auth { token: string | null; sessionId: number; signIn: (t: string) => void; signOut: () => void; isCurrent: (id: number) => boolean }
const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(loadDevToken);
  const [sessionId, setSessionId] = useState(0);
  const generation = useRef(0);
  const queryClient = useQueryClient();
  // Clearing the cache on every identity change keeps one customer's data from ever
  // rendering under another customer's token.
  const value = useMemo<Auth>(() => {
    const swap = (t: string | null) => {
      generation.current += 1;
      void queryClient.cancelQueries();
      queryClient.clear();
      saveDevToken(t);
      setSessionId(generation.current);
      setToken(t);
    };
    return { token, sessionId, signIn: (t) => swap(t.trim()), signOut: () => swap(null), isCurrent: (id) => generation.current === id };
  }, [token, sessionId, queryClient]);
  // Unmount query observers and page-local data as well as clearing the shared cache.
  return <AuthContext.Provider value={value}><Fragment key={sessionId}>{children}</Fragment></AuthContext.Provider>;
}

export function useAuth(): Auth {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

/** API caller bound to the current token; a 401 drops the token and returns to sign-in. */
export function useApi() {
  const { token, sessionId, signOut, isCurrent } = useAuth();
  return useCallback(
    async <T,>(path: string, init?: RequestInit) => {
      if (!token) throw new ApiError(401, "Not signed in.");
      try {
        const result = await apiRequest<T>(token, path, init);
        if (!isCurrent(sessionId)) throw new DOMException("Session changed", "AbortError");
        return result;
      } catch (e) {
        // A late result/401 from the previous identity must not affect the new session.
        if (!isCurrent(sessionId)) throw new DOMException("Session changed", "AbortError");
        if (e instanceof ApiError && e.status === 401) signOut();
        throw e;
      }
    },
    [token, sessionId, signOut, isCurrent],
  );
}
