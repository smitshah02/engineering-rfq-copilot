import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "./App";
import { DEV_TOKEN_KEY, tokenUsable, useApi, useAuth } from "./auth";
import { fakeToken, json, mockFetch, renderWithProviders, signedIn } from "./test/utils";

function Probe() {
  const { token, signIn, signOut } = useAuth();
  const api = useApi();
  return (
    <>
      <p>{token ? "signed-in" : "signed-out"}</p>
      <button onClick={() => signIn(fakeToken())}>in</button>
      <button onClick={signOut}>out</button>
      <button onClick={() => api("/customers/me").catch(() => {})}>call</button>
    </>
  );
}

describe("tokenUsable", () => {
  it("accepts an unexpired token and rejects expired, exp-less or malformed ones", () => {
    expect(tokenUsable(fakeToken())).toBe(true);
    expect(tokenUsable(fakeToken(1))).toBe(false);
    expect(tokenUsable(fakeToken(null))).toBe(false);
    for (const bad of ["", "abc", "a.b.c", "a.%%%.c", `x.${btoa("not json")}.y`]) expect(tokenUsable(bad)).toBe(false);
  });
});

describe("development token persistence", () => {
  it("survives a remount (page reload) in the same tab", async () => {
    const first = renderWithProviders(<Probe />);
    await userEvent.click(screen.getByText("in"));
    expect(screen.getByText("signed-in")).toBeInTheDocument();
    first.unmount();
    renderWithProviders(<Probe />);
    expect(screen.getByText("signed-in")).toBeInTheDocument();
  });

  it.each([["expired", fakeToken(1)], ["malformed", "not-a-jwt"]])("discards a %s stored token", (_, token) => {
    sessionStorage.setItem(DEV_TOKEN_KEY, token);
    renderWithProviders(<Probe />);
    expect(screen.getByText("signed-out")).toBeInTheDocument();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });

  it("clears the stored token on sign out", async () => {
    signedIn();
    renderWithProviders(<Probe />);
    await userEvent.click(screen.getByText("out"));
    expect(screen.getByText("signed-out")).toBeInTheDocument();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });

  it("clears authentication when the API answers 401", async () => {
    signedIn();
    mockFetch(() => json(401, { detail: "Not authenticated" }));
    renderWithProviders(<Probe />);
    await userEvent.click(screen.getByText("call"));
    expect(await screen.findByText("signed-out")).toBeInTheDocument();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });

  it("is not active outside development mode", async () => {
    vi.stubEnv("DEV", false);
    signedIn();
    renderWithProviders(<Probe />);
    expect(screen.getByText("signed-out")).toBeInTheDocument(); // stored token ignored
    sessionStorage.clear();
    await userEvent.click(screen.getByText("in"));
    expect(screen.getByText("signed-in")).toBeInTheDocument();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull(); // memory only
  });

  it("keeps the current route after a reload instead of redirecting", () => {
    signedIn();
    renderWithProviders(<App />, "/rfqs/new");
    expect(screen.getByRole("heading", { name: "New RFQ" })).toBeInTheDocument();
  });
});
