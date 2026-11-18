import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import type { RfqPage } from "../api";
import { useApi } from "../auth";
import { ErrorPanel, Loading, PageHeader, RfqTable } from "../ui";

const LIMIT = 20;

export default function RfqList() {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const q = useQuery({
    queryKey: ["rfqs", offset, LIMIT],
    queryFn: async () => (await api<RfqPage>(`/rfqs?limit=${LIMIT}&offset=${offset}`)).data,
    placeholderData: keepPreviousData,
  });
  const go = (o: number) => setParams(o ? { offset: String(o) } : {});

  return (
    <>
      <PageHeader title="RFQs" subtitle="All requests for quotation from your company, newest first."
        action={<Link to="/rfqs/new" className="btn-primary">New RFQ</Link>} />
      <div className="card overflow-x-auto">
        {q.isPending ? <Loading /> : q.isError ? <div className="p-4"><ErrorPanel error={q.error} onRetry={() => q.refetch()} /></div> : <RfqTable items={q.data.items} />}
      </div>
      {q.data && (offset > 0 || q.data.has_more) && (
        <nav aria-label="Pagination" className="mt-4 flex items-center justify-between text-sm">
          <button className="btn-secondary" disabled={offset === 0} onClick={() => go(Math.max(0, offset - LIMIT))}>Previous</button>
          <span className="text-slate-500">Showing {offset + 1}–{offset + q.data.items.length}</span>
          <button className="btn-secondary" disabled={!q.data.has_more} onClick={() => go(offset + LIMIT)}>Next</button>
        </nav>
      )}
    </>
  );
}
