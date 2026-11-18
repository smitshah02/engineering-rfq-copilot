import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter, type InitialEntry } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { AuthProvider, DEV_TOKEN_KEY } from "../auth";

/** An unsigned JWT-shaped string; the frontend never verifies signatures. */
export function fakeToken(exp: number | null = Math.floor(Date.now() / 1000) + 600): string {
  const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${b64({ alg: "EdDSA" })}.${b64(exp === null ? { sub: "u" } : { sub: "u", exp })}.sig`;
}

export const signedIn = (token = fakeToken()) => { sessionStorage.setItem(DEV_TOKEN_KEY, token); return token; };

export const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;
export function mockFetch(handler: Handler) {
  const fn = vi.fn((url: string, init: RequestInit) => Promise.resolve(handler(url, init ?? {})));
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function renderWithProviders(ui: ReactNode, entry: InitialEntry = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <MemoryRouter initialEntries={[entry]}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
