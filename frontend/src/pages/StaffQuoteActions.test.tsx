import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { QuoteAction, StaffQuoteActions as Actions, StaffQuoteDetail } from "../api";
import { useAuth } from "../auth";
import { fakeToken, json, mockFetch, renderWithProviders, signedIn } from "../test/utils";
import StaffQuoteActions from "./StaffQuoteActions";

const QUOTE: StaffQuoteDetail = {
  quote_id: "QUOTE-1", rfq_id: "RFQ-1", status: "pending_approval", current_version: 2,
  quote_version_id: "QUOTE-1-V2", created_at: "2026-11-18T10:00:00Z",
  version_created_at: "2026-11-18T11:00:00Z", expiry_date: "2026-12-17", currency: "USD",
  total_usd: "123.40", estimated_lead_business_days: 8,
  rfq: { rfq_id: "RFQ-1", status: "draft", submitted_at: "2026-11-18T09:00:00Z" },
  lines: [{ line_number: 1, part_id: "P-1", quantity: 1, line_total_usd: "123.40", part: { part_id: "P-1" } }],
};
const CONTEXT = { rfq_source_sha256: "a".repeat(64), requested_delivery_date: "2026-12-01" };
const LABELS: Record<QuoteAction, string> = {
  request_approval: "Submit for Review", approve_quote: "Approve", reject_quote: "Reject", release_quote: "Release",
};

function projection(allowed: QuoteAction[] = []): Actions {
  return {
    quote_id: "QUOTE-1", expected_version: 2, attestation_context: allowed.includes("approve_quote") ? CONTEXT : null,
    reviews: [{ kind: "human_approval", reasons: ["HUMAN_APPROVAL_REQUIRED"] }],
    actions: Object.fromEntries(Object.keys(LABELS).map((action) => [action, {
      allowed: allowed.includes(action as QuoteAction), reason: allowed.includes(action as QuoteAction) ? null : "permission_denied",
    }])) as Actions["actions"],
  };
}

function show(refresh = vi.fn(async () => undefined), controls = false) {
  function Controls() {
    const auth = useAuth();
    return <><button onClick={() => auth.signIn(fakeToken(Math.floor(Date.now() / 1000) + 400))}>Switch identity</button><button onClick={auth.signOut}>Logout</button></>;
  }
  signedIn();
  const view = renderWithProviders(<>{controls && <Controls />}<StaffQuoteActions quote={QUOTE} refresh={refresh} /></>);
  return { ...view, refresh };
}

describe("human quotation actions", () => {
  it("uses trusted server action hints even when the displayed status suggests another action", async () => {
    mockFetch(() => json(200, projection(["release_quote"])));
    show();
    expect(await screen.findByRole("button", { name: "Release" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Submit for Review" })).toBeNull();
  });

  it("keeps actions unavailable without a successful matching server assessment", async () => {
    mockFetch(() => json(403, { detail: "secret SQL" }));
    show();
    expect(await screen.findByRole("alert")).not.toHaveTextContent("secret");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("rejects a stale projection instead of guessing eligibility", async () => {
    mockFetch(() => json(200, { ...projection(["approve_quote"]), expected_version: 1 }));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("stale");
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it.each(Object.keys(LABELS) as QuoteAction[])("requires deliberate confirmation for %s and refreshes after success", async (action) => {
    let finish!: (response: Response) => void;
    const fetch = mockFetch((_url, init) => init.method === "POST"
      ? new Promise<Response>((resolve) => { finish = resolve; }) : json(200, projection([action])));
    const { refresh } = show();
    await userEvent.click(await screen.findByRole("button", { name: LABELS[action] }));
    const form = screen.getByRole("form", { name: `Confirm ${LABELS[action]}` });
    const confirm = within(form).getByRole("button", { name: `Confirm ${LABELS[action]}` });
    expect(confirm).toBeDisabled();
    if (action === "approve_quote") {
      expect(screen.getByText(CONTEXT.rfq_source_sha256)).toBeInTheDocument();
      expect(screen.getByText(/Requested delivery date: 2026-12-01/)).toBeInTheDocument();
      const attestation = screen.getByRole("checkbox", { name: /I reviewed the manufacturing/ });
      expect(attestation).not.toBeChecked();
      await userEvent.click(attestation);
    }
    if (action === "reject_quote") await userEvent.type(screen.getByLabelText("Rejection reason"), "Revise the delivery terms.");
    await userEvent.click(screen.getByRole("checkbox", { name: /I understand this action/ }));
    await userEvent.dblClick(confirm);
    const posts = fetch.mock.calls.filter(([, init]) => init.method === "POST");
    expect(posts).toHaveLength(1);
    const [url, init] = posts[0];
    expect(url).toBe(action === "request_approval" ? "/api/v1/staff/quotes/QUOTE-1/submit-for-review"
      : `/api/v1/quote-decisions/QUOTE-1/${{ approve_quote: "approval", reject_quote: "rejection", release_quote: "release" }[action]}`);
    const body = JSON.parse(init.body as string);
    expect(body.expected_version).toBe(2);
    expect(body.idempotency_key).toBeTruthy();
    if (action === "approve_quote") expect(body).toMatchObject({ ...CONTEXT, no_contradictions: true });
    else expect(body.no_contradictions).toBeUndefined();
    if (action === "reject_quote") expect(body.reason).toBe("Revise the delivery terms.");
    expect(refresh).not.toHaveBeenCalled();
    expect(confirm).toBeDisabled();
    await act(async () => finish(json(200, { quote_id: "QUOTE-1", version: 2, action: "approved", status: "approved", replayed: false })));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(fetch.mock.calls.filter(([url]) => url.includes("/actions?")).length).toBeGreaterThan(1);
  });

  it("reuses the same key and body after an uncertain network/503 result", async () => {
    const fetch = mockFetch((_url, init) => init.method === "POST" ? json(503, {}) : json(200, projection(["release_quote"])));
    show();
    await userEvent.click(await screen.findByRole("button", { name: "Release" }));
    await userEvent.click(screen.getByRole("checkbox", { name: /I understand/ }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm Release" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Confirm Release" }));
    const posts = fetch.mock.calls.filter(([, init]) => init.method === "POST");
    expect(posts).toHaveLength(2);
    expect(posts[0][1].body).toBe(posts[1][1].body);
    expect(fetch.mock.calls[0][0]).toContain("/actions?");
  });

  it.each([403, 404, 409, 422])("handles HTTP %s by closing confirmation and refreshing", async (status) => {
    mockFetch((_url, init) => init.method === "POST" ? json(status, { detail: "stale_version" }) : json(200, projection(["release_quote"])));
    const { refresh } = show();
    await userEvent.click(await screen.findByRole("button", { name: "Release" }));
    await userEvent.click(screen.getByRole("checkbox", { name: /I understand/ }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm Release" }));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
    expect(screen.queryByRole("form")).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent("version changed");
  });

  it.each(["Switch identity", "Logout"])("clears confirmation and private rejection text on %s", async (control) => {
    mockFetch(() => json(200, projection(["reject_quote"])));
    show(undefined, true);
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await userEvent.type(screen.getByLabelText("Rejection reason"), "Private staff reason");
    await userEvent.click(screen.getByRole("checkbox", { name: /I understand/ }));
    await userEvent.click(screen.getByRole("button", { name: control }));
    expect(screen.queryByDisplayValue("Private staff reason")).toBeNull();
    expect(screen.queryByRole("form")).toBeNull();
  });
});
