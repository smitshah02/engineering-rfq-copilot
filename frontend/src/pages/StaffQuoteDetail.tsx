import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import type { PartSpecification, StaffQuoteDetail as Detail } from "../api";
import { useApi, useAuth } from "../auth";
import { InternalQuoteNotice, QuoteStatusBadge, commercialTotal, expiryDate } from "../staffQuotes";
import { ErrorPanel, Loading, PageHeader, StatusBadge, formatDateTime } from "../ui";
import StaffQuoteActions from "./StaffQuoteActions";

const SPECIFICATIONS: [keyof PartSpecification, string][] = [
  ["part_type", "Type"], ["material_id", "Material"], ["finish_id", "Finish"],
  ["length_mm", "Length (mm)"], ["width_mm", "Width (mm)"], ["height_mm", "Height (mm)"],
  ["thickness_mm", "Thickness (mm)"], ["tolerance_mm", "Tolerance (mm)"],
  ["bend_radius_mm", "Bend radius (mm)"], ["weld_length_mm", "Weld length (mm)"], ["hole_count", "Holes"],
];

export default function StaffQuoteDetail() {
  const { quoteId = "" } = useParams();
  const api = useApi();
  const { sessionId } = useAuth();
  const quote = useQuery({
    queryKey: ["staff-quote", sessionId, quoteId],
    queryFn: async ({ signal }) => (await api<Detail>(`/staff/quotes/${encodeURIComponent(quoteId)}`, { signal })).data,
    retry: false,
  });
  return (
    <>
      <Link to="/staff/quotes" className="text-sm text-indigo-600 hover:underline">← All staff quotations</Link>
      <div className="mt-3" />
      {quote.isPending ? <Loading label="Loading quotation…" /> : quote.isError ? <ErrorPanel error={quote.error} onRetry={() => quote.refetch()} /> : (
        <>
          <PageHeader title={quote.data.quote_id} subtitle={`Created ${formatDateTime(quote.data.created_at)}`} action={<QuoteStatusBadge status={quote.data.status} />} />
          <InternalQuoteNotice status={quote.data.status} />
          <section className="card mb-6 p-4" aria-labelledby="quotation-facts">
            <h2 id="quotation-facts" className="mb-4 font-semibold">Quotation details</h2>
            <dl className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
              <div><dt className="text-slate-500">Related RFQ</dt><dd className="mt-1 break-all font-mono">{quote.data.rfq.rfq_id}</dd></div>
              <div><dt className="text-slate-500">RFQ status</dt><dd className="mt-1"><StatusBadge status={quote.data.rfq.status} /></dd></div>
              <div><dt className="text-slate-500">RFQ submitted</dt><dd className="mt-1">{formatDateTime(quote.data.rfq.submitted_at)}</dd></div>
              <div><dt className="text-slate-500">Current version</dt><dd className="mt-1">{quote.data.current_version}</dd></div>
              <div><dt className="text-slate-500">Version identifier</dt><dd className="mt-1 break-all font-mono">{quote.data.quote_version_id}</dd></div>
              <div><dt className="text-slate-500">Version created</dt><dd className="mt-1">{formatDateTime(quote.data.version_created_at)}</dd></div>
              <div><dt className="text-slate-500">Commercial total</dt><dd className="mt-1 font-semibold">{commercialTotal(quote.data.total_usd, quote.data.currency)}</dd></div>
              <div><dt className="text-slate-500">Expiry</dt><dd className="mt-1">{expiryDate(quote.data.expiry_date)}</dd></div>
              <div><dt className="text-slate-500">Estimated lead time</dt><dd className="mt-1">{quote.data.estimated_lead_business_days === null ? "Not specified" : `${quote.data.estimated_lead_business_days} business days`}</dd></div>
            </dl>
          </section>
          <StaffQuoteActions key={`${sessionId}:${quoteId}:${quote.data.current_version}:${quote.data.status}`} quote={quote.data} refresh={() => quote.refetch()} />
          <section className="card overflow-x-auto" aria-labelledby="priced-lines">
            <h2 id="priced-lines" className="border-b border-slate-200 px-4 py-3 font-semibold">Priced line items and manufacturing specifications</h2>
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>{["Line", "Part", "Quantity", "Line total", ...SPECIFICATIONS.map(([, label]) => label)].map((label) => <th key={label} className="whitespace-nowrap px-3 py-3 font-medium">{label}</th>)}</tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {quote.data.lines.map((line) => (
                  <tr key={line.line_number}>
                    <td className="px-3 py-3">{line.line_number}</td>
                    <td className="px-3 py-3 font-mono">{line.part_id}</td>
                    <td className="px-3 py-3">{line.quantity}</td>
                    <td className="whitespace-nowrap px-3 py-3">{commercialTotal(line.line_total_usd, quote.data.currency)}</td>
                    {SPECIFICATIONS.map(([field]) => <td key={field} className="whitespace-nowrap px-3 py-3">{line.part[field] ?? "—"}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </>
  );
}
