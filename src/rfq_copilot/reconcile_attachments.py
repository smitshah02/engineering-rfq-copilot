"""Find (and, only with ``--delete``, remove) attachment files that have no metadata row.

An upload whose process dies after storing the file but before COMMIT, or whose COMMIT
outcome was unknown (the route then keeps the file), leaves a file that no
``business.rfq_attachments.storage_ref`` names. This operator command reconciles them:

    python -m rfq_copilot.reconcile_attachments --storage-dir /abs/dir --env-file .env
    ... --bind                           # once: pair this storage with this database
    ... --grace-hours 24 --delete        # remove eligible orphans (explicit only)

Safety:
- Dry run by default; never run at application startup.
- **Pairing.** ``--delete`` needs the storage directory to be bound to the database being
  queried: a marker file (version 2) in the directory holds a fingerprint of that
  database (cluster ``system_identifier``, name and OID, hashed; no credentials). A
  marker without exactly ``"version": 2`` is not trusted; a version-1 marker (name and
  OID only) is reported as ``legacy`` and kept, never overwritten or deleted, until the
  operator runs ``--bind --replace-legacy``, which renames it aside first. ``--bind`` writes it only for the development
  target (the API's database) and only when ``--storage-dir`` is the same directory as
  the process's ``RFQ_API_ATTACHMENT_DIR`` (the API's own setting). A missing,
  unreadable or different marker refuses deletion; a dry run reports the pairing.
- Only server-named regular files (``ATT-<32 hex>.pdf|csv``) are candidates. Symlinks,
  directories and other names are counted and left alone.
- Only files older than the grace period (at least 1 hour, default 24) are candidates,
  so an upload still in progress is never touched.
- The directory is opened with the upload path's own checks (trusted ancestors,
  ``O_NOFOLLOW``, owner, mode 0700). Every operation is relative to that descriptor.
- ``flock`` on the directory keeps two reconcilers from running together.
- **Removal is rename, verify, unlink.** After the database is asked again, the file is
  atomically renamed to an unpredictable private name, then opened (``O_NOFOLLOW``) and
  checked with ``fstat``: regular file, same device, inode, size and mtime (ns) as at
  the scan, and still older than the grace period. Inode numbers alone are not trusted
  (filesystems reuse them). Anything else is renamed back without overwriting
  (``link``, which fails if the name is taken) or, if the name was taken meanwhile, kept
  under the private name and counted. Nothing is deleted on doubt.
- The database is opened read-only. Output is aggregate counts only: no names, ids or
  document contents. ``--list-missing`` additionally lists the server-generated
  attachment ids whose row names a file that is not in storage (no filenames, customers
  or contents). Those rows are never changed here: remediation is a manual, audited
  operator decision (see the runbook in phase_3_technical_debt_resolution.md).
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from rfq_copilot.attachments import _open_directory

STORED_NAME = re.compile(r"^ATT-[0-9A-F]{32}\.(pdf|csv)$")
QUARANTINE_PREFIX = ".reconcile-"
BINDING_MARKER = ".rfq-copilot-storage-binding"
MARKER_VERSION = 2
MAX_MARKER_BYTES = 1024
MIN_GRACE_SECONDS = 3600
DEFAULT_GRACE_HOURS = 24


class ReconcileBusy(RuntimeError):
    """Another reconciler holds the storage directory lock."""


class PairingRefused(RuntimeError):
    """The storage directory is not verifiably paired with this database."""


@dataclass(frozen=True)
class Report:
    mode: str
    files: int = 0
    referenced: int = 0
    orphans_in_grace: int = 0
    orphans_eligible: int = 0
    deleted: int = 0
    kept_on_recheck: int = 0
    quarantined_kept: int = 0
    quarantine_entries: int = 0
    unexpected_entries: int = 0
    rows_missing_file: int = 0


async def reconcile(
    directory: Path,
    referenced: Iterable[str],
    *,
    still_unreferenced: Callable[[str], Awaitable[bool]],
    grace_seconds: int = DEFAULT_GRACE_HOURS * 3600,
    delete: bool = False,
    now: float | None = None,
) -> Report:
    """Compare stored files with the referenced ``storage_ref`` values.

    Pairing is the caller's job (``main`` checks it before passing ``delete=True``).
    """
    if grace_seconds < MIN_GRACE_SECONDS:
        raise ValueError("grace period must be at least one hour")
    referenced = set(referenced)
    clock = time.time if now is None else (lambda: now)
    counts = dict.fromkeys(
        ("files", "referenced", "orphans_in_grace", "orphans_eligible", "deleted",
         "kept_on_recheck", "quarantined_kept", "quarantine_entries", "unexpected_entries"),
        0,
    )  # fmt: skip
    folder = _open_directory(directory, create=False)
    try:
        try:
            fcntl.flock(folder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReconcileBusy("another reconciler is running") from None
        present = set()
        for name in sorted(os.listdir(folder)):
            if name == BINDING_MARKER:
                continue
            info = os.stat(name, dir_fd=folder, follow_symlinks=False)
            if name.startswith(QUARANTINE_PREFIX):
                counts["quarantine_entries"] += 1  # left by an interrupted run; never removed
                continue
            if not STORED_NAME.match(name) or not stat.S_ISREG(info.st_mode):
                counts["unexpected_entries"] += 1
                continue
            counts["files"] += 1
            present.add(name)
            if name in referenced:
                counts["referenced"] += 1
            elif clock() - info.st_mtime < grace_seconds:
                counts["orphans_in_grace"] += 1
            else:
                counts["orphans_eligible"] += 1
                if delete:
                    outcome = await _remove_if_still_orphan(
                        folder, name, info, still_unreferenced, grace_seconds, clock
                    )
                    counts[outcome] += 1
        missing = len(referenced - present)
    finally:
        os.close(folder)  # also releases the lock
    return Report("delete" if delete else "dry-run", **counts, rows_missing_file=missing)


def _same_file(info: os.stat_result, before: os.stat_result) -> bool:
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return stat.S_ISREG(info.st_mode) and all(getattr(info, k) == getattr(before, k) for k in keys)


async def _remove_if_still_orphan(folder, name, before, still_unreferenced, grace, clock) -> str:
    """Return the counter to increment: deleted, kept_on_recheck or quarantined_kept."""
    if not await still_unreferenced(name):
        return "kept_on_recheck"  # a row appeared: keep the file
    private = f"{QUARANTINE_PREFIX}{secrets.token_hex(16)}"
    try:
        # Atomic: whatever is at ``name`` now (perhaps a replacement) moves to a name no
        # upload uses and nobody else can predict, so it cannot be swapped after the check.
        os.rename(name, private, src_dir_fd=folder, dst_dir_fd=folder)
    except FileNotFoundError:
        return "kept_on_recheck"
    verified = False
    try:
        descriptor = os.open(private, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        descriptor = None  # a symlink or something unopenable: not ours to delete
    if descriptor is not None:
        try:
            info = os.fstat(descriptor)
            verified = _same_file(info, before) and clock() - info.st_mtime >= grace
        finally:
            os.close(descriptor)
    if verified:
        os.unlink(private, dir_fd=folder)
        return "deleted"
    try:  # put it back without overwriting anything created at ``name`` meanwhile
        os.link(private, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
    except FileExistsError:
        return "quarantined_kept"
    os.unlink(private, dir_fd=folder)
    return "kept_on_recheck"


def missing_attachment_ids(directory: Path, referenced: Iterable[str]) -> tuple[str, ...]:
    """Attachment ids whose ``storage_ref`` names a file absent from the directory."""
    folder = _open_directory(directory, create=False)
    try:
        present = set(os.listdir(folder))
    finally:
        os.close(folder)
    return tuple(
        sorted(
            ref.rsplit(".", 1)[0]
            for ref in set(referenced)
            if STORED_NAME.match(ref) and ref not in present
        )  # fmt: skip
    )


# --- Database/storage pairing ---------------------------------------------------------------


def database_fingerprint(system_identifier: int, name: str, oid: int) -> str:
    """A non-secret identity for one database of one PostgreSQL cluster.

    ``system_identifier`` differs between clusters created by ``initdb``; name and OID
    differ between databases of one cluster, and a dropped and recreated database gets a
    new OID. A logical restore (``pg_restore``) into another cluster or database gets a
    new identity. A physical copy (``pg_basebackup``, file-level clone) keeps all three:
    such a copy must be re-bound to its own storage (see the operator runbook).
    """
    return hashlib.sha256(
        f"rfq-copilot-storage:{system_identifier}:{name}:{oid}".encode()
    ).hexdigest()


def read_binding(directory: Path) -> str | None:
    """The fingerprint the storage directory is bound to; None if absent, unsafe or invalid.

    A version-1 marker returns the sentinel ``"legacy"``.
    """
    folder = _open_directory(directory, create=False)
    try:
        descriptor = os.open(
            BINDING_MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder
        )
    except OSError:
        os.close(folder)
        return None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return None
        data = os.read(descriptor, MAX_MARKER_BYTES + 1)
    finally:
        os.close(descriptor)
        os.close(folder)
    try:
        marker = json.loads(data) if len(data) <= MAX_MARKER_BYTES else None
    except ValueError:
        return None
    if not isinstance(marker, dict) or set(marker) != {"version", "database"}:
        return None
    version, value = marker["version"], marker["database"]
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        return None
    if type(version) is int and version == 1:
        return "legacy"
    if type(version) is not int or version != MARKER_VERSION:
        return None  # missing, unknown or wrong-typed version (true, 2.0, "2")
    return value


def pairing_status(directory: Path, fingerprint: str) -> str:
    """verified | mismatch | legacy | unbound. Only ``verified`` permits deletion."""
    bound = read_binding(directory)
    if bound is None:
        return "unbound"
    if bound == "legacy":
        return "legacy"
    return "verified" if secrets.compare_digest(bound, fingerprint) else "mismatch"


def bind_storage(
    directory: Path,
    fingerprint: str,
    *,
    target: str,
    environ=None,
    replace_legacy: bool = False,
) -> str:
    """Write the pairing marker. Only the API's own pairing may be bound.

    The API uses the development database and ``RFQ_API_ATTACHMENT_DIR``; the directory
    must be that same directory (compared by device and inode, not by spelling).
    """
    environ = os.environ if environ is None else environ
    configured = environ.get("RFQ_API_ATTACHMENT_DIR")
    if target != "development" or not configured or not os.path.isabs(configured):
        raise PairingRefused("binding requires the API's database and storage settings")
    folder = _open_directory(directory, create=False)
    try:
        try:
            api = os.stat(configured)
        except OSError:
            raise PairingRefused("the API storage directory is not available") from None
        mine = os.fstat(folder)
        if (api.st_dev, api.st_ino) != (mine.st_dev, mine.st_ino):
            raise PairingRefused("storage directory is not the API's storage directory")
        existing = pairing_status(directory, fingerprint)
        if existing == "verified":
            return "verified"
        if existing == "mismatch":
            raise PairingRefused("storage directory is bound to a different database")
        if existing == "legacy":
            if not replace_legacy:
                raise PairingRefused("a version-1 marker exists; rerun with --replace-legacy")
            # Kept for the record under a new name; never deleted or overwritten.
            aside = f"{BINDING_MARKER}.v1-{secrets.token_hex(8)}"
            os.rename(BINDING_MARKER, aside, src_dir_fd=folder, dst_dir_fd=folder)
        elif _marker_present(folder):
            raise PairingRefused("an unreadable or invalid marker exists; inspect it manually")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(BINDING_MARKER, flags, 0o600, dir_fd=folder)
        try:
            marker = {"version": MARKER_VERSION, "database": fingerprint}
            os.write(descriptor, json.dumps(marker).encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(folder)
    return "verified"


def _marker_present(folder: int) -> bool:
    try:
        os.stat(BINDING_MARKER, dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


class _Catalog:
    """The read-only database side: fingerprint, references and the per-file recheck."""

    def __init__(self, target: str, env_file: Path | None) -> None:
        from rfq_copilot.config import DatabaseConfig
        from rfq_copilot.database import Database

        self.database = Database(DatabaseConfig.from_env(target, env_file=env_file), read_only=True)  # fmt: skip

    async def __aenter__(self):
        await self.database.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self.database.__aexit__(*exc)

    async def fingerprint(self) -> str:
        from sqlalchemy import text

        async with self.database.session() as session:
            system_identifier, name, oid = (
                await session.execute(
                    text(
                        "SELECT (SELECT system_identifier FROM pg_control_system()), d.datname, d.oid "
                        "FROM pg_database d WHERE d.datname = current_database()"
                    )
                )
            ).one()
        return database_fingerprint(int(system_identifier), name, int(oid))

    async def references(self) -> list[str]:
        from sqlalchemy import select

        from rfq_copilot.models import RFQAttachment

        async with self.database.session() as session:
            return list((await session.scalars(select(RFQAttachment.storage_ref))).all())

    async def still_unreferenced(self, name: str) -> bool:
        from sqlalchemy import func, select

        from rfq_copilot.models import RFQAttachment

        async with self.database.session() as session:
            count = await session.scalar(
                select(func.count()).where(RFQAttachment.storage_ref == name)
            )
        return count == 0


async def _run(args) -> dict:
    async with _Catalog(args.target, args.env_file) as catalog:
        fingerprint = await catalog.fingerprint()
        if args.bind:
            pairing = bind_storage(
                args.storage_dir,
                fingerprint,
                target=args.target,
                replace_legacy=args.replace_legacy,
            )
            return {"mode": "bind", "pairing": pairing}
        pairing = pairing_status(args.storage_dir, fingerprint)
        if args.delete and pairing != "verified":
            raise PairingRefused(pairing)  # checked before anything is listed or removed
        references = await catalog.references()
        report = await reconcile(
            args.storage_dir,
            references,
            still_unreferenced=catalog.still_unreferenced,
            grace_seconds=int(args.grace_hours * 3600),
            delete=args.delete,
        )
    result = {**asdict(report), "pairing": pairing}
    if args.list_missing:
        result["missing_attachment_ids"] = list(
            missing_attachment_ids(args.storage_dir, references)
        )
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--target", choices=("development", "test"), default="development")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--delete", action="store_true", help="remove eligible orphans")
    action.add_argument("--bind", action="store_true", help="pair storage with the API database")
    parser.add_argument(
        "--list-missing", action="store_true", help="list attachment ids whose file is missing"
    )
    parser.add_argument(
        "--replace-legacy", action="store_true", help="with --bind: set a version-1 marker aside"
    )
    args = parser.parse_args(argv)
    if not args.storage_dir.is_absolute():
        parser.error("--storage-dir must be absolute")
    if args.replace_legacy and not args.bind:
        parser.error("--replace-legacy requires --bind")
    try:
        result = asyncio.run(_run(args))
    except PairingRefused:
        print(json.dumps({"status": "refused", "error": "pairing_unverified"}))
        return 2
    except (OSError, ValueError, RuntimeError) as error:  # DatabaseError is a RuntimeError
        # Fixed messages only: no paths, names or database details.
        kind = "busy" if isinstance(error, ReconcileBusy) else type(error).__name__
        print(json.dumps({"status": "error", "error": kind}))
        return 1
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
