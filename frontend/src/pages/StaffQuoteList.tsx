import { useEffect, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { QUOTE_STATUSES, type QuoteStatus, type StaffQuotePage } from "../api";
import { useApi, useAuth } from "../auth";
import { InternalQuoteNotice, QuoteStatusBadge, commercialTotal, expiryDate, quoteStatusLabel } from "../staffQuotes";
import { ErrorPanel, Loading, PageHeader, formatDateTime } from "../ui";

const LIMIT = 20;
const MAX_OFFSET = 10_000;

export default function StaffQuoteList() {
  const api = useApi();
  const { sessionId } = useAuth();
  const [params, setParams] = useSearchParams();
  const rawOffset = Number(params.get("offset") ?? 0);
  const offset = Number.isInteger(rawOffset) && rawOffset >= 0 && rawOffset <= MAX_OFFSET ? rawOffset : 0;
  const rawStatus = params.get("status");
  const status = QUOTE_STATUSES.includes(rawStatus as QuoteStatus) ? rawStatus as QuoteStatus : "";
  const rawSearch = params.get("q") ?? "";
  const search = rawSearch.trim() && rawSearch.length <= 64 && !rawSearch.includes("\0") ? rawSearch : "";
  const [searchInput, setSearchInput] = useState(search);
  useEffect(() => setSearchInput(search), [search]);
  const query = new URLSearchParams({ limit: String(LIMIT), offset: String(offset) });
  if (status) query.set("status", status);
  if (search) query.set("q", search);
  const page = useQuery({
    queryKey: ["staff-quotes", sessionId, offset, status, search],
    queryFn: async ({ signal }) => (await api<StaffQuotePage>(`/staff/quotes?${query}`, { signal })).data,
    retry: false,
  });
  const go = (nextOffset: number, nextStatus = status, nextSearch = search) => {
    const next = new URLSearchParams();
    if (nextOffset) next.set("offset", String(nextOffset));
    if (nextStatus) next.set("status", nextStatus);
    if (nextSearch) next.set("q", nextSearch);
    setParams(next);
  };
  const submit = (event: FormEvent) => { event.preventDefault(); go(0, status, searchInput.trim()); };

  return (
    <>
      <PageHeader title="Staff quotations" subtitle="Quotations available to your current customer scope and RFQ assignments." />
      <InternalQuoteNotice />
      <div className="mb-4 flex flex-wrap items-end gap-4">
        <div>
          <label htmlFor="quote-status" className="mb-1 block text-sm font-medium">Quotation status</label>
          <select id="quote-status" className="input" value={status} onChange={(event) => go(0, event.target.value as QuoteStatus | "")}>
            <option value="">All statuses</option>
            {QUOTE_STATUSES.map((value) => <option key={value} value={value}>{quoteStatusLabel(value)}</option>)}
          </select>
        </div>
        <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
          <div>
            <label htmlFor="quote-search" className="mb-1 block text-sm font-medium">Exact quotation or RFQ identifier</label>
            <input id="quote-search" className="input" maxLength={64} value={searchInput} onChange={(event) => setSearchInput(event.target.value)} />
          </div>
          <button className="btn-secondary" type="submit">Search</button>
          {search && <button className="btn-secondary" type="button" onClick={() => { setSearchInput(""); go(0, status, ""); }}>Clear search</button>}
        </form>
      </div>
      <div className="card overflow-x-auto">
        {page.isPending ? <Loading label="Loading quotations…" /> : page.isError ? <div className="p-4"><ErrorPanel error={page.error} onRetry={() => page.refetch()} /></div> : page.data.items.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">No quotations found in your authorized scope for these filters.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>{["Quotation", "RFQ", "Status", "Version", "Created", "Expiry", "Commercial total", "Details"].map((label) => <th key={label} className="whitespace-nowrap px-4 py-3 font-medium">{label}</th>)}</tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {page.data.items.map((quote) => (
                <tr key={quote.quote_id} className="hover:bg-slate-50">
                  <td className="px-4 py-3 font-mono">{quote.quote_id}</td>
                  <td className="px-4 py-3 font-mono">{quote.rfq_id}</td>
                  <td className="px-4 py-3"><QuoteStatusBadge status={quote.status} /></td>
                  <td className="px-4 py-3">{quote.current_version}</td>
                  <td className="whitespace-nowrap px-4 py-3">{formatDateTime(quote.created_at)}</td>
                  <td className="whitespace-nowrap px-4 py-3">{expiryDate(quote.expiry_date)}</td>
                  <td className="whitespace-nowrap px-4 py-3">{commercialTotal(quote.total_usd, quote.currency)}</td>
                  <td className="px-4 py-3"><Link className="whitespace-nowrap text-indigo-600 hover:underline" to={`/staff/quotes/${encodeURIComponent(quote.quote_id)}`} aria-label={`View details for ${quote.quote_id}`}>View Details</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <nav aria-label="Quotation pagination" className="mt-4 flex items-center justify-between gap-3 text-sm">
        <button className="btn-secondary" disabled={page.isFetching || offset === 0} onClick={() => go(Math.max(0, offset - LIMIT))}>Previous</button>
        <span className="text-slate-500">{page.isSuccess && page.data.items.length > 0 ? `Showing ${offset + 1}–${offset + page.data.items.length}` : ""}</span>
        <button className="btn-secondary" disabled={page.isFetching || !page.isSuccess || !page.data.has_more || offset + LIMIT > MAX_OFFSET} onClick={() => go(offset + LIMIT)}>Next</button>
      </nav>
      {offset + LIMIT > MAX_OFFSET && <p className="mt-2 text-sm text-slate-500">Page limit reached. Narrow the status or identifier filter.</p>}
    </>
  );
}
