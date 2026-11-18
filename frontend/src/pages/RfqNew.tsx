import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiError, PART_TYPES, type PartSpecification, type RfqReceipt, type RfqSubmissionRequest } from "../api";
import { useApi } from "../auth";
import { PageHeader } from "../ui";

// Limits mirror schemas/rfq.py and schemas/common.py.
const MAX_PARTS = 50;
const MAX_RAW = 20_000;
const INT32_MAX = 2_147_483_647;

type TextKey = "part_id" | "material_id" | "finish_id";
type DecKey = "length_mm" | "width_mm" | "height_mm" | "thickness_mm" | "tolerance_mm" | "bend_radius_mm" | "weld_length_mm";
type IntKey = "quantity" | "hole_count";
type Draft = { uid: string; part_type: string } & Record<TextKey | DecKey | IntKey, string>;

const POSITIVE_DEC: DecKey[] = ["length_mm", "width_mm", "height_mm", "thickness_mm"];
const NONNEG_DEC: DecKey[] = ["tolerance_mm", "bend_radius_mm", "weld_length_mm"];
const LABELS: Record<TextKey | DecKey | IntKey, string> = {
  part_id: "Part ID", material_id: "Material ID", finish_id: "Finish ID",
  length_mm: "Length (mm)", width_mm: "Width (mm)", height_mm: "Height (mm)", thickness_mm: "Thickness (mm)",
  tolerance_mm: "Tolerance (mm)", bend_radius_mm: "Bend radius (mm)", weld_length_mm: "Weld length (mm)",
  quantity: "Quantity", hole_count: "Hole count",
};

const emptyPart = (): Draft => ({
  uid: crypto.randomUUID(), part_id: "", part_type: "", material_id: "", finish_id: "",
  length_mm: "", width_mm: "", height_mm: "", thickness_mm: "", tolerance_mm: "",
  bend_radius_mm: "", weld_length_mm: "", quantity: "", hole_count: "",
});

const blank = (v: string) => v.trim() === "";

function decimalError(v: string, positive: boolean): string | undefined {
  if (!/^\d+(\.\d+)?$/.test(v)) return "Enter a plain number such as 4 or 2.5.";
  const [whole, frac = ""] = v.split(".");
  if (frac.length > 3) return "At most 3 decimal places.";
  if (whole.replace(/^0+(?=\d)/, "").length + frac.length > 12) return "Too many digits.";
  if (positive && Number(v) === 0) return "Must be greater than 0.";
}

function intError(v: string, min: number): string | undefined {
  if (!/^\d+$/.test(v)) return "Enter a whole number.";
  const n = Number(v);
  if (n < min) return `Must be at least ${min}.`;
  if (n > INT32_MAX) return "Too large.";
}

/** Client-side mirror of the backend's structural rules. Keys match the API field paths. */
function validate(parts: Draft[], raw: string): Record<string, string> {
  const errors: Record<string, string> = {};
  const seen = new Set<string>();
  parts.forEach((p, i) => {
    const at = (k: string) => `parts.${i}.${k}`;
    if (blank(p.part_id)) errors[at("part_id")] = "Part ID is required.";
    else if (seen.has(p.part_id)) errors[at("part_id")] = "Part ID must be unique within the RFQ.";
    seen.add(p.part_id);
    for (const k of ["part_id", "material_id", "finish_id"] as const)
      if (p[k].length > 64) errors[at(k)] = "At most 64 characters.";
    for (const k of POSITIVE_DEC) if (!blank(p[k])) { const e = decimalError(p[k].trim(), true); if (e) errors[at(k)] = e; }
    for (const k of NONNEG_DEC) if (!blank(p[k])) { const e = decimalError(p[k].trim(), false); if (e) errors[at(k)] = e; }
    if (!blank(p.quantity)) { const e = intError(p.quantity.trim(), 1); if (e) errors[at("quantity")] = e; }
    if (!blank(p.hole_count)) { const e = intError(p.hole_count.trim(), 0); if (e) errors[at("hole_count")] = e; }
  });
  if (raw.length > MAX_RAW) errors.raw_request = `At most ${MAX_RAW.toLocaleString()} characters.`;
  return errors;
}

/** Omitted fields mean "not stated"; identifiers are sent verbatim (the API never trims). */
function toRequest(parts: Draft[], raw: string): RfqSubmissionRequest {
  return {
    parts: parts.map((p) => {
      const out: PartSpecification = { part_id: p.part_id };
      if (p.part_type) out.part_type = p.part_type as PartSpecification["part_type"];
      if (!blank(p.material_id)) out.material_id = p.material_id;
      if (!blank(p.finish_id)) out.finish_id = p.finish_id;
      for (const k of [...POSITIVE_DEC, ...NONNEG_DEC]) if (!blank(p[k])) out[k] = p[k].trim();
      if (!blank(p.quantity)) out.quantity = Number(p.quantity.trim());
      if (!blank(p.hole_count)) out.hole_count = Number(p.hole_count.trim());
      return out;
    }),
    ...(blank(raw) ? {} : { raw_request: raw }),
  };
}

function submitErrorMessage(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong.";
  if (e.status === 404) return "RFQ submission is not enabled on this server.";
  if (e.status === 403) return "Your account is not permitted to submit RFQs.";
  if (e.status === 0 || e.status === 503 || e.status >= 500)
    return `${e.message} Retrying is safe: the same submission key is reused, so no duplicate RFQ is created.`;
  return e.message;
}

export default function RfqNew() {
  const api = useApi();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [parts, setParts] = useState<Draft[]>([emptyPart()]);
  const [raw, setRaw] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  // One Idempotency-Key per distinct payload: an unchanged retry reuses it (the server
  // replays the stored receipt); any edit gets a fresh key, so it is never a 409 conflict.
  const attempt = useRef<{ key: string; body: string } | null>(null);

  const mutation = useMutation({
    mutationFn: async (body: string) => {
      if (attempt.current?.body !== body) attempt.current = { key: crypto.randomUUID(), body };
      return api<RfqReceipt>("/rfqs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": attempt.current.key },
        body,
      });
    },
    onSuccess: ({ data, headers }) => {
      queryClient.invalidateQueries({ queryKey: ["rfqs"] });
      navigate(`/rfqs/${encodeURIComponent(data.rfq_id)}`, {
        state: { submitted: true, replayed: headers.get("Idempotent-Replay") === "true" },
      });
    },
    onError: (e) => {
      if (!(e instanceof ApiError)) return;
      if (e.status === 409) attempt.current = null; // the key is spent; the next try gets a new one
      if (e.details.length) {
        const server: Record<string, string> = {};
        for (const d of e.details) server[d.field.replace(/^body\./, "")] ??= d.message;
        setErrors(server);
      }
    },
  });

  const update = (i: number, k: keyof Draft, v: string) =>
    setParts((ps) => ps.map((p, j) => (j === i ? { ...p, [k]: v } : p)));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const found = validate(parts, raw);
    setErrors(found);
    if (Object.keys(found).length) {
      document.querySelector<HTMLElement>("[aria-invalid=true]")?.focus();
      return;
    }
    mutation.mutate(JSON.stringify(toRequest(parts, raw)));
  };

  const field = (i: number, k: TextKey | DecKey | IntKey, extra: { inputMode?: "decimal" | "numeric"; required?: boolean } = {}) => {
    const id = `p${i}-${k}`;
    const err = errors[`parts.${i}.${k}`];
    return (
      <div key={k}>
        <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-600">
          {LABELS[k]}{extra.required && <span className="text-red-600" aria-hidden> *</span>}
        </label>
        <input id={id} className="input" value={parts[i][k]} onChange={(e) => update(i, k, e.target.value)}
          inputMode={extra.inputMode} required={extra.required} autoComplete="off"
          aria-invalid={!!err} aria-describedby={err ? `${id}-err` : undefined} />
        {err && <p id={`${id}-err`} className="mt-1 text-xs text-red-600">{err}</p>}
      </div>
    );
  };

  const formError = mutation.isError ? submitErrorMessage(mutation.error) : null;

  return (
    <>
      <PageHeader title="New RFQ" subtitle="Describe each part. Only Part ID is required; leave anything you don't know blank and our team will ask." />
      <form onSubmit={submit} noValidate className="space-y-6">
        {parts.map((p, i) => (
          <fieldset key={p.uid} className="card p-5">
            <div className="mb-4 flex items-center justify-between">
              <legend className="text-sm font-semibold text-slate-700">Part {i + 1}</legend>
              {parts.length > 1 && (
                <button type="button" className="text-sm text-red-600 hover:underline"
                  onClick={() => { setParts((ps) => ps.filter((_, j) => j !== i)); setErrors({}); }}>Remove</button>
              )}
            </div>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {field(i, "part_id", { required: true })}
              <div>
                <label htmlFor={`p${i}-part_type`} className="mb-1 block text-xs font-medium text-slate-600">Part type</label>
                <select id={`p${i}-part_type`} className="input" value={p.part_type} onChange={(e) => update(i, "part_type", e.target.value)}>
                  <option value="">Not specified</option>
                  {PART_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, " ")}</option>)}
                </select>
              </div>
              {field(i, "material_id")}
              {field(i, "finish_id")}
              {[...POSITIVE_DEC, ...NONNEG_DEC].map((k) => field(i, k, { inputMode: "decimal" }))}
              {field(i, "quantity", { inputMode: "numeric" })}
              {field(i, "hole_count", { inputMode: "numeric" })}
            </div>
          </fieldset>
        ))}

        <button type="button" className="btn-secondary" disabled={parts.length >= MAX_PARTS}
          onClick={() => setParts((ps) => [...ps, emptyPart()])}>+ Add part</button>

        <div className="card p-5">
          <label htmlFor="raw_request" className="mb-1 block text-sm font-semibold text-slate-700">Notes (optional)</label>
          <textarea id="raw_request" className="input" rows={4} value={raw} onChange={(e) => setRaw(e.target.value)}
            aria-invalid={!!errors.raw_request} aria-describedby={errors.raw_request ? "raw-err" : undefined} />
          {errors.raw_request && <p id="raw-err" className="mt-1 text-xs text-red-600">{errors.raw_request}</p>}
        </div>

        {formError && <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-4 text-sm text-red-800">{formError}</div>}

        <div className="flex justify-end gap-3">
          <button type="submit" className="btn-primary" disabled={mutation.isPending}>
            {mutation.isPending ? "Submitting…" : mutation.isError ? "Retry submission" : "Submit RFQ"}
          </button>
        </div>
      </form>
    </>
  );
}
