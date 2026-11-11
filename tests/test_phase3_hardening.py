"""Phase 3.5 hardening: public tests (no PostgreSQL, no dataset).

Key rotation (TD-013), trusted ancestor directories, ambiguous commits, isolated PDF
parsing, orphan reconciliation, the validation-error registry and SQL paging (TD-011).
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError
from test_attachments import FakeSession, make_app, pdf, pdf_file, stored, upload
from test_attachments import fakes as fakes
from test_auth import AUDIENCE, ISSUER, SUBJECT, pem_public, write_key
from test_auth import trust as trust

import rfq_copilot.attachments as attachments
import rfq_copilot.paths as paths
from rfq_copilot.api.app import create_app
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.errors import TRUSTED_SEGMENTS
from rfq_copilot.api.tokens import (
    InvalidToken,
    TokenConfigurationError,
    TokenVerifier,
    load_public_key,
)
from rfq_copilot.reconcile_attachments import ReconcileBusy, reconcile
from rfq_copilot.repositories import find_materials

# --- TD-013: trusted key set for rotation -------------------------------------------------


@pytest.fixture
def keys(tmp_path):
    folder = tmp_path / "keys"
    folder.mkdir(mode=0o700)
    private = {kid: ec.generate_private_key(ec.SECP256R1()) for kid in ("k-old", "k-new")}
    files = {kid: write_key(folder, f"{kid}.pem", pem_public(key), 0o600) for kid, key in private.items()}  # fmt: skip
    return private, files


def key_set_settings(files, kids):
    return ApiSettings(
        environment="development",
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        jwt_algorithm="ES256",
        jwt_public_keys=tuple((kid, files[kid]) for kid in kids),
    )


def signed(private_key, kid=None, **headers):
    import jwt

    now = int(time.time())
    if kid is not None:
        headers["kid"] = kid
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 300}
    return jwt.encode(claims, private_key, algorithm="ES256", headers=headers or None)


def test_overlapping_keys_each_verify_only_their_own_kid(keys):
    private, files = keys
    verifier = TokenVerifier.from_settings(key_set_settings(files, ("k-old", "k-new")))
    for kid in ("k-old", "k-new"):
        assert verifier.subject(signed(private[kid], kid)) == SUBJECT
    with pytest.raises(InvalidToken):  # right kid, other key: no fallback
        verifier.subject(signed(private["k-new"], "k-old"))


@pytest.mark.parametrize("kid", [None, "k-unknown", "K-OLD", "k-old ", "../k-old", ""])
def test_missing_unknown_or_near_miss_kid_is_rejected(keys, kid):
    private, files = keys
    verifier = TokenVerifier.from_settings(key_set_settings(files, ("k-old", "k-new")))
    with pytest.raises(InvalidToken):
        verifier.subject(signed(private["k-old"], kid))


def test_non_string_kid_and_key_locating_headers_are_rejected(keys):
    import jwt

    private, files = keys
    verifier = TokenVerifier.from_settings(key_set_settings(files, ("k-old",)))
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT, "iat": now, "exp": now + 300}
    from test_auth import handmade

    for header in ({"alg": "ES256", "kid": 7}, {"alg": "ES256", "kid": ["k-old"]}):
        with pytest.raises(InvalidToken):  # PyJWT will not sign these; built by hand
            verifier.subject(handmade(header, claims, b"x" * 64))
    with pytest.raises(InvalidToken):
        verifier.subject(jwt.encode(claims, private["k-old"], algorithm="ES256", headers={"kid": "k-old", "jku": "https://evil.invalid/jwks"}))  # fmt: skip


def test_removed_key_stops_verifying_after_reconfiguration(keys):
    private, files = keys
    token = signed(private["k-old"], "k-old")
    assert TokenVerifier.from_settings(key_set_settings(files, ("k-old", "k-new"))).subject(token)
    rotated = TokenVerifier.from_settings(key_set_settings(files, ("k-new",)))
    with pytest.raises(InvalidToken):
        rotated.subject(token)
    assert rotated.subject(signed(private["k-new"], "k-new")) == SUBJECT


def test_single_key_mode_is_unchanged_and_ignores_kid(trust):
    verifier = trust.verifier()
    assert verifier.key_set is None
    assert verifier.subject(trust.mint(headers={"kid": "anything"})) == SUBJECT


def test_key_set_comes_from_environment_and_is_validated(keys):
    private, files = keys
    env = {
        "RFQ_API_JWT_ISSUER": ISSUER,
        "RFQ_API_JWT_AUDIENCE": AUDIENCE,
        "RFQ_API_JWT_ALGORITHM": "ES256",
        "RFQ_API_JWT_PUBLIC_KEYS": f"k-old={files['k-old']}, k-new={files['k-new']}",
    }
    verifier = TokenVerifier.from_settings(ApiSettings.from_env(env))
    assert set(verifier.key_set) == {"k-old", "k-new"}
    with pytest.raises(TypeError):
        verifier.key_set["k-evil"] = object()  # read-only mapping
    bad_sets = [
        f"k-old={files['k-old']},k-old={files['k-new']}",  # duplicate kid
        f"a={files['k-old']},b={files['k-old']}",  # duplicate file
        "k-old=relative/key.pem",
        f"bad kid={files['k-old']}",
        f"{files['k-old']}",  # no kid
        ",".join(f"k{i}={files['k-old']}{i}" for i in range(5)),  # too many
    ]
    for value in bad_sets:
        with pytest.raises(ValidationError):
            ApiSettings.from_env({**env, "RFQ_API_JWT_PUBLIC_KEYS": value})
    with pytest.raises(ValidationError):  # both modes at once
        ApiSettings.from_env({**env, "RFQ_API_JWT_PUBLIC_KEY_FILE": str(files["k-old"])})


def test_every_key_in_a_set_passes_the_secure_loader(keys, tmp_path):
    private, files = keys
    link = files["k-old"].parent / "link.pem"
    link.symlink_to(files["k-new"])
    settings = key_set_settings({**files, "k-old": link}, ("k-old", "k-new"))
    with pytest.raises(TokenConfigurationError) as caught:
        create_app(settings)
    assert str(tmp_path) not in str(caught.value)


def test_verifier_requires_exactly_one_key_source(keys):
    with pytest.raises(TokenConfigurationError):
        TokenVerifier(ISSUER, AUDIENCE, "ES256", None, 900)
    with pytest.raises(TokenConfigurationError):
        TokenVerifier(ISSUER, AUDIENCE, "ES256", object(), 900, key_set={"k": object()})


# --- Trusted ancestors (key file and storage directory) ----------------------------------


def nested(tmp_path, *modes):
    """tmp/a/b/... with the given modes, innermost last; returns the innermost directory."""
    current = tmp_path
    for index, mode in enumerate(modes):
        current = current / f"d{index}"
        current.mkdir()
        current.chmod(mode)
    return current


@pytest.mark.parametrize("ancestor_mode", [0o777, 0o770, 0o757])
def test_key_under_a_writable_grandparent_is_refused(trust, tmp_path, ancestor_mode):
    # The pre-Phase-3 loader checked only the immediate parent and accepted this.
    folder = nested(tmp_path, ancestor_mode, 0o755)
    key = write_key(folder, "k.pem", trust.key_file.read_bytes(), 0o644)
    with pytest.raises(TokenConfigurationError) as caught:
        load_public_key(key, "ES256")
    assert str(tmp_path) not in str(caught.value)
    (tmp_path / "d0").chmod(0o755)
    assert load_public_key(key, "ES256") is not None


def test_sticky_ancestor_is_trusted_but_not_as_the_key_parent(trust, tmp_path):
    folder = nested(tmp_path, 0o1777, 0o755)
    assert load_public_key(write_key(folder, "k.pem", trust.key_file.read_bytes()), "ES256")
    sticky = nested(tmp_path / "d0", 0o1777)  # the key's own parent is sticky
    with pytest.raises(TokenConfigurationError):
        load_public_key(write_key(sticky, "k.pem", trust.key_file.read_bytes()), "ES256")


def test_foreign_owned_ancestors_are_refused(trust, tmp_path, monkeypatch):
    key = write_key(nested(tmp_path, 0o755), "k.pem", trust.key_file.read_bytes())
    monkeypatch.setattr(paths.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(TokenConfigurationError):
        load_public_key(key, "ES256")


def test_symlink_swapped_into_the_path_after_resolution_is_refused(tmp_path, monkeypatch):
    real = nested(tmp_path, 0o755)
    (tmp_path / "alias").symlink_to(real)
    # realpath would resolve the alias; simulate the alias appearing after resolution.
    monkeypatch.setattr(paths.os.path, "realpath", lambda p: os.path.abspath(p))
    with pytest.raises(paths.UntrustedPathError):
        paths.open_trusted_parent(tmp_path / "alias" / "file")
    descriptor, name = paths.open_trusted_parent(real / "file")
    os.close(descriptor)
    assert name == "file"


def test_storage_directory_with_an_untrusted_ancestor_is_refused(tmp_path):
    parent = nested(tmp_path, 0o777)
    with pytest.raises(attachments.UnsafeStorageError):
        attachments.store_file(parent / "store", attachments.new_attachment_id(), "pdf", b"%PDF-")  # fmt: skip
    assert not (parent / "store").exists() or list((parent / "store").iterdir()) == []


@pytest.mark.asyncio
async def test_upload_with_an_untrusted_storage_ancestor_answers_503(tmp_path, fakes):
    tmp_path.chmod(0o777)
    try:
        app = make_app(tmp_path)
        response = await upload(app, files=pdf_file())
        assert response.status_code == 503
        assert app.state.fake_session.added == [] and app.state.fake_session.commits == 0
        assert stored(tmp_path) == []
    finally:
        tmp_path.chmod(0o700)


# --- Ambiguous COMMIT: never delete blindly ---------------------------------------------


@pytest.mark.asyncio
async def test_failed_commit_keeps_the_file_for_reconciliation(tmp_path, fakes, caplog):
    app = make_app(tmp_path, session=FakeSession(fail_commit=True))
    response = await upload(app, files=pdf_file())
    assert response.status_code == 503 and response.json() == {"detail": "Service unavailable."}
    files = stored(tmp_path)
    assert len(files) == 1  # COMMIT may have succeeded: deleting could orphan the row
    assert app.state.fake_session.flushes == 1
    assert "kept for reconciliation" in caplog.text and files[0] not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_commit_keeps_the_file(tmp_path, fakes, monkeypatch):
    async def cancelled_commit(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(FakeSession, "commit", cancelled_commit)
    with pytest.raises(asyncio.CancelledError):
        await upload(make_app(tmp_path), files=pdf_file())
    assert len(stored(tmp_path)) == 1


@pytest.mark.asyncio
async def test_failed_flush_still_removes_the_file(tmp_path, fakes):
    response = await upload(make_app(tmp_path, session=FakeSession(fail_flush=True)), files=pdf_file())  # fmt: skip
    assert response.status_code == 503 and stored(tmp_path) == []


# --- Isolated, time-bounded PDF parsing --------------------------------------------------


def test_isolated_extraction_matches_in_process_extraction():
    data = pdf("Bracket RFQ-A1", None, "Second page")
    assert attachments.extract_pdf_isolated(data) == attachments.extract_pdf(data)
    with pytest.raises(attachments.AttachmentRejected) as caught:
        attachments.extract_pdf_isolated(b"%PDF-1.4 garbage")
    assert caught.value.reason == "pdf_malformed"


def test_runaway_parser_is_killed_at_the_deadline(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    script = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"  # fmt: skip
    monkeypatch.setattr(attachments, "_PDF_WORKER", (sys.executable, "-c", script))
    monkeypatch.setattr(attachments, "PDF_TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    with pytest.raises(attachments.AttachmentRejected) as caught:
        attachments.extract_pdf_isolated(pdf("x"))
    assert caught.value.reason == "pdf_too_complex" and time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):  # killed and reaped: no runaway worker
        os.kill(int(pid_file.read_text()), 0)


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        ("import sys; sys.exit(3)", "pdf_malformed"),
        ("import os, signal; os.kill(os.getpid(), signal.SIGKILL)", "pdf_too_complex"),
        ("print('not json')", "pdf_malformed"),
        ('print(\'{"rejected": "bom_rows"}\')', "pdf_malformed"),
        ('print(\'{"rejected": "pdf_encrypted"}\')', "pdf_encrypted"),
        (
            'print(\'{"status": "text", "page_count": 1, "pages": [[1, 5]], "truncated": false}\')',
            "pdf_malformed",
        ),  # fmt: skip
        (
            "import json; print(json.dumps({'status': 'text', 'page_count': 1, 'pages': [[1, 'x' * 200001]], 'truncated': False}))",
            "pdf_malformed",
        ),  # fmt: skip
    ],
)
def test_worker_failures_and_bad_output_become_fixed_rejections(monkeypatch, script, reason):
    monkeypatch.setattr(attachments, "_PDF_WORKER", (sys.executable, "-c", script))
    with pytest.raises(attachments.AttachmentRejected) as caught:
        attachments.extract_pdf_isolated(pdf("x"))
    assert caught.value.reason == reason


def test_worker_runs_isolated_without_the_server_environment(monkeypatch):
    seen = {}
    real = subprocess.run

    def spy(args, **kwargs):
        seen.update(args=args, env=kwargs.get("env"), timeout=kwargs.get("timeout"))
        return real(args, **kwargs)

    monkeypatch.setenv("DATABASE_URL", "postgresql://never-reaches-the-worker")
    monkeypatch.setattr(attachments.subprocess, "run", spy)
    attachments.extract_pdf_isolated(pdf("x"))
    assert seen["env"] == {} and "-I" in seen["args"]
    assert seen["timeout"] == attachments.PDF_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_timed_out_pdf_upload_is_rejected_without_side_effects(tmp_path, fakes, monkeypatch):  # fmt: skip
    monkeypatch.setattr(attachments, "_PDF_WORKER", (sys.executable, "-c", "import time; time.sleep(30)"))  # fmt: skip
    monkeypatch.setattr(attachments, "PDF_TIMEOUT_SECONDS", 1)
    app = make_app(tmp_path)
    response = await upload(app, files=pdf_file())
    assert response.status_code == 422 and response.json()["reason"] == "pdf_too_complex"
    assert stored(tmp_path) == [] and app.state.fake_session.added == []


# --- Orphan reconciliation ---------------------------------------------------------------


def storage(tmp_path):
    folder = tmp_path / "store"
    folder.mkdir(mode=0o700)
    return folder


def stored_file(folder, age_seconds, kind="pdf"):
    name = f"{attachments.new_attachment_id()}.{kind}"
    path = folder / name
    path.write_bytes(b"x")
    path.chmod(0o600)
    then = time.time() - age_seconds
    os.utime(path, (then, then))
    return name


async def unreferenced(name):
    return True


@pytest.mark.asyncio
async def test_dry_run_reports_and_deletes_nothing(tmp_path):
    folder = storage(tmp_path)
    kept = stored_file(folder, 7200)
    orphan = stored_file(folder, 7200)
    young = stored_file(folder, 60)
    (folder / "notes.txt").write_text("other")
    (folder / f"{attachments.new_attachment_id()}.pdf").symlink_to(folder / kept)
    report = await reconcile(folder, [kept, "ATT-MISSING.pdf"], still_unreferenced=unreferenced, grace_seconds=3600)  # fmt: skip
    assert (report.mode, report.files, report.referenced) == ("dry-run", 3, 1)
    assert (report.orphans_eligible, report.orphans_in_grace, report.deleted) == (1, 1, 0)
    assert (report.unexpected_entries, report.rows_missing_file) == (2, 1)
    assert {orphan, young, kept} <= set(os.listdir(folder))


@pytest.mark.asyncio
async def test_delete_removes_only_old_unreferenced_server_files(tmp_path):
    folder = storage(tmp_path)
    kept, orphan, young = stored_file(folder, 7200), stored_file(folder, 7200), stored_file(folder, 60)  # fmt: skip
    (folder / "ATT-not-ours.pdf").write_bytes(b"x")
    report = await reconcile(folder, [kept], still_unreferenced=unreferenced, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 1 and orphan not in os.listdir(folder)
    assert {kept, young, "ATT-not-ours.pdf"} <= set(os.listdir(folder))


@pytest.mark.asyncio
async def test_row_appearing_or_file_replaced_before_removal_keeps_the_file(tmp_path):
    folder = storage(tmp_path)
    first, second = stored_file(folder, 7200), stored_file(folder, 7200)

    async def appears(name):
        if name == second:  # replaced on disk between the scan and the removal
            (folder / name).unlink()
            (folder / name).write_bytes(b"new")
            return True
        return False  # a row now names the first file

    report = await reconcile(folder, [], still_unreferenced=appears, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 0 and report.kept_on_recheck == 2
    assert {first, second} <= set(os.listdir(folder))


@pytest.mark.asyncio
async def test_reconciler_refuses_short_grace_unsafe_directories_and_concurrency(tmp_path):
    import fcntl

    folder = storage(tmp_path)
    with pytest.raises(ValueError):
        await reconcile(folder, [], still_unreferenced=unreferenced, grace_seconds=600)
    folder.chmod(0o755)
    with pytest.raises(attachments.UnsafeStorageError):
        await reconcile(folder, [], still_unreferenced=unreferenced)
    folder.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(folder)
    with pytest.raises(attachments.UnsafeStorageError):
        await reconcile(link, [], still_unreferenced=unreferenced)
    held = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(ReconcileBusy):
            await reconcile(folder, [], still_unreferenced=unreferenced)
    finally:
        os.close(held)


def test_reconciler_command_prints_counts_only(tmp_path, monkeypatch, capsys):
    import rfq_copilot.reconcile_attachments as command

    folder = storage(tmp_path)
    secret_name = stored_file(folder, 7200)

    async def fake_run(args):
        from dataclasses import asdict

        report = await reconcile(args.storage_dir, [], still_unreferenced=unreferenced)
        return {**asdict(report), "pairing": "unbound"}

    monkeypatch.setattr(command, "_run", fake_run)
    assert command.main(["--storage-dir", str(folder)]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["orphans_eligible"] == 0 and json.loads(out)["mode"] == "dry-run"
    assert secret_name not in out and str(tmp_path) not in out
    folder.chmod(0o777)
    assert command.main(["--storage-dir", str(folder)]) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["status"] == "error" and str(tmp_path) not in out
    folder.chmod(0o700)


# --- Validation-error registry (Day 17) ---------------------------------------------------


def test_every_declared_parameter_is_a_trusted_error_segment(trust):
    # A new route whose parameters were not registered would get redacted error paths;
    # this fails first, so the registry is updated deliberately.
    schema = create_app(trust.settings()).openapi()
    names = {
        parameter["name"]
        for item in schema["paths"].values()
        for operation in item.values()
        for parameter in operation.get("parameters", [])
    }
    assert names and names <= TRUSTED_SEGMENTS


def test_unregistered_nested_model_fields_are_redacted():
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from rfq_copilot.api.errors import request_validation_handler

    class Inner(BaseModel):
        unregistered_secret_field: int

    class Outer(BaseModel):
        inner_unregistered: Inner

    app = FastAPI(exception_handlers={RequestValidationError: request_validation_handler})

    @app.post("/probe")
    async def probe(body: Outer):
        return {}

    response = TestClient(app).post("/probe", json={"inner_unregistered": {"unregistered_secret_field": "x"}})  # fmt: skip
    assert response.status_code == 422
    assert "unregistered_secret_field" not in response.text and "inner_unregistered" not in response.text  # fmt: skip


# --- TD-011: paging arguments are validated before any query ------------------------------


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}, {"limit": True}, {"offset": -1}, {"offset": 1.5}])  # fmt: skip
@pytest.mark.asyncio
async def test_find_materials_rejects_bad_paging_before_querying(kwargs):
    with pytest.raises(ValueError):
        await find_materials(None, **kwargs)  # no session is touched
