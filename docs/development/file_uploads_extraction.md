# Attachment uploads and document extraction

The main FastAPI application supports authenticated PDF and CSV BOM upload plus attachment
metadata listing. This guide describes the current contract; the filename is retained for
existing links. Original implementation and verification history is archived privately.

## Endpoints and authorization

| Method / route | Input | Output |
| --- | --- | --- |
| `POST /api/v1/rfqs/{rfq_id}/attachments` | `multipart/form-data`, exactly one file field named `file` | `201`, attachment metadata and typed PDF/BOM extraction |
| `GET /api/v1/rfqs/{rfq_id}/attachments` | RFQ identifier | Attachment metadata list |

Both routes require verified identity and an eligible customer binding. Listing requires
`view_assigned_rfqs`. Upload requires `create_rfq` under policy v1 or
`attach_rfq_document` under v2. The RFQ must belong to the bound customer and, when enabled,
be assigned to the user. Missing, foreign and unassigned RFQs receive the same 404 response.

Authentication, permission, storage configuration and RFQ ownership are checked before
reading file content. See [the router](../../src/rfq_copilot/api/routers/attachments.py) and
[current authorization policy](day_21_rbac_authorization.md).

## Storage and input limits

Set `RFQ_API_ATTACHMENT_DIR` to an absolute private storage directory. Missing configuration
fails closed. New stored files have server-generated names and mode `0600`; the storage
boundary verifies directory ownership/permissions and refuses unsafe paths and symlinks.
Storage references and directory paths are not returned to clients.

The default file limit is 10 MiB, configured by `RFQ_API_MAX_UPLOAD_BYTES`. The multipart
stream has a byte cap including a bounded allowance for headers. Exactly one file and no
extra form fields are accepted. Filename, extension, declared media type, magic bytes,
empty content and size are validated; ZIP/spreadsheet archives are not expanded.

Only PDF and CSV BOM uploads are supported. Existing metadata can also describe historical
Markdown attachments; this does not make Markdown an accepted upload type.

## Extraction contracts

- PDF extraction returns text pages, page count, truncation and `text`/`no_text` status.
  Scanned documents are not OCRed. Parsing uses an isolated child process with an empty
  environment, wall-clock/CPU limits and platform-dependent memory limits. Database and
  decision credentials are not supplied to that process.
- CSV BOM extraction validates UTF-8, the required column set, row shapes and row limits.
  Decimal values remain exact. Parsed rows are untrusted requirements, not catalog,
  feasibility or pricing decisions; missing manufacturing facts still need validation.
- Upload returns extraction immediately. The list route returns metadata only; there is
  no download or later extraction-retrieval HTTP route.

See [response schemas](../../src/rfq_copilot/api/schemas/attachments.py) and
[extraction/storage implementation](../../src/rfq_copilot/attachments.py).

## Transaction ownership and idempotency

The upload uses a separate writable database/session from read routes. It releases its
connection while receiving/parsing the body, then rechecks RFQ ownership before insertion.
The route commits; errors before commit roll back and remove the file once its writer
finishes. An uncertain commit keeps the file for reconciliation rather than risking a
committed row that references a deleted file.

With `RFQ_API_UPLOAD_IDEMPOTENCY=enabled`, an optional `Idempotency-Key` header binds the
verified actor/customer, RFQ and canonical upload content. Identical retries return the
stored response; reuse for different content receives 409. A retry should retain its key.
Enabling the setting alone does not require every caller to send a key.

The [reconciliation CLI](../../src/rfq_copilot/reconcile_attachments.py) is dry-run by default.
Review its output and operational instructions before any explicitly authorized deletion.
Never clean the upload directory blindly after an ambiguous commit.

## Errors and verification

Rejected content has a fixed `ATTACHMENT_REJECTED` envelope and reason vocabulary. Path/body
validation uses the API validation envelope. Responses expose neither driver exceptions nor
private storage paths. The manually parsed multipart request still needs an OpenAPI input
contract; see the [current limitations register](technical_debt.md).

Relevant regression modules include `tests/test_attachments.py`,
`tests/test_day19_attachments_database.py`, `tests/test_phase3_reconcile.py`, and
`tests/test_phase3_hardening_database.py`. Database checks require the guarded test target.
No verification result is asserted by this documentation update.
