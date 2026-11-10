# Day 19: Secure File Uploads and Document Extraction

Status: implemented and verified, 2026-11-10. Uncommitted, like Days 16–18.

## Scope

A verified customer can attach a PDF or a CSV bill of materials (BOM) to one of their own RFQs. The service checks the file's real content, extracts its text or rows deterministically, stores it under a server-generated name, and records metadata in the existing `business.rfq_attachments` table. The customer can also list attachment metadata.

Not in scope: authentication (Day 20), OCR, LLM or agent extraction, Markdown attachments, attachment download, and any manufacturing validation of what was extracted.

## Supported formats (from the repository, not invented)

| Source | What it says |
| --- | --- |
| `rfq_attachments.document_type` check | `pdf`, `csv`, `md` |
| Dataset v1.1.0 `attachments/` | 79 searchable PDF specifications, 44 CSV BOMs, 1 Markdown drawing description |
| Dataset BOM header (all 44 files) | `rfq_id,revision,line,part_type,length_mm,width_mm,height_mm,material_id,quantity,tolerance_mm,finish_id` |
| Dataset generator | `source_role = customer_supplied_simulated`, `is_trusted_instruction = false` |

PDF and CSV are supported. Markdown is deferred: the task covers PDF and BOM only.

## Architecture

| Module | Responsibility |
| --- | --- |
| `src/rfq_copilot/attachments.py` | Pure logic: filename and content checks, PDF and BOM extraction, file storage and removal. No FastAPI, no database. |
| `src/rfq_copilot/repositories/attachments.py` | `rfq_belongs_to`, `add_attachment`, `list_attachments`. Each takes the verified `customer_id`. None commits. |
| `src/rfq_copilot/api/routers/attachments.py` | `POST` and `GET /api/v1/rfqs/{rfq_id}/attachments`, a bounded multipart reader, and a fixed rejection table. |
| `src/rfq_copilot/api/schemas/attachments.py` | Response and rejection contracts. |
| `src/rfq_copilot/api/db.py` | New `get_write_database` / `get_write_session`, separate from the read path. |
| `src/rfq_copilot/api/config.py` | New `attachment_dir` and `max_upload_bytes` settings. |

### Upload lifecycle

Each step fails closed before the next one runs.

1. **Identity.** `get_principal` is Day 18's dependency, unchanged. It always answers 401 until Day 20, and it reads nothing from the request. Without a principal, the body is never read: a test counts ASGI `receive` calls and gets 0. No storage directory, database or file is touched.
2. **Storage configured.** If `RFQ_API_ATTACHMENT_DIR` is unset, the answer is 503.
3. **Write session.** `get_write_session` depends on `get_principal`. It uses its own writable `Database`, which is built lazily from `DATABASE_URL` and disposed at shutdown when the app owns it.
4. **Ownership pre-check.** `rfq_belongs_to(rfq_id, principal.customer_id)`. Another customer's RFQ and a missing RFQ both get the identical body `404 {"detail": "Not found."}`, and the body is not read. The transaction is then rolled back, so no pooled connection is held while the client uploads.
5. **Bounded multipart read.**
   - The Content-Type must be `multipart/form-data`.
   - A declared Content-Length above `max_upload_bytes + 64 KiB` gets 413 before anything is read.
   - While the body streams, a byte counter enforces the same cap, which also covers chunked bodies without a length. The counter raises a `MultiPartException` subclass, so Starlette closes any spooled part file.
   - `MultiPartParser` runs with `max_files=1, max_fields=0`. Exactly one file part named `file` is accepted. Any other field, a second file or a broken structure gets 400.
   - The form is closed in `finally`.
6. **Content checks and extraction** (`check_file`, run in a worker thread):
   - Filename rules first. The name is only display metadata and never a path. Names are rejected, not repaired, for `/`, `\`, control or format characters (including bidi overrides), a leading dot, surrounding whitespace, or more than 255 characters. The extension must be `.pdf` or `.csv`.
   - The file must not be empty and not over the limit.
   - ZIP/OOXML magic is refused, so no archive is ever expanded.
   - The `%PDF-` magic must agree with the extension.
   - The client's declared MIME type must be compatible. It is checked, but never trusted on its own.
   - Then extraction runs.
7. **Store.** The id is `ATT-` plus 32 random hex characters.
   - The storage directory is created with mode 0700 if it is missing. It is then opened with `O_DIRECTORY|O_NOFOLLOW` and checked on the opened descriptor.
   - It must not be a symlink, must be owned by the server's user, and must give no group or other permissions. Otherwise the upload gets an opaque 503 with a log line that names no path.
   - An existing directory's mode is never changed. This is the same rule seeding applies to its snapshot directory.
   - The file `<attachment_id>.<pdf|csv>` is created relative to that descriptor (`dir_fd`) with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode 0600, then `fsync`ed. A directory swapped in after the check therefore cannot receive the write.
   - A partial write is unlinked.
8. **Persist.** In the write transaction, ownership is checked again, the row is staged and the transaction commits. Every row gets `source_role = customer_supplied`, `is_trusted_instruction = false` and `revision = NULL`; the revision is never inferred from the file or its name.
   - The storage write runs in a worker thread that cancellation cannot stop, so the route awaits it through `asyncio.shield`.
   - On any failure (including cancellation during the write, during the ownership re-check or during the commit), cleanup waits for the thread to finish and then removes the file that thread created, in another worker thread.
   - The cleanup is its own referenced, shielded task, so a second cancellation cannot abandon it.
   - A failed write already removed its own partial file, and cleanup never removes a file the request did not create.
   - The session dependency rolls back. A storage or database failure is an opaque 503.
9. **Respond** with 201: `attachment_id, rfq_id, filename, document_type, revision, sha256, size_bytes, extraction`. The storage reference and directory are never returned.

`GET .../attachments` uses Day 18's read-only session. It checks ownership (404 otherwise) and returns metadata only. There is no download route.

### Extraction

**PDF** (pypdf, `strict=False`):
- Only `extract_text()` is called. The extractor does no OCR, does no rendering, follows no links, and does not run JavaScript, embedded files or open actions. A test blocks sockets during extraction of a hostile PDF.
- An encrypted PDF gets `pdf_encrypted`. Decryption is never attempted, not even with an empty password.
- A corrupt or hostile file gets `pdf_malformed`. pypdf errors and the plain Python errors it can raise on hostile input are mapped to that reason, and no exception text is reported.
- 0 pages also gets `pdf_malformed`. More than 100 pages gets `pdf_too_many_pages`.
- Total text is capped at 200,000 characters; anything cut is flagged with `truncated: true`.
- A PDF with pages but no extractable text (for example a scan) is accepted with `status: "no_text"` and empty page text. Nothing is invented.
- Decoding is bounded by pypdf's built-in 75 MB stream-output limits. A test confirms that an 80 MB compression bomb is rejected.

**BOM** (stdlib `csv`, `strict=True`):
- The file must be strict UTF-8; a UTF-8 signature is allowed. Otherwise it gets `bom_encoding`, and NUL bytes get the same reason.
- The header must equal the eleven dataset columns, in order. A column that is missing, extra, renamed, reordered, duplicated or differs in case gets `bom_columns`. No column is guessed.
- At least one data row and at most 500 rows.
- Per-cell rules:
  - Only `height_mm` may be blank. It becomes `null`, never 0. The dataset leaves it blank for flat parts in 35 of 52 development/validation rows.
  - `line` and `quantity` must be ASCII digits from 1 to 2^31−1.
  - `*_mm` values must be plain ASCII decimals. They become `Decimal` and keep the written scale, so `80.50` stays `80.50`. Exponents, signs, spaces and full-width digits are rejected, not reinterpreted.
  - Text cells must not start with `= + - @ TAB CR` (formula-like), must have no surrounding whitespace or control characters, and must be at most 64 characters.
- Row problems are returned as `{row, column, code}`, at most 20. `column` comes only from the fixed column list, never from the file.

Extraction does not check catalog membership, part-type vocabulary, ranges or consistency with the RFQ. Those decisions stay in `rfq_copilot.services`.

### Error contract

Rejections return `{"code": "ATTACHMENT_REJECTED", "message", "reason", "issues"}`.
- `reason` comes from a fixed `Literal`, and `message` from a static table. Neither is built from the request, the file or an exception.
- Status codes: 400 `malformed_multipart`; 413 `request_too_large`, `file_too_large`; 415 `unsupported_type`, `content_type_mismatch`; 422 for every other reason.
- Invalid path parameters still use the Day 17 structured 422, with its allowlist and redaction unchanged.

## Security considerations and OBS-1

- The write path is clearly separated. Day 18 read routes still use `get_read_session` and the lazily built read-only database. The upload uses `get_write_session` on a different `Database` object.
- An integration test checks the separation: after an upload request, the write database reports `default_transaction_read_only = off`, the read database still reports `on`, and the read database was not repurposed.
- **OBS-1 / TD-009 remains open.** A caller-injected read `Database` is still not verified read-only. Day 19 neither widens nor fixes it.
- File-system safety does not depend on the client: server-generated names, `O_EXCL` against overwrite and collisions, `O_NOFOLLOW` against planted symlinks, and 0600 files in a verified private directory. Creation and removal go through a directory descriptor, so they stay inside that directory. Unsafe existing directories are rejected, never chmod-ed. This relies on POSIX `dir_fd` and `O_DIRECTORY`, available on Linux and macOS.
- Nothing logs file contents. pypdf may log structural warnings (for example "EOF marker not found"); these name no content.

## Known limitations

- Cancellation and every in-process failure now clean up. A process crash or kill between writing the file and committing can still leave an orphan file with no row. It is harmless and not served, but there is no sweeper yet.
- Parent directories of the storage directory are not checked. Only the configured directory itself must be private and non-symlinked.
- If a commit succeeds on the server but its acknowledgement is lost, the file is removed while the row remains. This is rare. The row's `sha256` can be checked against storage later.
- There is no wall-clock timeout on PDF parsing. Bounds come from the size limit, the page and text caps and pypdf's decode limits.
- Attachment metadata cannot record upload time or size: the table has no such columns, and schema changes need approval.
- Uploads are unreachable until Day 20 provides an authenticator.

## Verification (actual results)

| Check | Result |
| --- | --- |
| `pytest tests/test_attachments.py` (public) | 113 passed |
| `pytest tests/test_day19_attachments_database.py` (guarded test DB plus dataset parity) | 9 passed |
| Day 16–18 API files plus public smoke suite (6 files) | 373 passed |
| Full regression `pytest -q` | **1464 passed**, 0 failed, 0 skipped, 0 warnings (baseline 1342) |
| Clean copy (no `.env`, no dataset, fresh `uv sync --locked`) smoke | 373 passed |
| Mutation checks (ownership, stream cap, file cleanup, traversal, content sniffing) | each caught by at least 1 test |
| `ruff check .` / `ruff format --check src tests alembic` | pass / 105 files formatted |
| `uv lock --check` | pass. The lock diff adds only `pypdf 6.21.4` and `python-multipart 0.0.33` |
| Uvicorn, production and development | `/health/live` 200; upload and list 401, including with forged `Authorization` and `X-Customer-Id`; storage directory never created; clean shutdown |
| `python -m rfq_copilot.integrity --target development` (read only) | PASS, records 1090; Alembic head `20261109_01` unchanged |

Dataset parity uses development and validation attachments only, never frozen evaluation: all 32 BOMs reproduce every cell exactly, and all 50 PDFs extract text containing their RFQ id.

## Dependencies

- `pypdf>=6.1,<7`: the dataset's own validation utility uses pypdf, and it was already present but undeclared in `.venv`. It is now declared.
- `python-multipart>=0.0.20,<1`: required by Starlette/FastAPI to parse multipart bodies. Writing our own multipart parser would be a security risk.

Both were added with `uv add --no-sync`, so undeclared packages in `.venv` were not removed.

## Day 18 observations: still open

OBS-1 (injected `Database` not verified read-only), OBS-2 (quote lookup in Python) and OBS-3 (material pagination in Python) are tracked as TD-009, TD-010 and TD-011 in [technical_debt.md](technical_debt.md). Each entry records its origin, risk, recommended fix and verification criteria. None was changed on Day 19.

## Correction (2026-11-10): filesystem safety

1. **Cancellation during the threaded write.** Previously, cleanup started only after `await asyncio.to_thread(store_file, ...)`. A cancellation during that await left the thread running and its file orphaned. The write is now shielded, and every failure path waits for the thread before removing its file. A deterministic test pauses the worker with threading events, cancels the request, releases the worker, and asserts that the file is gone and nothing was committed.
2. **Existing storage directory.** Previously, `mkdir(mode=0o700, exist_ok=True)` accepted any existing directory. Now a symlink, a directory owned by someone else, or one with any group or other permission is rejected (`UnsafeStorageError`, which the API answers with 503). The write and the removal go through a verified directory descriptor.

Tests: 124 public tests in `test_attachments.py` (+11) and 1,475 in the full regression. A mutation check confirmed that the old lifecycle fails the cancellation test and that removing the directory checks fails 8 tests.
