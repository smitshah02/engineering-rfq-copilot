import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, quoteActionReason, type QuoteAction, type QuoteActionResult, type StaffQuoteActions as Actions, type StaffQuoteDetail } from "../api";
import { useApi, useAuth } from "../auth";
import { commercialTotal } from "../staffQuotes";
import { ErrorPanel, Loading } from "../ui";

const LABELS: Record<QuoteAction, string> = {
  request_approval: "Submit for Review", approve_quote: "Approve", reject_quote: "Reject", release_quote: "Release",
};
const DECISIONS: Partial<Record<QuoteAction, string>> = {
  approve_quote: "approval", reject_quote: "rejection", release_quote: "release",
};
const CONSEQUENCES: Record<QuoteAction, string> = {
  request_approval: "Send this version for independent human review. It remains unavailable to the customer.",
  approve_quote: "Record your approval of this exact version. The quotation remains unavailable to the customer until a separate authorized person releases it.",
  reject_quote: "Reject this version and preserve your reason in the approval history. A new revision is required before resubmission.",
  release_quote: "Make the approved version available to the authorized customer. This records release; it does not send or dispatch anything.",
};

// Mounted only inside the staff detail page. The parent's identity/version/status key
// removes confirmation, attestation, reasons and keys when the reviewed context changes.
export default function StaffQuoteActions({ quote, refresh }: { quote: StaffQuoteDetail; refresh: () => Promise<unknown> }) {
  const api = useApi();
  const { sessionId, isCurrent } = useAuth();
  const client = useQueryClient();
  const [selected, setSelected] = useState<QuoteAction | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [attested, setAttested] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [complete, setComplete] = useState(false);
  const inFlight = useRef(false);
  const keys = useRef(new Map<string, string>());
  const actions = useQuery({
    queryKey: ["staff-quote-actions", sessionId, quote.quote_id, quote.current_version, quote.status],
    queryFn: async ({ signal }) => (await api<Actions>(`/staff/quotes/${encodeURIComponent(quote.quote_id)}/actions?expected_version=${quote.current_version}`, { signal })).data,
    retry: false,
    staleTime: 0,
  });
  const projection = actions.data;
  const matching = projection?.quote_id === quote.quote_id && projection?.expected_version === quote.current_version;
  const allowed = (action: QuoteAction) => projection !== undefined && matching && !actions.isError && projection.actions[action]?.allowed === true;
  const context = matching && projection ? projection.attestation_context : null;
  const validReason = reason.trim().length > 0 && reason.length <= 1000 && !/[\u0000-\u001f\u007f]/.test(reason);
  const canConfirm = selected !== null && allowed(selected) && confirmed && !busy && !complete
    && (selected !== "approve_quote" || (attested && context !== null))
    && (selected !== "reject_quote" || validReason);

  async function perform() {
    if (!selected || !canConfirm || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    const body = {
      expected_version: quote.current_version,
      ...(selected === "approve_quote" && context ? { ...context, no_contradictions: true } : {}),
      ...(selected === "reject_quote" ? { reason } : {}),
    };
    const identity = JSON.stringify([quote.quote_id, selected, body]);
    const idempotency_key = keys.current.get(identity) ?? crypto.randomUUID();
    keys.current.set(identity, idempotency_key);
    const path = selected === "request_approval"
      ? `/staff/quotes/${encodeURIComponent(quote.quote_id)}/submit-for-review`
      : `/quote-decisions/${encodeURIComponent(quote.quote_id)}/${DECISIONS[selected]}`;
    try {
      await api<QuoteActionResult>(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, idempotency_key }),
      });
      if (!isCurrent(sessionId)) return;
      setComplete(true); // No optimistic status change; block another action until refetch.
      setSelected(null);
      setAttested(false);
      setConfirmed(false);
      setReason("");
      await Promise.all([
        refresh(), actions.refetch(),
        client.invalidateQueries({ queryKey: ["staff-quotes", sessionId] }),
      ]);
    } catch (caught) {
      if (!isCurrent(sessionId)) return;
      setError(caught);
      if (caught instanceof ApiError && [403, 404, 409, 422].includes(caught.status)) {
        setSelected(null);
        setAttested(false);
        setConfirmed(false);
        await Promise.all([refresh(), actions.refetch()]);
      }
      // Network/503 retries keep the same payload key, including an unknown commit outcome.
    } finally {
      inFlight.current = false;
      if (isCurrent(sessionId)) setBusy(false);
    }
  }

  return <section className="card mb-6 p-4" aria-labelledby="staff-actions">
    <h2 id="staff-actions" className="mb-3 font-semibold">Human quotation workflow</h2>
    <p className="mb-3 text-sm text-slate-600">Actions are assessed by the server and checked again when confirmed. Independent actors are required by policy.</p>
    {actions.isPending ? <Loading label="Checking available actions…" /> : actions.isError ? <>
      <p className="mb-2 text-sm">Actions are unavailable until the server can assess this version.</p>
      <ErrorPanel error={actions.error} onRetry={() => actions.refetch()} />
    </> : !matching || !projection ? <p role="alert">Action information is stale. Refresh the quotation.</p> : <>
      {projection.reviews.length > 0 && <ul className="mb-3 list-inside list-disc text-sm">
        {projection.reviews.map((review) => <li key={review.kind}>{review.kind.replaceAll("_", " ")}{review.reasons.length > 0 ? `: ${review.reasons.join(", ")}` : ""}</li>)}
      </ul>}
      <div className="flex flex-wrap gap-2">
        {(Object.keys(LABELS) as QuoteAction[]).filter(allowed).map((action) =>
          <button key={action} className="btn-secondary" disabled={busy || complete || selected !== null} onClick={() => {
            setSelected(action); setConfirmed(false); setAttested(false); setReason(""); setError(null);
          }}>{LABELS[action]}</button>)}
      </div>
      {!Object.keys(LABELS).some((action) => allowed(action as QuoteAction)) && <p className="text-sm">No actions are currently available to your account for this version.</p>}
      <ul className="mt-2 text-sm text-slate-600">
        {(Object.keys(LABELS) as QuoteAction[]).filter((action) => !allowed(action) && projection.actions[action]?.reason !== "permission_denied").map((action) =>
          <li key={action}>{LABELS[action]}: {quoteActionReason(projection.actions[action]?.reason ?? null)}</li>)}
      </ul>
    </>}
    {selected && <form className="mt-4 space-y-3 border-t border-slate-200 pt-4" aria-label={`Confirm ${LABELS[selected]}`} onSubmit={(event) => { event.preventDefault(); void perform(); }}>
      <h3 className="font-semibold">Confirm {LABELS[selected]}</h3>
      <p className="text-sm">{CONSEQUENCES[selected]}</p>
      <p className="text-sm">Quotation {quote.quote_id}, version {quote.current_version}. Total {commercialTotal(quote.total_usd, quote.currency)}.</p>
      {selected === "approve_quote" && context && <>
        <p className="break-all text-sm">RFQ source SHA-256: <code>{context.rfq_source_sha256}</code></p>
        <p className="text-sm">Requested delivery date: {context.requested_delivery_date ?? "None recorded"}.</p>
        <label className="flex gap-2 text-sm"><input type="checkbox" checked={attested} disabled={busy} onChange={(event) => setAttested(event.target.checked)} />
          I reviewed the manufacturing specifications and commercial summary for this exact quotation version and RFQ source digest. I confirm the requested delivery date shown above and that there are no contradictions in this context.
        </label>
      </>}
      {selected === "reject_quote" && <label className="block text-sm">Rejection reason
        <input className="mt-1 block w-full rounded border border-slate-300 p-2" value={reason} disabled={busy} maxLength={1000} onChange={(event) => setReason(event.target.value)} />
      </label>}
      <label className="flex gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />I understand this action and explicitly confirm {LABELS[selected].toLowerCase()} for version {quote.current_version}.</label>
      <div className="flex gap-2">
        <button type="submit" className="btn-primary" disabled={!canConfirm}>{busy ? "Recording action…" : `Confirm ${LABELS[selected]}`}</button>
        <button type="button" className="btn-secondary" disabled={busy} onClick={() => { setSelected(null); setAttested(false); setConfirmed(false); setReason(""); }}>Cancel</button>
      </div>
    </form>}
    {error !== null && <div className="mt-3"><ErrorPanel error={error} /></div>}
    {complete && <p role="status" className="mt-3 text-sm">Action recorded. Refresh the quotation to review its authoritative state.</p>}
  </section>;
}
