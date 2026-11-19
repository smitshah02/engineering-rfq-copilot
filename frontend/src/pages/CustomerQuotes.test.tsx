import { act, screen, waitFor, within } from "@testing-library/react";
import { focusManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App";
import type { ReleasedQuote, ReleasedQuoteReference, RfqSummary } from "../api";
import { DEV_TOKEN_KEY, useAuth } from "../auth";
import { fakeToken, json, mockFetch, renderWithProviders, signedIn } from "../test/utils";

const REFERENCE: ReleasedQuoteReference = {
  quote_id: "QUOTE-1", version: 2, released_at: "2026-11-18T12:00:00Z", expiry_date: "2099-12-31",
};
const RFQ: RfqSummary = {
  rfq_id: "RFQ-1", status: "draft", submitted_at: "2026-11-18T10:00:00Z", released_quote: REFERENCE,
};
const QUOTE: ReleasedQuote = {
  ...REFERENCE, rfq_id: "RFQ-1", status: "released", total_usd: "123.40", currency: "USD",
  estimated_lead_business_days: 8,
  lines: [{ line_number: 10, part_id: "P-RELEASED", quantity: 4, line_total_usd: "123.40" }],
};

function backend(
  rfqs: RfqSummary[] = [RFQ],
  detail: () => Response | Promise<Response> = () => json(200, QUOTE),
) {
  return mockFetch((url) => {
    if (url.startsWith("/api/v1/staff/")) return json(403, {});
    if (url.startsWith("/api/v1/rfqs?")) return json(200, { items: rfqs, limit: 20, offset: 0, has_more: false });
    if (url === "/api/v1/rfqs/RFQ-1") return json(200, { ...rfqs[0], lines: [] });
    if (url === "/api/v1/rfqs/RFQ-1/attachments") return json(200, { items: [] });
    if (url === "/api/v1/quotes/QUOTE-1") return detail();
    if (url === "/api/v1/customers/me") return json(200, {
      customer_id: "CUST-1", company_name: "Customer One", contacts: [],
    });
    return json(404, {});
  });
}
const show = (entry = "/quotes/QUOTE-1") => { signedIn(); return renderWithProviders(<App />, entry); };

describe("customer released quotation discovery", () => {
  it.each(["/", "/rfqs"])("shows a separate release indicator on %s while retaining Received", async (entry) => {
    backend();
    show(entry);
    expect(await screen.findByText("Quotation Available")).toBeInTheDocument();
    expect(screen.getByText("Received")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View Quotation" })).toHaveAttribute("href", "/quotes/QUOTE-1");
    expect(screen.queryByRole("link", { name: "Staff quotations" })).toBeNull();
    expect(screen.queryByText(/pending approval|reasoning|internal quotation|123.40/i)).toBeNull();
  });

  it.each(["/", "/rfqs", "/rfqs/RFQ-1"])("has no quotation link before release on %s", async (entry) => {
    backend([{ ...RFQ, released_quote: null }]);
    show(entry);
    expect((await screen.findAllByText("Received")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("link", { name: "View Quotation" })).toBeNull();
    expect(screen.queryByText("Quotation Available")).toBeNull();
    expect(screen.queryByText("QUOTE-1")).toBeNull();
  });

  it("links the RFQ detail to the released quotation without changing RFQ status", async () => {
    backend();
    show("/rfqs/RFQ-1");
    expect(await screen.findByRole("link", { name: "View Quotation" })).toHaveAttribute("href", "/quotes/QUOTE-1");
    expect(screen.getAllByText("Received").length).toBeGreaterThan(0);
  });

  it("marks an elapsed release as expired in RFQ history while keeping its link", async () => {
    backend([{ ...RFQ, released_quote: { ...REFERENCE, expiry_date: "2020-01-01" } }]);
    show("/rfqs");
    expect(await screen.findByText("Expired")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View Quotation" })).toBeInTheDocument();
    expect(screen.getByText("Received")).toBeInTheDocument();
  });

  it.each(["/", "/rfqs"])("keeps the empty RFQ state on %s", async (entry) => {
    backend([]);
    show(entry);
    expect(await screen.findByText(/No RFQs yet/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View Quotation" })).toBeNull();
    expect(screen.getByRole("link", { name: "Submit your first RFQ" })).toBeInTheDocument();
  });

  it("refetches the RFQ projection to discover a later release", async () => {
    let released = false;
    mockFetch((url) => url.startsWith("/api/v1/rfqs?") ? json(200, {
      items: [{ ...RFQ, released_quote: released ? REFERENCE : null }], limit: 20, offset: 0, has_more: false,
    }) : json(403, {}));
    show("/rfqs");
    await screen.findByText("Received");
    expect(screen.queryByRole("link", { name: "View Quotation" })).toBeNull();
    released = true;
    // A focus refresh uses the same projection as the 30-second discovery refresh.
    try {
      act(() => { focusManager.setFocused(false); focusManager.setFocused(true); });
      expect(await screen.findByRole("link", { name: "View Quotation" })).toBeInTheDocument();
    } finally {
      focusManager.setFocused(undefined);
    }
  });

  it("preserves RFQ submission and history without inventing a quotation", async () => {
    let submissions = 0;
    const received = { ...RFQ, rfq_id: "RFQ-NEW", released_quote: null };
    mockFetch((url, init) => {
      if (url === "/api/v1/rfqs" && init.method === "POST") {
        submissions++;
        return json(201, { rfq_id: "RFQ-NEW", status: "draft" });
      }
      if (url === "/api/v1/rfqs/RFQ-NEW") return json(200, { ...received, lines: [] });
      if (url.endsWith("/attachments")) return json(200, { items: [] });
      if (url.startsWith("/api/v1/rfqs?")) return json(200, { items: [received], limit: 20, offset: 0, has_more: false });
      return json(403, {});
    });
    show("/rfqs/new");
    await userEvent.type(screen.getByLabelText(/Part ID/), "P-NEW");
    await userEvent.click(screen.getByRole("button", { name: "Submit RFQ" }));
    expect(await screen.findByRole("heading", { name: "RFQ-NEW" })).toBeInTheDocument();
    expect(screen.getByText(/Your RFQ was received successfully/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View Quotation" })).toBeNull();
    await userEvent.click(screen.getByRole("link", { name: /All RFQs/ }));
    expect(await screen.findByText("RFQ-NEW")).toBeInTheDocument();
    expect(screen.getByText("Received")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "View Quotation" })).toBeNull();
    expect(submissions).toBe(1);
  });
});

describe("customer quotation detail", () => {
  it("navigates from RFQ history to the existing safe API and shows exact approved prices", async () => {
    const fetch = backend();
    show("/rfqs");
    await userEvent.click(await screen.findByRole("link", { name: "View Quotation" }));
    expect(await screen.findByRole("heading", { name: "QUOTE-1" })).toBeInTheDocument();
    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("8 business days")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "RFQ-1" })).toHaveAttribute("href", "/rfqs/RFQ-1");
    expect(screen.getAllByText("123.40 USD")).toHaveLength(2);
    const table = screen.getByRole("table");
    for (const text of ["10", "P-RELEASED", "4", "123.40 USD"])
      expect(within(table).getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|reject|release|submit|revise/i })).toBeNull();
    expect(screen.queryByText(/cost.breakdown|approval.history|reasoning|internal quotation/i)).toBeNull();
    expect(fetch.mock.calls.some(([url]) => url === "/api/v1/quotes/QUOTE-1")).toBe(true);
    expect(fetch.mock.calls.every(([, init]) => !init.method || init.method === "GET")).toBe(true);
  });

  it("shows loading, unspecified values and empty line items", async () => {
    let finish!: (response: Response) => void;
    backend(undefined, () => new Promise<Response>((resolve) => { finish = resolve; }));
    show();
    expect(screen.getByText("Loading quotation…")).toBeInTheDocument();
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await act(async () => finish(json(200, { ...QUOTE, expiry_date: null, estimated_lead_business_days: null, lines: [] })));
    expect(await screen.findByText(/No line items are available/)).toBeInTheDocument();
    expect(screen.getAllByText("Not specified")).toHaveLength(2);
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText(/has expired/)).toBeNull();
  });

  it("shows expired released quotations and preserves a zero-day lead time", async () => {
    backend(undefined, () => json(200, { ...QUOTE, expiry_date: "2020-01-01", estimated_lead_business_days: 0 }));
    show();
    expect(await screen.findByText(/This quotation has expired/)).toBeInTheDocument();
    expect(screen.getByText("0 business days")).toBeInTheDocument();
    expect(screen.getByText("P-RELEASED")).toBeInTheDocument();
  });

  it.each([403, 404, 500, 503])("handles HTTP %s without showing stale quotation data", async (status) => {
    backend(undefined, () => json(status, { detail: "private database error" }));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("private");
    if (status === 403) expect(alert).toHaveTextContent("Access denied");
    if (status === 404) expect(alert).toHaveTextContent("This quotation is unavailable to your account");
    if (status === 503) expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("P-RELEASED")).toBeNull();
    expect(screen.queryByText("123.40 USD")).toBeNull();
  });

  it("shows a retry state when the server cannot be reached", async () => {
    backend(undefined, () => { throw new TypeError("network down"); });
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the server");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("returns to sign-in on 401", async () => {
    backend(undefined, () => json(401, {}));
    show();
    expect(await screen.findByLabelText("Development token")).toBeInTheDocument();
    expect(screen.queryByText("P-RELEASED")).toBeNull();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });
});

describe("customer quotation identity isolation", () => {
  it("clears visible quotation data on logout", async () => {
    backend();
    show();
    await screen.findByText("P-RELEASED");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(screen.getByLabelText("Development token")).toBeInTheDocument();
    expect(screen.queryByText("P-RELEASED")).toBeNull();
    expect(screen.queryByText("123.40 USD")).toBeNull();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });

  it("removes the first customer's visible quotation immediately on account switch", async () => {
    const oldToken = signedIn();
    const newToken = fakeToken(Math.floor(Date.now() / 1000) + 1200);
    function Switch() {
      const { signIn } = useAuth();
      return <><button onClick={() => signIn(newToken)}>Switch account</button><App /></>;
    }
    mockFetch((url, init) => {
      if (!url.startsWith("/api/v1/quotes/")) return json(403, {});
      return (init.headers as Record<string, string>).Authorization === `Bearer ${oldToken}`
        ? json(200, QUOTE) : json(404, {});
    });
    renderWithProviders(<Switch />, "/quotes/QUOTE-1");
    await screen.findByText("P-RELEASED");
    await userEvent.click(screen.getByRole("button", { name: "Switch account" }));
    expect(screen.queryByText("P-RELEASED")).toBeNull();
    expect(screen.queryByText("123.40 USD")).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable to your account");
  });

  it.each([200, 401])("discards an in-flight old customer HTTP %s after account switch", async (status) => {
    const oldToken = signedIn();
    const newToken = fakeToken(Math.floor(Date.now() / 1000) + 1200);
    let finish!: (response: Response) => void;
    function Switch() {
      const { signIn } = useAuth();
      return <><button onClick={() => signIn(newToken)}>Switch account</button><App /></>;
    }
    mockFetch((url, init) => {
      if (!url.startsWith("/api/v1/quotes/")) return json(403, {});
      if ((init.headers as Record<string, string>).Authorization !== `Bearer ${oldToken}`) return json(404, {});
      return new Promise<Response>((resolve) => { finish = resolve; });
    });
    renderWithProviders(<Switch />, "/quotes/QUOTE-1");
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await userEvent.click(screen.getByRole("button", { name: "Switch account" }));
    await screen.findByRole("alert");
    await act(async () => finish(json(status, status === 200 ? QUOTE : {})));
    expect(screen.queryByText("P-RELEASED")).toBeNull();
    expect(screen.queryByLabelText("Development token")).toBeNull();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBe(newToken);
  });
});
