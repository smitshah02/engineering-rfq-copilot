# Development Log: Day 19 — Secure File Uploads and Document Extraction

Date: 2026-11-10

## Objective

Identity-gated, ownership-checked upload of PDF and CSV BOM attachments to an RFQ, with deterministic extraction, server-named file storage and metadata in the existing `rfq_attachments` table. Technical description: [day_19_file_uploads_extraction.md](../development/day_19_file_uploads_extraction.md).

Baseline: Days 16–18 uncommitted (preserved); 1342 tests passing.

## Day 18 observations recorded first

OBS-1, OBS-2 and OBS-3 were appended to the existing register, `docs/development/technical_debt.md`, as TD-009, TD-010 and TD-011, all **Open**. Each entry records origin, risk, recommended correction and verification criteria. None was fixed or refactored today.

## Files

| File | Change |
| --- | --- |
| `src/rfq_copilot/attachments.py` | New. Filename and content checks, PDF extraction (pypdf), BOM extraction (csv), `store_file` / `remove_file`, fixed rejection vocabulary. |
| `src/rfq_copilot/repositories/attachments.py` | New. `rfq_belongs_to`, `add_attachment`, `list_attachments`, all customer-scoped; none commits. |
| `src/rfq_copilot/repositories/__init__.py` | Exports the three functions (additive). |
| `src/rfq_copilot/api/routers/attachments.py` | New. `POST` and `GET /api/v1/rfqs/{rfq_id}/attachments`, bounded multipart reader, static rejection messages. |
| `src/rfq_copilot/api/schemas/attachments.py` | New. Metadata, extraction, upload and rejection contracts. |
| `src/rfq_copilot/api/db.py` | Adds `get_write_database` and `get_write_session`. The read path is unchanged. |
| `src/rfq_copilot/api/config.py` | Adds `attachment_dir` (absolute path, default `None`, which makes uploads answer 503) and `max_upload_bytes` (default 10 MiB, at most 50 MiB). |
| `src/rfq_copilot/api/app.py` | Registers the router and tag; stores settings and write-database state; lifespan also disposes an owned write database; description updated. |
| `tests/test_attachments.py` | New. 113 public tests: no PostgreSQL, no dataset. |
| `tests/test_day19_attachments_database.py` | New. 7 PostgreSQL integration tests and 2 dataset parity tests (development and validation only). |
| `tests/test_api.py`, `tests/test_api_schemas.py` | Exact route-set, tag and description assertions updated to the new exact surface. The upload is the only allowed non-GET operation. |
| `pyproject.toml`, `uv.lock` | Adds `pypdf>=6.1,<7` and `python-multipart>=0.0.20,<1`. The lock diff adds only those two packages. |
| `.github/workflows/smoke.yml` | Adds `tests/test_attachments.py`. |
| `README.md` | Current features, endpoint table, settings, smoke command, limitations. |
| `docs/development/technical_debt.md` | Appended TD-009, TD-010 and TD-011 (Day 18 OBS-1, OBS-2, OBS-3). |
| `docs/development/day_19_file_uploads_extraction.md` | New. |

Not changed: ORM models, migrations, business services, rule versions, evaluation fingerprints, dataset v1.1.0, `seeding/validation.py`, and the Day 18 read routes and read session.

## Decisions

- **Persistence.** Uploads reuse `business.rfq_attachments`, whose columns cover id, RFQ, filename, storage reference, type, source role, trust flag and sha256. No schema change was needed.
- **Formats.** `pdf` and `csv` are supported, as the table and the dataset justify. `md` is deferred.
- **Ownership.** Ownership is checked before the body is read, and again inside the insert transaction. The connection is released while the body streams.
- **Content errors are rejected, never repaired.** This covers filenames, BOM columns and cells. Blank `height_mm` is the only optional cell, as in the dataset.
- **Image-only PDFs** are accepted with `status: "no_text"`. Encrypted PDFs are rejected.
- **`source_role = customer_supplied`.** This follows the dataset's `customer_supplied_simulated`, without the "simulated" suffix.

## Problems found and fixed during the day

1. Seven test failures on the first run of the public tests, all in the tests themselves: a size limit too small for the sample files, CSV quoting in the test builder, and one expectation. `Decimal` keeps the exact value and scale but drops leading zeros (`0007.250` becomes `7.250`); the assertion now checks value and scale.
2. The integration fixture used plain `@pytest.fixture` for an async fixture. Fixed with `pytest_asyncio.fixture(loop_scope="module")`. The asyncio mark now applies to async tests only, which avoids a warning on the sync parity tests.
3. The seed fixture shares `tmp_path` (it writes `snapshots/`), so attachment tests now use `tmp_path/store`.
4. One unused import, caught by ruff.

## Verification (commands and actual results)

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_attachments.py` | 113 passed |
| `.venv/bin/python -m pytest -q tests/test_day19_attachments_database.py` | 9 passed |
| `pytest -q tests/test_database_config.py tests/test_hardening_public.py tests/test_api.py tests/test_api_schemas.py tests/test_api_reads.py tests/test_attachments.py` | 373 passed |
| `.venv/bin/python -m pytest -q` | 1464 passed in 130.65s; 0 failed, 0 skipped, 0 warnings |
| Clean copy (no `.env`, no `data/`, `uv sync --locked`), same smoke command | 373 passed |
| Mutation checks: ownership disabled, stream cap disabled, cleanup removed, `/` allowed in names, PDF magic check removed | 2, 1, 3, 2 and 3 failures respectively (all caught) |
| `.venv/bin/ruff check .` | All checks passed |
| `.venv/bin/ruff format --check src tests alembic` | 105 files already formatted |
| `uv lock --check` | Resolved 42 packages, pass |
| Uvicorn (`--factory`, production and development, no `DATABASE_URL`) | live 200; POST and GET attachments 401, including with forged `Authorization` and `X-Customer-Id`; docs 404 in production, 200 in development; storage directory not created; clean shutdown; 0 error lines |
| `python -m rfq_copilot.integrity --target development …` (read only) | PASS, records 1090 |
| `alembic … current` | `20261109_01 (head)` |

## Git status at end of day

Nothing staged or committed.

```
 M .github/workflows/smoke.yml
 M README.md
 M docs/development/technical_debt.md
 M pyproject.toml
 M src/rfq_copilot/repositories/__init__.py
 M src/rfq_copilot/repositories/queries.py        (Day 18 change, untouched today)
 M uv.lock
?? day16_review_bundle.zip / day17_review_bundle.zip / day18_review_bundle.zip
?? docs/dev_log/day_16.md … day_19.md
?? docs/development/day_16_… day_19_file_uploads_extraction.md
?? src/rfq_copilot/api/
?? src/rfq_copilot/attachments.py
?? src/rfq_copilot/repositories/attachments.py
?? tests/test_api.py tests/test_api_reads.py tests/test_api_schemas.py
?? tests/test_attachments.py tests/test_day18_api_database.py tests/test_day19_attachments_database.py
```

## Correction — 2026-11-10: filesystem safety

Two independently reproduced findings, fixed with minimal changes.

**Fix 1: cancellation during the threaded storage write**
- Before: `routers/attachments.py` awaited `asyncio.to_thread(store_file, …)` outside the cleanup `try`. Cancelling the request during that await could not stop the thread, so its file was written with no row and never removed.
- Now: the write is a task awaited through `asyncio.shield`, inside the `try`.
  - Every failure path calls `discard_after`. It waits for the thread to finish, then removes the file through `asyncio.to_thread`, so the event loop does no blocking I/O.
  - The cleanup is a separate task, shielded and held in a module set until done, so a second cancellation cannot abandon it.
  - It removes only a file this request created: a failed write already removed its partial file, and a collision must never delete someone else's file.
  - `OSError` (unsafe directory, disk, driver) now maps to an opaque 503 with a log line that names no path.

**Fix 2: storage directory security**
- Before: `attachments.store_file` used `mkdir(mode=0o700, exist_ok=True)`, which leaves an existing directory as it is.
- Now: `_open_directory` creates the directory 0700 if it is missing, then opens it with `O_DIRECTORY|O_NOFOLLOW`. On the descriptor, it rejects a non-owner directory or any `0o077` permission bit. A symlink gets `UnsafeStorageError`. Modes are never changed.
- Files are created and unlinked with `dir_fd`, which closes the gap where the directory could be replaced between the check and the write.

**Files**
- `src/rfq_copilot/attachments.py`: `UnsafeStorageError`, `_open_directory`; `store_file` and `remove_file` now use `dir_fd`.
- `src/rfq_copilot/api/routers/attachments.py`: shielded write, `discard_after`, OSError → 503, logger.
- `tests/test_attachments.py`: 11 new tests (an existing private directory works; modes 0777, 0755, 0750, 0701 and 0720 rejected and left unchanged; symlinked directory; foreign owner; directory swapped after the check; unsafe directory answers 503 over the API; deterministic cancellation with threading events). One existing test now creates its directory with mode 0700, because the 0755 default is correctly rejected.
- `docs/development/day_19_file_uploads_extraction.md`: steps 7 and 8, security, limitations, and a correction section.

No database code, business services, ORM models, migrations or dataset files were changed.

**Verification (actual)**

| Command | Result |
| --- | --- |
| `pytest -q tests/test_attachments.py tests/test_day19_attachments_database.py` | 133 passed |
| `pytest -q tests/test_api.py tests/test_api_schemas.py tests/test_api_reads.py tests/test_day18_api_database.py` | 231 passed |
| Public smoke (6 files, no `DATABASE_URL`) | 384 passed |
| `.venv/bin/python -m pytest -q` | 1475 passed in 141.66s; 0 failed, 0 skipped, 0 warnings |
| Mutation: old unshielded lifecycle restored | cancellation test fails (1 failed) |
| Mutation: directory checks and `O_NOFOLLOW` removed | 8 failed |
| `ruff check .` / `ruff format --check src tests alembic` | pass / 105 files formatted |
| `uv lock --check` | pass |
| `python -m rfq_copilot.integrity --target development …` (read only) | PASS, records 1090 |

**Remaining limitations:** a process kill between writing the file and committing can still leave an orphan file (there is no sweeper); parent directories are not checked; the code relies on POSIX `dir_fd`, which is available on Linux and macOS.

Git: nothing staged or committed. The new changes are confined to the three source and test files above plus the two Day 19 docs.
