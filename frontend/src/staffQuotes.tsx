import { useQuery } from "@tanstack/react-query";
import { Outlet } from "react-router-dom";
import { ApiError, type QuoteStatus, type StaffQuotePage } from "./api";
import { useApi, useAuth } from "./auth";
import { ErrorPanel, Loading } from "./ui";

// A successful protected list request validates the current database identity, binding,
// role and read grant, including accounts with zero visible quotations. No JWT role claims.
export function useStaffAccess() {
  const api = useApi();
  const { sessionId } = useAuth();
  return useQuery({
    queryKey: ["staff-quote-access", sessionId],
    queryFn: async ({ signal }) => {
      const { data } = await api<StaffQuotePage>("/staff/quotes?limit=1", { signal });
      if (!Array.isArray(data.items) || data.limit !== 1 || typeof data.has_more !== "boolean")
        throw new ApiError(503, "The service is temporarily unavailable. Try again shortly.");
      return true;
    },
    retry: false,
    refetchOnWindowFocus: true,
  });
}

export function StaffQuoteAccess() {
  const access = useStaffAccess();
  if (access.isPending) return <Loading label="Checking staff access…" />;
  if (access.isError) return <ErrorPanel error={access.error} onRetry={() => access.refetch()} />;
  return <Outlet />;
}

const LABELS: Record<QuoteStatus, string> = {
  draft: "Draft", revised_draft: "Revised draft", pending_approval: "Pending approval",
  approved: "Approved", rejected: "Rejected", expired: "Expired",
  clarification_required: "Clarification required", blocked: "Blocked", released: "Released",
};
export const quoteStatusLabel = (status: QuoteStatus) => LABELS[status] ?? "Status unavailable";

export function QuoteStatusBadge({ status }: { status: QuoteStatus }) {
  return <span className="inline-flex whitespace-nowrap rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 ring-1 ring-inset ring-indigo-200">{quoteStatusLabel(status)}</span>;
}

export function InternalQuoteNotice({ status }: { status?: QuoteStatus }) {
  if (status === "released") return <p className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">This quotation has been released to the customer.</p>;
  return <p className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">Internal quotation. Non-binding until authorized release. This view shows stored quotation facts only.</p>;
}

// Preserve exact decimal strings; do not round or convert commercial amounts to floats.
export const commercialTotal = (amount: string | null, currency: string) => amount === null ? "Not available" : `${amount} ${currency}`;
export const expiryDate = (iso: string | null) => iso === null ? "Not specified" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" }).format(new Date(`${iso}T00:00:00Z`));
