"""Phase 3.5 reconciler corrections: inode reuse and database/storage pairing.

Public (no PostgreSQL): the database side of the command is a fake ``_Catalog``. The
real fingerprint query runs in test_phase3_hardening_database.py.
"""

import json
import os
import time

import pytest
from test_phase3_hardening import storage, stored_file, unreferenced

import rfq_copilot.reconcile_attachments as command
from rfq_copilot.reconcile_attachments import (
    BINDING_MARKER,
    PairingRefused,
    bind_storage,
    database_fingerprint,
    pairing_status,
    reconcile,
)

CLUSTER = 7432100000000000001
DEV = database_fingerprint(CLUSTER, "rfq_copilot_dev", 16384)
TEST = database_fingerprint(CLUSTER, "rfq_copilot_test", 16385)


class InodeReuse:
    """Make every stat of ``name``'s replacement report the original (st_dev, st_ino).

    Linux filesystems can hand a just-freed inode to the next file; macOS rarely does.
    Rewriting the reported identity makes the worst case deterministic on both.
    """

    def __init__(self, monkeypatch, folder, name):
        original = os.stat(folder / name)
        self.identity = (original.st_dev, original.st_ino)
        self.replacement = None
        real_stat, real_fstat = os.stat, os.fstat

        def disguise(result):
            if self.replacement is not None and (result.st_dev, result.st_ino) == self.replacement:
                return _Disguised(result, *self.identity)
            return result

        monkeypatch.setattr(command.os, "stat", lambda *a, **k: disguise(real_stat(*a, **k)))
        monkeypatch.setattr(command.os, "fstat", lambda *a, **k: disguise(real_fstat(*a, **k)))
        self.real_stat = real_stat

    def replace(self, path, data, mtime=None):
        path.unlink()
        path.write_bytes(data)
        path.chmod(0o600)
        if mtime is not None:
            os.utime(path, ns=(mtime, mtime))
        info = self.real_stat(path)
        self.replacement = (info.st_dev, info.st_ino)


class _Disguised:
    def __init__(self, real, dev, ino):
        self._real, self.st_dev, self.st_ino = real, dev, ino

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- Finding 1: a replacement with a reused inode is never deleted ------------------------


@pytest.mark.asyncio
async def test_fresh_replacement_with_reused_inode_is_kept(tmp_path, monkeypatch):
    folder = storage(tmp_path)
    name = stored_file(folder, 7200)
    reuse = InodeReuse(monkeypatch, folder, name)

    async def replaced_meanwhile(candidate):
        reuse.replace(folder / candidate, b"replacement written just now")
        return True

    report = await reconcile(folder, [], still_unreferenced=replaced_meanwhile, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 0 and report.kept_on_recheck == 1
    assert (folder / name).read_bytes() == b"replacement written just now"
    assert not [n for n in os.listdir(folder) if n.startswith(command.QUARANTINE_PREFIX)]


@pytest.mark.asyncio
async def test_old_looking_replacement_with_reused_inode_is_kept(tmp_path, monkeypatch):
    folder = storage(tmp_path)
    name = stored_file(folder, 7200)
    before = os.stat(folder / name)
    reuse = InodeReuse(monkeypatch, folder, name)

    async def replaced_meanwhile(candidate):  # backdated mtime, different size
        reuse.replace(folder / candidate, b"other content", mtime=before.st_mtime_ns)
        return True

    report = await reconcile(folder, [], still_unreferenced=replaced_meanwhile, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 0 and (folder / name).read_bytes() == b"other content"


@pytest.mark.asyncio
async def test_name_taken_during_quarantine_keeps_both_files(tmp_path, monkeypatch):
    folder = storage(tmp_path)
    name = stored_file(folder, 7200)
    real_rename = os.rename

    async def replaced_meanwhile(candidate):
        (folder / candidate).unlink()
        (folder / candidate).write_bytes(b"replacement")
        return True

    def rename_then_new_upload(src, dst, **kwargs):
        real_rename(src, dst, **kwargs)
        (folder / name).write_bytes(b"third writer")  # the name is reused immediately

    monkeypatch.setattr(command.os, "rename", rename_then_new_upload)
    report = await reconcile(folder, [], still_unreferenced=replaced_meanwhile, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 0 and report.quarantined_kept == 1
    assert (folder / name).read_bytes() == b"third writer"
    kept = [n for n in os.listdir(folder) if n.startswith(command.QUARANTINE_PREFIX)]
    assert len(kept) == 1 and (folder / kept[0]).read_bytes() == b"replacement"
    monkeypatch.undo()
    again = await reconcile(folder, [], still_unreferenced=unreferenced, grace_seconds=3600)
    assert again.quarantine_entries == 1  # reported, never removed automatically


@pytest.mark.asyncio
async def test_symlink_swapped_in_is_restored_not_followed(tmp_path):
    folder = storage(tmp_path)
    name = stored_file(folder, 7200)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"not ours")

    async def swapped(candidate):
        (folder / candidate).unlink()
        (folder / candidate).symlink_to(outside)
        return True

    report = await reconcile(folder, [], still_unreferenced=swapped, grace_seconds=3600, delete=True)  # fmt: skip
    assert (
        report.deleted == 0 and (folder / name).is_symlink() and outside.read_bytes() == b"not ours"
    )


@pytest.mark.asyncio
async def test_unchanged_old_orphan_is_still_deleted(tmp_path):
    folder = storage(tmp_path)
    orphan, young = stored_file(folder, 7200), stored_file(folder, 60)
    report = await reconcile(folder, [], still_unreferenced=unreferenced, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 1 and orphan not in os.listdir(folder) and young in os.listdir(folder)
    assert not [n for n in os.listdir(folder) if n.startswith(command.QUARANTINE_PREFIX)]


@pytest.mark.asyncio
async def test_age_is_revalidated_at_removal(tmp_path):
    folder = storage(tmp_path)
    name = stored_file(folder, 7200)
    now = time.time()

    async def touched(candidate):  # same file, but written to just now (mtime moves)
        os.utime(folder / candidate, (now, now))
        return True

    report = await reconcile(folder, [], still_unreferenced=touched, grace_seconds=3600, delete=True)  # fmt: skip
    assert report.deleted == 0 and name in os.listdir(folder)


# --- Finding 2: database/storage pairing --------------------------------------------------


def api_env(folder):
    return {"RFQ_API_ATTACHMENT_DIR": str(folder)}


def test_binding_is_written_only_for_the_api_pairing(tmp_path):
    folder = storage(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(folder)
    # Same directory spelled differently in the API setting: compared by inode.
    assert bind_storage(folder, DEV, target="development", environ=api_env(alias)) == "verified"
    marker = folder / BINDING_MARKER
    assert oct(marker.stat().st_mode & 0o777) == "0o600"
    assert pairing_status(folder, DEV) == "verified" and pairing_status(folder, TEST) == "mismatch"
    assert bind_storage(folder, DEV, target="development", environ=api_env(folder)) == "verified"
    with pytest.raises(PairingRefused):  # rebinding to another database is refused
        bind_storage(folder, TEST, target="development", environ=api_env(folder))
    assert "rfq_copilot" not in marker.read_text() and str(tmp_path) not in marker.read_text()


@pytest.mark.parametrize(
    ("target", "environ"),
    [
        ("test", "same"),  # the API never pairs with the test database
        ("development", None),  # API storage setting missing
        ("development", "other"),  # a different directory
        ("development", "relative"),
    ],
)
def test_binding_refused_without_a_verified_api_pairing(tmp_path, target, environ):
    folder = storage(tmp_path)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    env = {
        "same": api_env(folder),
        None: {},
        "other": api_env(other),
        "relative": {"RFQ_API_ATTACHMENT_DIR": "store"},
    }[environ]
    with pytest.raises(PairingRefused):
        bind_storage(folder, DEV, target=target, environ=env)
    assert not (folder / BINDING_MARKER).exists()


@pytest.mark.parametrize("kind", ["symlink", "loose_mode", "garbage", "oversized"])
def test_unsafe_or_malformed_marker_counts_as_unbound(tmp_path, kind):
    folder = storage(tmp_path)
    marker = folder / BINDING_MARKER
    good = json.dumps({"version": 2, "database": DEV})
    if kind == "symlink":
        (tmp_path / "elsewhere").write_text(good)
        marker.symlink_to(tmp_path / "elsewhere")
    else:
        marker.write_text({"loose_mode": good, "garbage": "{not json", "oversized": good + " " * 2000}[kind])  # fmt: skip
        marker.chmod(0o644 if kind == "loose_mode" else 0o600)
    assert pairing_status(folder, DEV) == "unbound"


class FakeCatalog:
    fingerprint_value = DEV

    def __init__(self, target, env_file):
        self.target = target

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fingerprint(self):
        return self.fingerprint_value

    async def references(self):
        return []

    async def still_unreferenced(self, name):
        return True


def run(monkeypatch, capsys, argv, fingerprint=DEV, environ=None):
    monkeypatch.setattr(command, "_Catalog", type("C", (FakeCatalog,), {"fingerprint_value": fingerprint}))  # fmt: skip
    for key, value in (environ or {}).items():
        monkeypatch.setenv(key, value)
    code = command.main([*argv, "--grace-hours", "1"])
    return code, json.loads(capsys.readouterr().out)


def test_delete_refused_when_unbound_but_dry_run_works(tmp_path, monkeypatch, capsys):
    folder = storage(tmp_path)
    orphan = stored_file(folder, 7200)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--delete"])
    assert (code, out) == (2, {"status": "refused", "error": "pairing_unverified"})
    assert orphan in os.listdir(folder)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder)])
    assert code == 0 and out["pairing"] == "unbound" and out["orphans_eligible"] == 1
    assert out["deleted"] == 0 and orphan in os.listdir(folder)


def test_development_storage_with_test_database_is_refused(tmp_path, monkeypatch, capsys):
    folder = storage(tmp_path)
    orphan = stored_file(folder, 7200)
    bind_storage(folder, DEV, target="development", environ=api_env(folder))
    # The audit scenario: development storage, but the queried database is the test one.
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--target", "test", "--delete"], fingerprint=TEST)  # fmt: skip
    assert code == 2 and out["error"] == "pairing_unverified" and orphan in os.listdir(folder)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--target", "test"], fingerprint=TEST)  # fmt: skip
    assert code == 0 and out["pairing"] == "mismatch" and out["deleted"] == 0


def test_bind_then_explicit_delete_removes_eligible_orphans(tmp_path, monkeypatch, capsys):
    folder = storage(tmp_path)
    orphan, young = stored_file(folder, 7200), stored_file(folder, 60)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--bind"], environ=api_env(folder))  # fmt: skip
    assert (code, out) == (0, {"status": "ok", "mode": "bind", "pairing": "verified"})
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--delete"])
    assert code == 0 and out["pairing"] == "verified" and out["deleted"] == 1
    assert orphan not in os.listdir(folder) and young in os.listdir(folder)
    assert out["unexpected_entries"] == 0  # the marker is not reported as stray content
    for value in (orphan, young, str(tmp_path)):
        assert value not in json.dumps(out)


def test_bind_refused_for_test_target_or_wrong_directory(tmp_path, monkeypatch, capsys):
    folder = storage(tmp_path)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--bind", "--target", "test"], environ=api_env(folder))  # fmt: skip
    assert code == 2 and out["error"] == "pairing_unverified"
    monkeypatch.delenv("RFQ_API_ATTACHMENT_DIR", raising=False)
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--bind"])
    assert code == 2 and not (folder / BINDING_MARKER).exists()
    with pytest.raises(SystemExit):  # --bind and --delete together are a usage error
        command.main(["--storage-dir", str(folder), "--bind", "--delete"])


# --- Marker version and database identity (Phase 3.5 completion) -------------------------


def write_marker(folder, content, mode=0o600):
    marker = folder / BINDING_MARKER
    marker.write_text(content if isinstance(content, str) else json.dumps(content))
    marker.chmod(mode)
    return marker


@pytest.mark.parametrize(
    "content",
    [
        {"database": DEV},  # no version
        {"version": "2", "database": DEV},
        {"version": True, "database": DEV},
        {"version": 2.0, "database": DEV},
        {"version": 3, "database": DEV},
        {"version": None, "database": DEV},
        {"version": 2, "database": DEV, "extra": 1},
        {"version": 2, "database": DEV.upper()},
        [2, DEV],
    ],
)
def test_marker_without_exact_supported_version_is_unbound(tmp_path, content):
    folder = storage(tmp_path)
    write_marker(folder, content)
    assert pairing_status(folder, DEV) == "unbound"


def test_version_one_marker_is_legacy_refused_and_never_overwritten(tmp_path, monkeypatch, capsys):
    folder = storage(tmp_path)
    orphan = stored_file(folder, 7200)
    legacy = json.dumps({"version": 1, "database": "a" * 64})
    marker = write_marker(folder, legacy)
    assert pairing_status(folder, DEV) == "legacy"
    code, out = run(monkeypatch, capsys, ["--storage-dir", str(folder), "--delete"])
    assert code == 2 and orphan in os.listdir(folder)
    with pytest.raises(PairingRefused):
        bind_storage(folder, DEV, target="development", environ=api_env(folder))
    assert marker.read_text() == legacy  # untouched
    assert bind_storage(folder, DEV, target="development", environ=api_env(folder), replace_legacy=True) == "verified"  # fmt: skip
    aside = [n for n in os.listdir(folder) if n.startswith(BINDING_MARKER + ".v1-")]
    assert len(aside) == 1 and (folder / aside[0]).read_text() == legacy  # kept, renamed
    assert pairing_status(folder, DEV) == "verified"


def test_invalid_marker_blocks_binding_until_inspected(tmp_path):
    folder = storage(tmp_path)
    marker = write_marker(folder, "{broken")
    with pytest.raises(PairingRefused):
        bind_storage(folder, DEV, target="development", environ=api_env(folder), replace_legacy=True)  # fmt: skip
    assert marker.read_text() == "{broken"


def test_replace_legacy_requires_bind(tmp_path):
    with pytest.raises(SystemExit):
        command.main(["--storage-dir", str(storage(tmp_path)), "--replace-legacy"])


def test_identity_distinguishes_clusters_and_recreated_databases(tmp_path):
    folder = storage(tmp_path)
    bind_storage(folder, DEV, target="development", environ=api_env(folder))
    other_cluster = database_fingerprint(CLUSTER + 1, "rfq_copilot_dev", 16384)  # same name, OID
    recreated = database_fingerprint(CLUSTER, "rfq_copilot_dev", 99999)  # dropped + recreated
    for fingerprint in (other_cluster, recreated, TEST):
        assert pairing_status(folder, fingerprint) == "mismatch"
    assert len({DEV, TEST, other_cluster, recreated}) == 4
