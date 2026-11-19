// Types mirror src/rfq_copilot/api/schemas/{reads,rfq,common}.py exactly.
// Decimals travel as plain decimal strings ("4.0") or integers, never floats.

export type PartType =
  | "flat_panel" | "mounting_plate" | "l_bracket" | "u_bracket" | "enclosure" | "fabricated_assembly";
export const PART_TYPES: PartType[] = [
  "flat_panel", "mounting_plate", "l_bracket", "u_bracket", "enclosure", "fabricated_assembly",
];

export interface PartSpecification {
  part_id: string;
  part_type?: PartType | null;
  material_id?: string | null;
  finish_id?: string | null;
  length_mm?: string | null;
  width_mm?: string | null;
  height_mm?: string | null;
  thickness_mm?: string | null;
  tolerance_mm?: string | null;
  bend_radius_mm?: string | null;
  weld_length_mm?: string | null;
  quantity?: number | null;
  hole_count?: number | null;
}
export interface RfqSubmissionRequest {
  parts: PartSpecification[];
  requested_delivery_date?: string | null;
  raw_request?: string | null;
}
export interface RfqReceipt { rfq_id: string; status: "draft" }
export interface ReleasedQuoteReference {
  quote_id: string; version: number; released_at: string; expiry_date: string | null;
}
export interface RfqSummary { rfq_id: string; status: string; submitted_at: string; released_quote?: ReleasedQuoteReference | null }
export interface RfqPage { items: RfqSummary[]; limit: number; offset: number; has_more: boolean }
export interface RfqDetail extends RfqSummary { lines: { line_number: number; part: PartSpecification }[] }
// Customer contract only; no staff snapshots, costs, decisions or audit information.
export interface ReleasedQuote {
  quote_id: string; rfq_id: string; status: "released"; released_at: string; version: number;
  currency: string; total_usd: string; expiry_date: string | null;
  estimated_lead_business_days: number | null;
  lines: { line_number: number; part_id: string; quantity: number; line_total_usd: string }[];
}
export interface CustomerProfile {
  customer_id: string; company_name: string; industry: string; city: string; state: string;
  country: string; payment_terms: string; preferred_shipping: string;
  contacts: { contact_name: string; email: string; phone: string; is_primary: boolean }[];
}
export interface ValidationIssue { field: string; code: string; message: string }

// Mirrors api/schemas/staff_quotes.py. These contracts never enter customer dashboards.
export const QUOTE_STATUSES = [
  "draft", "revised_draft", "pending_approval", "approved", "rejected", "expired",
  "clarification_required", "blocked", "released",
] as const;
export type QuoteStatus = typeof QUOTE_STATUSES[number];
export interface StaffQuoteSummary {
  quote_id: string; rfq_id: string; status: QuoteStatus; current_version: number;
  created_at: string; expiry_date: string | null; currency: string; total_usd: string | null;
}
export interface StaffQuotePage { items: StaffQuoteSummary[]; limit: number; offset: number; has_more: boolean }
export interface StaffQuoteDetail extends Omit<StaffQuoteSummary, "total_usd"> {
  total_usd: string; quote_version_id: string; version_created_at: string;
  estimated_lead_business_days: number | null; rfq: RfqSummary;
  lines: { line_number: number; part_id: string; quantity: number; line_total_usd: string; part: PartSpecification }[];
}

export type QuoteAction = "request_approval" | "approve_quote" | "reject_quote" | "release_quote";
export interface StaffQuoteActions {
  quote_id: string; expected_version: number;
  actions: Record<QuoteAction, { allowed: boolean; reason: string | null }>;
  attestation_context: { rfq_source_sha256: string; requested_delivery_date: string | null } | null;
  reviews: { kind: string; reasons: string[] }[];
}
export interface QuoteActionResult {
  quote_id: string; version: number; action: "submitted" | "approved" | "rejected" | "released";
  status: QuoteStatus; replayed: boolean;
}

const ACTION_ERRORS: Record<string, string> = {
  stale_version: "The quotation version changed. Refresh and review the current version.",
  idempotency_key_conflict: "This action key was already used with different information. Refresh the quotation.",
  separation_of_duties: "An independent authorized person must perform this action.",
  quote_expired: "This quotation has expired. A new revision is required.",
  review_unverified: "Required engineering review has not been verified.",
  not_approvable: "Approval is blocked by policy or changed evidence. Review the current quotation.",
  not_submittable: "This quotation cannot be submitted under the current policy and evidence.",
  not_releasable: "Release is blocked by policy or changed evidence.",
  not_assessable: "This action is unavailable for the current quotation state.",
  not_pending: "This version is no longer pending review.",
  resubmission_blocked: "This rejected quotation requires an eligible revision before resubmission.",
  attestation_mismatch: "The attestation does not match the current quotation. Refresh and review it again.",
  permission_denied: "Your account is not permitted to do this.",
  not_found: "Not found.",
  invalid_request: "The action request was invalid.",
};
export const quoteActionReason = (reason: string | null) =>
  reason ? ACTION_ERRORS[reason] ?? "This action is unavailable. Review the current quotation." : "";

// Mirrors src/rfq_copilot/api/schemas/attachments.py.
export interface AttachmentMetadata {
  attachment_id: string; rfq_id: string; filename: string;
  document_type: "pdf" | "csv" | "md"; revision: string | null; sha256: string;
}
export interface AttachmentList { items: AttachmentMetadata[] }
export interface PdfExtraction {
  kind: "pdf"; status: "text" | "no_text"; page_count: number;
  pages: { page_number: number; text: string }[]; truncated: boolean;
}
export interface BomExtraction { kind: "bom"; rows: Record<string, string | number | null>[] }
export interface AttachmentUploadResponse extends AttachmentMetadata {
  size_bytes: number; extraction: PdfExtraction | BomExtraction;
}
/** One CSV problem from an ATTACHMENT_REJECTED body; row is 1-based, null for the header. */
export interface BomIssue { row: number | null; column: string | null; code: string }

/** The shape of `useApi()`. */
export type ApiCall = <T>(path: string, init?: RequestInit) => Promise<{ data: T; headers: Headers }>;

const attachmentsPath = (rfqId: string) => `/rfqs/${encodeURIComponent(rfqId)}/attachments`;

export const listAttachments = async (api: ApiCall, rfqId: string) =>
  (await api<AttachmentList>(attachmentsPath(rfqId))).data;

/** Exactly one multipart field named "file"; the browser sets the multipart Content-Type. */
export async function uploadAttachment(api: ApiCall, rfqId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return (await api<AttachmentUploadResponse>(attachmentsPath(rfqId), { method: "POST", body: form })).data;
}

/** A failed API call. `message` is safe to show: it is the backend's static text or ours. */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public reason?: string,
    public details: ValidationIssue[] = [],
    public issues: BomIssue[] = [],
  ) {
    super(message);
  }
}

const FALLBACK: Record<number, string> = {
  401: "Your session is missing or expired. Enter a new token.",
  403: "Your account is not permitted to do this.",
  404: "Not found.",
  409: "This request conflicts with an earlier submission.",
  413: "The file is too large.",
  422: "The request was invalid.",
  503: "The service is temporarily unavailable. Try again shortly.",
};

export async function apiRequest<T>(
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<{ data: T; headers: Headers }> {
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      ...init,
      // Authenticated data must be fetched again after logout or identity changes.
      cache: "no-store",
      headers: { Accept: "application/json", Authorization: `Bearer ${token}`, ...init.headers },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check your connection and try again.");
  }
  if (res.ok) return { data: (await res.json()) as T, headers: res.headers };

  let body: { message?: unknown; reason?: unknown; detail?: unknown; details?: unknown; issues?: unknown } = {};
  try { body = await res.json(); } catch { /* non-JSON error body */ }
  // Only the structured RFQ/validation contracts carry customer-facing text; for anything
  // else (e.g. FastAPI's plain {"detail": ...}) use our own fixed message.
  const structured = !path.startsWith("/staff/quotes") && typeof body.message === "string";
  const lifecycle = path.startsWith("/staff/quotes/") || path.startsWith("/quote-decisions/");
  const actionReason = lifecycle && typeof body.detail === "string" && Object.hasOwn(ACTION_ERRORS, body.detail)
    ? body.detail : undefined;
  throw new ApiError(
    res.status,
    actionReason ? ACTION_ERRORS[actionReason] : structured && !lifecycle ? (body.message as string) : FALLBACK[res.status] ?? `Request failed (${res.status}).`,
    actionReason ?? (typeof body.reason === "string" ? body.reason : undefined),
    Array.isArray(body.details) ? (body.details as ValidationIssue[]) : [],
    Array.isArray(body.issues) ? (body.issues as BomIssue[]) : [],
  );
}
