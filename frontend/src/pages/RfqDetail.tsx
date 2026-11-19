import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, listAttachments, uploadAttachment, type BomIssue, type PartSpecification, type RfqDetail as Detail } from "../api";
import { useApi } from "../auth";
import { ErrorPanel, Loading, PageHeader, ReleasedQuotationLink, StatusBadge, formatDateTime, statusLabel } from "../ui";

const COLUMNS: [keyof PartSpecification, string][] = [
  ["part_id", "Part"], ["part_type", "Type"], ["material_id", "Material"], ["finish_id", "Finish"],
  ["length_mm", "L mm"], ["width_mm", "W mm"], ["height_mm", "H mm"], ["thickness_mm", "T mm"],
  ["tolerance_mm", "Tol mm"], ["bend_radius_mm", "Bend R mm"], ["weld_length_mm", "Weld mm"],
  ["hole_count", "Holes"], ["quantity", "Qty"],
];

export default function RfqDetail() {
  const { rfqId = "" } = useParams();
  const api = useApi();
  const flash = useLocation().state as { submitted?: boolean; replayed?: boolean } | null;
  const q = useQuery({
    queryKey: ["rfq", rfqId],
    queryFn: async ({ signal }) => (await api<Detail>(`/rfqs/${encodeURIComponent(rfqId)}`, { signal })).data,
    refetchInterval: 30_000,
  });

  return (
    <>
      <Link to="/rfqs" className="text-sm text-indigo-600 hover:underline">← All RFQs</Link>
      <div className="mt-3" />
      {flash?.submitted && (
        <p role="status" className="mb-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
          {flash.replayed
            ? "This RFQ was already received. Showing the stored submission; no additional submission is required."
            : "Your RFQ was received successfully. No additional submission is required. A quotation will be shared only after authorized review, approval, and release."}
        </p>
      )}
      {q.isPending ? <Loading /> : q.isError ? <ErrorPanel error={q.error} onRetry={() => q.refetch()} /> : (
        <>
          <PageHeader title={q.data.rfq_id} subtitle={`Submitted ${formatDateTime(q.data.submitted_at)}`} action={<StatusBadge status={q.data.status} />} />
          <p className="mb-4 rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-600">
            Status: <span className="font-medium text-slate-800">{statusLabel(q.data.status)}</span>.{" "}
            A quotation is shared with you only after it has been approved by our team and released.
          </p>
          {q.data.released_quote && <div className="mb-4"><ReleasedQuotationLink quote={q.data.released_quote} /></div>}
          <section className="card overflow-x-auto" aria-labelledby="parts-h">
            <h2 id="parts-h" className="border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700">Parts</h2>
            {q.data.lines.length === 0 ? (
              <p className="p-4 text-sm text-slate-500">No part lines are available for this RFQ (RFQs imported from history have none).</p>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                  <tr><th className="px-3 py-2 font-medium">#</th>{COLUMNS.map(([, h]) => <th key={h} className="whitespace-nowrap px-3 py-2 font-medium">{h}</th>)}</tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {q.data.lines.map((l) => (
                    <tr key={l.line_number}>
                      <td className="px-3 py-2 text-slate-500">{l.line_number}</td>
                      {COLUMNS.map(([k]) => <td key={k} className="whitespace-nowrap px-3 py-2">{l.part[k] ?? <span className="text-slate-400">—</span>}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <Attachments rfqId={q.data.rfq_id} />
        </>
      )}
    </>
  );
}

// Client checks are a convenience only; the backend's own validation is authoritative.
const MAX_BYTES = 10 * 1024 * 1024; // backend default ApiSettings.max_upload_bytes
const ALLOWED = /\.(pdf|csv)$/i;
const ISSUE_TEXT: Record<string, string> = {
  missing_value: "missing value", invalid_number: "invalid number", invalid_integer: "invalid whole number",
  invalid_text: "invalid text", formula_like: "looks like a formula", too_long: "too long",
  wrong_field_count: "wrong number of columns",
};
const issueText = (i: BomIssue) =>
  `${i.row === null ? "Header" : `Row ${i.row}`}${i.column ? `, ${i.column}` : ""}: ${ISSUE_TEXT[i.code] ?? i.code}`;

function Attachments({ rfqId }: { rfqId: string }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [clientError, setClientError] = useState<string | null>(null);
  const [uploaded, setUploaded] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["attachments", rfqId], queryFn: () => listAttachments(api, rfqId) });
  const upload = useMutation({
    mutationFn: (f: File) => uploadAttachment(api, rfqId, f),
    onSuccess: (data) => {
      setUploaded(data.filename);
      setFile(null);
      if (input.current) input.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["attachments", rfqId] });
    },
  });

  const choose = (f: File | null) => {
    setUploaded(null);
    upload.reset();
    setFile(f);
    setClientError(
      !f ? null
        : !ALLOWED.test(f.name) ? "Only PDF and CSV files are accepted."
        : f.size === 0 ? "The file is empty."
        : f.size > MAX_BYTES ? "The file is larger than 10 MiB." : null,
    );
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (file && !clientError && !upload.isPending) upload.mutate(file);
  };
  const err = upload.error;
  const issues = err instanceof ApiError ? err.issues : [];

  return (
    <section className="card mt-6" aria-labelledby="att-h">
      <h2 id="att-h" className="border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-700">Attachments</h2>
      {list.isPending ? <Loading label="Loading attachments…" /> : list.isError ? (
        <div className="p-4"><ErrorPanel error={list.error} onRetry={() => list.refetch()} /></div>
      ) : list.data.items.length === 0 ? (
        <p className="p-4 text-sm text-slate-500">No attachments yet.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {list.data.items.map((a) => (
            <li key={a.attachment_id} className="flex flex-col gap-1 px-4 py-3 text-sm sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <p className="truncate font-medium">{a.filename}</p>
                <p className="truncate font-mono text-xs text-slate-400" title={a.sha256}>SHA-256 {a.sha256.slice(0, 16)}…</p>
              </div>
              <div className="flex shrink-0 items-center gap-2 text-xs text-slate-500">
                <span className="rounded bg-slate-100 px-2 py-0.5 font-medium uppercase text-slate-700">{a.document_type}</span>
                {a.revision && <span>Rev {a.revision}</span>}
                <span className="font-mono">{a.attachment_id}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={submit} className="space-y-3 border-t border-slate-200 p-4">
        <div>
          <label htmlFor="att-file" className="mb-1 block text-sm font-medium">Add a drawing (PDF) or bill of materials (CSV)</label>
          <input id="att-file" ref={input} type="file" accept=".pdf,.csv,application/pdf,text/csv"
            className="block w-full text-sm text-slate-600 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-sm file:font-medium hover:file:bg-slate-200"
            disabled={upload.isPending} aria-invalid={!!clientError} aria-describedby="att-help"
            onChange={(e) => choose(e.target.files?.[0] ?? null)} />
          <p id="att-help" className={`mt-1 text-xs ${clientError ? "text-red-600" : "text-slate-500"}`}>
            {clientError ?? "One file at a time, up to 10 MiB."}
          </p>
        </div>
        {uploaded && <p role="status" className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">Uploaded {uploaded}.</p>}
        {err && (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
            <p>{err instanceof ApiError ? err.message : "Upload failed."}</p>
            {issues.length > 0 && (
              <ul className="mt-2 list-disc space-y-0.5 pl-5 text-xs">
                {issues.slice(0, 10).map((i, n) => <li key={n}>{issueText(i)}</li>)}
                {issues.length > 10 && <li>…and {issues.length - 10} more</li>}
              </ul>
            )}
          </div>
        )}
        <button type="submit" className="btn-primary" disabled={!file || !!clientError || upload.isPending}>
          {upload.isPending ? "Uploading…" : "Upload"}
        </button>
      </form>
    </section>
  );
}
