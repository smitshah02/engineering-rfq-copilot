import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "./api";

// The one customer-facing presentation of the stored RFQ status (business.rfqs rule_01).
// It describes the RFQ record only: never workflow, agent, approval or quotation progress.
// Unknown values never reach the screen raw.
const STATUS: Record<string, { label: string; style: string }> = {
  draft: { label: "Received", style: "bg-blue-50 text-blue-700 ring-blue-200" },
  intake: { label: "Under review", style: "bg-indigo-50 text-indigo-700 ring-indigo-200" },
  validated: { label: "Under review", style: "bg-indigo-50 text-indigo-700 ring-indigo-200" },
  blocked: { label: "Unable to proceed", style: "bg-amber-50 text-amber-800 ring-amber-200" },
  closed: { label: "Closed", style: "bg-slate-100 text-slate-600 ring-slate-300" },
  archived: { label: "Archived", style: "bg-slate-100 text-slate-500 ring-slate-200" },
};
const UNKNOWN_STATUS = { label: "Status unavailable", style: "bg-slate-100 text-slate-600 ring-slate-300" };

export const statusLabel = (status: string) => (STATUS[status] ?? UNKNOWN_STATUS).label;

export function StatusBadge({ status }: { status: string }) {
  const { label, style } = STATUS[status] ?? UNKNOWN_STATUS;
  return (
    <span className={`inline-flex whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${style}`}>
      {label}
    </span>
  );
}

export const formatDateTime = (iso: string) =>
  new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center gap-3 p-6 text-sm text-slate-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const status = error instanceof ApiError ? error.status : 0;
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  const transient = status === 0 || status === 503;
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <p className="font-medium">{status === 404 ? "Not found" : status === 403 ? "Access denied" : "Could not load data"}</p>
      <p className="mt-1">{message}</p>
      {transient && onRetry && (
        <button className="btn-secondary mt-3" onClick={onRetry}>Try again</button>
      )}
    </div>
  );
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function RfqTable({ items }: { items: { rfq_id: string; status: string; submitted_at: string }[] }) {
  if (items.length === 0)
    return <p className="p-6 text-sm text-slate-500">No RFQs yet. <Link to="/rfqs/new" className="text-indigo-600 hover:underline">Submit your first RFQ</Link>.</p>;
  return (
    <table className="w-full text-left text-sm">
      <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
        <tr><th className="px-4 py-3 font-medium">RFQ</th><th className="px-4 py-3 font-medium">Status</th><th className="px-4 py-3 font-medium">Submitted</th></tr>
      </thead>
      <tbody className="divide-y divide-slate-100">
        {items.map((r) => (
          <tr key={r.rfq_id} className="hover:bg-slate-50">
            <td className="px-4 py-3 font-mono"><Link to={`/rfqs/${encodeURIComponent(r.rfq_id)}`} className="text-indigo-600 hover:underline">{r.rfq_id}</Link></td>
            <td className="px-4 py-3"><StatusBadge status={r.status} /></td>
            <td className="px-4 py-3 text-slate-500">{formatDateTime(r.submitted_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
