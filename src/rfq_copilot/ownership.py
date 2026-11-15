"""Principal ownership (moved verbatim from the Phase 5 conversation graph, Day 49 D49-03).

One implementation of the pseudonymous owner key shared by Phase 5 conversation threads and
Day 49 Supervisor recovery, so neither application module needs the raw LangGraph module.
The conversation graph re-exports both names unchanged (same objects).
"""

import hashlib

from rfq_copilot.api.auth import Principal


class ThreadAccessError(Exception):
    """One opaque answer for: missing trusted context, malformed, unknown or foreign thread."""

    def __init__(self) -> None:
        super().__init__("thread_unavailable")


def principal_owner(principal: Principal) -> str:
    # Fail closed: only a verified principal with both a customer and a user owns threads.
    if not isinstance(principal, Principal) or not principal.customer_id or not principal.user_id:
        raise ThreadAccessError()
    key = f"{principal.customer_id}\x00{principal.user_id}".encode()
    return hashlib.sha256(key).hexdigest()  # pseudonymous binding, never model-visible
