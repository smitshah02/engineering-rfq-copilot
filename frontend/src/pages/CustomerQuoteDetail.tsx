import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, type ReleasedQuote } from "../api";
import { useApi, useAuth } from "../auth";
import { ErrorPanel, Loading, PageHeader, formatDateTime, quoteExpired, quoteExpiry } from "../ui";

export default function CustomerQuoteDetail() {
  const { quoteId = "" } = useParams();
  const api = useApi();
  const { sessionId } = useAuth();
  const quote = useQuery({
    queryKey: ["customer-quote", sessionId, quoteId],
    queryFn: async ({ signal }) => (await api<ReleasedQuote>(`/quotes/${encodeURIComponent(quoteId)}`, { signal })).data,
    retry: false,
    refetchInterval: 30_000,
  });
  const error = quote.error instanceof ApiError && quote.error.status === 404
    ? new ApiError(404, "This quotation is unavailable to your account. Return to your RFQ history for available quotations.")
    : quote.error;
  return <>
    <Link to="/rfqs" className="text-sm text-indigo-600 hover:underline">← All RFQs</Link>
    <div className="mt-3" />
    {quote.isPending ? <Loading label="Loading quotation…" /> : quote.isError ? <ErrorPanel error={error} onRetry={() => quote.refetch()} /> : (
      <>
        <PageHeader title={quote.data.quote_id} subtitle={`Released ${formatDateTime(quote.data.released_at)}`}
          action={<span className="rounded-full bg-emerald-50 px-3 py-1 text-sm font-medium text-emerald-800">Released</span>} />
        {quoteExpired(quote.data.expiry_date) && <p role="status" className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">This quotation has expired. Contact our team for an updated quotation.</p>}
        <section className="card mb-6 p-4" aria-labelledby="customer-quotation-facts">
          <h2 id="customer-quotation-facts" className="mb-4 font-semibold">Quotation details</h2>
          <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
            <div><dt className="text-slate-500">Related RFQ</dt><dd className="mt-1 break-all font-mono"><Link to={`/rfqs/${encodeURIComponent(quote.data.rfq_id)}`} className="text-indigo-600 hover:underline">{quote.data.rfq_id}</Link></dd></div>
            <div><dt className="text-slate-500">Quotation version</dt><dd className="mt-1">{quote.data.version}</dd></div>
            <div><dt className="text-slate-500">Total price</dt><dd className="mt-1 font-semibold">{quote.data.total_usd} {quote.data.currency}</dd></div>
            <div><dt className="text-slate-500">Valid through (UTC)</dt><dd className="mt-1">{quoteExpiry(quote.data.expiry_date)}</dd></div>
            <div><dt className="text-slate-500">Estimated lead time</dt><dd className="mt-1">{quote.data.estimated_lead_business_days === null ? "Not specified" : `${quote.data.estimated_lead_business_days} business days`}</dd></div>
          </dl>
        </section>
        <section className="card overflow-x-auto" aria-labelledby="customer-quotation-lines">
          <h2 id="customer-quotation-lines" className="border-b border-slate-200 px-4 py-3 font-semibold">Line items</h2>
          {quote.data.lines.length === 0 ? <p className="p-4 text-sm text-slate-500">No line items are available for this quotation.</p> : (
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>{["Line", "Part", "Quantity", "Line total"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {quote.data.lines.map((line) => <tr key={line.line_number}>
                  <td className="px-4 py-3">{line.line_number}</td><td className="px-4 py-3 font-mono">{line.part_id}</td>
                  <td className="px-4 py-3">{line.quantity}</td><td className="whitespace-nowrap px-4 py-3">{line.line_total_usd} {quote.data.currency}</td>
                </tr>)}
              </tbody>
            </table>
          )}
        </section>
      </>
    )}
  </>;
}
