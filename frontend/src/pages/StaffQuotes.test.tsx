import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App";
import type { StaffQuoteDetail, StaffQuotePage, StaffQuoteSummary } from "../api";
import { DEV_TOKEN_KEY, useAuth } from "../auth";
import { fakeToken, json, mockFetch, renderWithProviders, signedIn } from "../test/utils";

const SUMMARY: StaffQuoteSummary = {
  quote_id: "QUOTE-1", rfq_id: "RFQ-1", status: "revised_draft", current_version: 2,
  created_at: "2026-11-18T10:00:00Z", expiry_date: "2026-12-17", currency: "USD", total_usd: "123.40",
};
const PAGE: StaffQuotePage = { items: [SUMMARY], limit: 20, offset: 0, has_more: false };
const DETAIL: StaffQuoteDetail = {
  ...SUMMARY, total_usd: "123.40", quote_version_id: "QV-2", version_created_at: "2026-11-18T11:00:00Z",
  estimated_lead_business_days: 8,
  rfq: { rfq_id: "RFQ-1", status: "validated", submitted_at: "2026-11-18T09:00:00Z" },
  lines: [{ line_number: 10, part_id: "P-1", quantity: 4, line_total_usd: "123.40", part: {
    part_id: "P-1", part_type: "l_bracket", quantity: 4, material_id: "MAT-1", finish_id: "FIN-1",
    length_mm: "120.000", width_mm: "60.000", height_mm: "30.000", thickness_mm: "2.000",
    tolerance_mm: "0.100", bend_radius_mm: "3.000", weld_length_mm: "0.000", hole_count: 2,
  } }],
};

const ACCESS = { items: [], limit: 1, offset: 0, has_more: false };
type Reply = (params: URLSearchParams) => Response | Promise<Response>;
function backend(list: Reply = () => json(200, PAGE), detail: () => Response | Promise<Response> = () => json(200, DETAIL)) {
  return mockFetch((url) => {
    const parsed = new URL(url, "http://test");
    if (parsed.pathname === "/api/v1/staff/quotes")
      return parsed.searchParams.get("limit") === "1" ? json(200, ACCESS) : list(parsed.searchParams);
    if (parsed.pathname === "/api/v1/staff/quotes/QUOTE-1") return detail();
    if (parsed.pathname === "/api/v1/rfqs") return json(200, { items: [], limit: 20, offset: 0, has_more: false });
    return json(404, {});
  });
}
const show = (entry = "/staff/quotes") => { signedIn(); return renderWithProviders(<App />, entry); };

describe("staff quotation discovery", () => {
  it("renders safe summary columns, exact amounts and nullable fields", async () => {
    backend(() => json(200, { ...PAGE, items: [SUMMARY, {
      ...SUMMARY, quote_id: "QUOTE-2", rfq_id: "RFQ-2", expiry_date: null, total_usd: null,
    }] }));
    show();
    expect(await screen.findByText("QUOTE-1")).toBeInTheDocument();
    const table = screen.getByRole("table");
    for (const label of ["RFQ-1", "Revised draft", "2", "123.40 USD", "Not specified", "Not available"])
      expect(within(table).getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getByText(/Non-binding until authorized release/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Staff quotations" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|reject|release|submit quotation|revise/i })).toBeNull();
  });

  it("uses server pagination and resets the offset when the status changes", async () => {
    const fetch = backend((params) => {
      const offset = Number(params.get("offset"));
      return json(200, { ...PAGE, offset, has_more: offset === 0, items: [{
        ...SUMMARY, quote_id: offset ? "QUOTE-21" : "QUOTE-1",
      }] });
    });
    show();
    await screen.findByText("QUOTE-1");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("QUOTE-21")).toBeInTheDocument();
    expect(screen.queryByText("QUOTE-1")).toBeNull();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Quotation status"), "draft");
    await screen.findByText("QUOTE-1");
    expect(fetch.mock.calls.some(([url]) => url.includes("offset=20"))).toBe(true);
    expect(fetch.mock.calls.some(([url]) => url.includes("offset=0&status=draft"))).toBe(true);
  });

  it("submits and clears exact identifier search on the backend", async () => {
    const fetch = backend();
    show();
    await screen.findByText("QUOTE-1");
    await userEvent.type(screen.getByLabelText("Exact quotation or RFQ identifier"), "RFQ-1");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => url.includes("q=RFQ-1"))).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByLabelText("Exact quotation or RFQ identifier")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  });

  it("renders loading and empty states", async () => {
    let finish!: (response: Response) => void;
    backend(() => new Promise<Response>((resolve) => { finish = resolve; }));
    show();
    expect(await screen.findByText("Loading quotations…")).toBeInTheDocument();
    await act(async () => finish(json(200, { ...PAGE, items: [] })));
    expect(await screen.findByText(/No quotations found in your authorized scope/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Staff quotations" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it.each([403, 404, 500, 503])("handles list HTTP %s without exposing backend details", async (status) => {
    backend(() => json(status, { detail: "secret SQL payload", message: "secret SQL payload" }));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("secret");
    if (status === 403) expect(alert).toHaveTextContent("Access denied");
    if (status === 404) expect(alert).toHaveTextContent("Not found");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("bounds pagination and drops invalid URL filters", async () => {
    const fetch = backend();
    show("/staff/quotes?offset=10001&status=invalid");
    await screen.findByText("QUOTE-1");
    expect(fetch.mock.calls.some(([url]) => url === "/api/v1/staff/quotes?limit=20&offset=0")).toBe(true);
    expect(fetch.mock.calls.some(([url]) => url.includes("invalid") || url.includes("10001"))).toBe(false);
  });
});

describe("staff quotation detail", () => {
  it.each(["draft", "approved", "released"] as const)("shows a state-aware notice for %s", async (status) => {
    backend(undefined, () => json(200, { ...DETAIL, status }));
    show("/staff/quotes/QUOTE-1");
    await screen.findByRole("heading", { name: "QUOTE-1" });
    if (status === "released") {
      expect(screen.getByText("This quotation has been released to the customer.")).toBeInTheDocument();
      expect(screen.queryByText(/Non-binding until authorized release/)).toBeNull();
    } else {
      expect(screen.getByText(/Internal quotation. Non-binding until authorized release/)).toBeInTheDocument();
      expect(screen.queryByText("This quotation has been released to the customer.")).toBeNull();
    }
  });

  it("navigates to existing detail API, shows priced lines/specifications and has no mutations", async () => {
    const fetch = backend();
    show();
    await userEvent.click(await screen.findByRole("link", { name: "View details for QUOTE-1" }));
    expect(await screen.findByRole("heading", { name: "QUOTE-1" })).toBeInTheDocument();
    for (const text of ["QV-2", "8 business days", "P-1", "l_bracket", "MAT-1", "FIN-1", "120.000", "3.000", "0.100"])
      expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getAllByText("123.40 USD")).toHaveLength(2);
    expect(screen.getByText("Under review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /All staff quotations/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve|release|reject|revise|submit/i })).toBeNull();
    expect(fetch.mock.calls.every(([, init]) => !init.method || init.method === "GET")).toBe(true);
    expect(fetch.mock.calls.some(([url]) => url === "/api/v1/staff/quotes/QUOTE-1")).toBe(true);
    expect(screen.queryByText(/readiness|reasoning|cost_breakdown/i)).toBeNull();
  });

  it("shows loading then missing lead time and expiry without inventing values", async () => {
    let finish!: (response: Response) => void;
    backend(undefined, () => new Promise<Response>((resolve) => { finish = resolve; }));
    show("/staff/quotes/QUOTE-1");
    expect(await screen.findByText("Loading quotation…")).toBeInTheDocument();
    await act(async () => finish(json(200, { ...DETAIL, estimated_lead_business_days: null, expiry_date: null })));
    expect(await screen.findAllByText("Not specified")).toHaveLength(2);
  });

  it.each([403, 404, 503])("handles detail HTTP %s safely", async (status) => {
    backend(undefined, () => json(status, { detail: "secret database exception" }));
    show("/staff/quotes/QUOTE-1");
    expect(await screen.findByRole("alert")).not.toHaveTextContent("secret");
    expect(screen.queryByText("P-1")).toBeNull();
  });
});

describe("staff authorization and identity cleanup", () => {
  it("requires authentication for direct staff routes", () => {
    const fetch = mockFetch(() => json(500, {}));
    renderWithProviders(<App />, "/staff/quotes/QUOTE-1");
    expect(screen.getByLabelText("Development token")).toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps customer navigation and dashboards free of internal quotations", async () => {
    const fetch = mockFetch((url) => {
      if (url.startsWith("/api/v1/staff/")) return json(403, {});
      if (url.startsWith("/api/v1/rfqs")) return json(200, { items: [], limit: 20, offset: 0, has_more: false });
      return json(200, { customer_id: "CUST-1", company_name: "Customer One", contacts: [] });
    });
    show("/");
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(await screen.findByText("Customer One")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Staff quotations" })).toBeNull();
    expect(screen.getAllByRole("link", { name: "New RFQ" }).length).toBeGreaterThan(0);
    expect(screen.queryByText(/QUOTE-1|123.40|Internal quotation/)).toBeNull();
  });

  it("blocks customer direct links before requesting quotation detail", async () => {
    const fetch = mockFetch(() => json(403, {}));
    show("/staff/quotes/QUOTE-1");
    expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
    expect(screen.queryByRole("link", { name: "Staff quotations" })).toBeNull();
    expect(fetch.mock.calls.every(([url]) => url === "/api/v1/staff/quotes?limit=1")).toBe(true);
  });

  it("clears the visible quotation and stored session on logout", async () => {
    backend();
    show("/staff/quotes/QUOTE-1");
    await screen.findByText("P-1");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(screen.getByLabelText("Development token")).toBeInTheDocument();
    expect(screen.queryByText("P-1")).toBeNull();
    expect(screen.queryByText("123.40 USD")).toBeNull();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });

  it("clears staff data and navigation when switching to a customer identity", async () => {
    const firstToken = signedIn();
    const secondToken = fakeToken(Math.floor(Date.now() / 1000) + 1200);
    function Switch() {
      const { signIn } = useAuth();
      return <><button onClick={() => signIn(secondToken)}>Switch identity</button><App /></>;
    }
    mockFetch((url, init) => {
      const authorization = (init.headers as Record<string, string>).Authorization;
      if (authorization !== `Bearer ${firstToken}`) return json(403, {});
      return url.endsWith("QUOTE-1") ? json(200, DETAIL) : json(200, ACCESS);
    });
    renderWithProviders(<Switch />, "/staff/quotes/QUOTE-1");
    await screen.findByText("P-1");
    await userEvent.click(screen.getByRole("button", { name: "Switch identity" }));
    expect(screen.queryByText("P-1")).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
    expect(screen.queryByRole("link", { name: "Staff quotations" })).toBeNull();
  });

  it("drops authentication when a staff request returns 401", async () => {
    backend(() => json(401, {}));
    show();
    expect(await screen.findByLabelText("Development token")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(sessionStorage.getItem(DEV_TOKEN_KEY)).toBeNull();
  });
});
