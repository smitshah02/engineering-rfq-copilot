"""Trusted directory paths, checked through descriptors (POSIX only).

``open_trusted_parent(path)`` returns a descriptor for ``path``'s parent directory and
the final name. The parent is resolved once (``realpath``, so system symlinks such as
macOS ``/var`` -> ``/private/var`` are followed), then opened component by component
from ``/`` with ``O_NOFOLLOW``. Each directory on the way, the parent included, must be
owned by root or this process's user, and must not be writable by group or others
unless it has the sticky bit (e.g. ``/tmp``), where only an entry's owner can rename it.

The final name is never resolved: callers open it relative to the descriptor with
``O_NOFOLLOW`` and apply their own, stricter checks.
"""

import os
import stat


class UntrustedPathError(OSError):
    """A directory on the path is a symlink, owned by another user, or writable by others."""


def trusted_ancestor(info: os.stat_result) -> bool:
    owner_ok = info.st_uid in (0, os.geteuid())
    writable_by_others = info.st_mode & 0o022
    return owner_ok and (not writable_by_others or bool(info.st_mode & stat.S_ISVTX))


def open_trusted_parent(path: os.PathLike | str) -> tuple[int, str]:
    folder, name = os.path.split(os.fspath(path))
    if not name or name in (".", ".."):
        raise UntrustedPathError("path must name an entry")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        if not trusted_ancestor(os.fstat(descriptor)):
            raise UntrustedPathError("untrusted directory on path")
        for part in os.path.realpath(folder or ".").split(os.sep):
            if not part:
                continue
            try:
                child = os.open(part, flags | os.O_NOFOLLOW, dir_fd=descriptor)
            except OSError:
                raise UntrustedPathError("untrusted directory on path") from None
            os.close(descriptor)
            descriptor = child
            if not trusted_ancestor(os.fstat(descriptor)):
                raise UntrustedPathError("untrusted directory on path")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, name
