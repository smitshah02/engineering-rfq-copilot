"""Test-only staging of history that K2/K3 refuse to create (not a pytest module).

draft persistence tests must still prove that already-corrupted data is *detected*: rows changed
before K2 existed, or by the table owner or a superuser, whom triggers do not bind. The
only honest way to create such rows now is the owner's own power, so ``corrupted`` disables
every guard trigger of the named tables (K2 and K3: a table may have several, including the
K3 deferred constraint triggers), runs the tamper, and restores them:

- only on the guarded ``rfq_copilot_test`` database, checked on the very connection;
- only as the owner fixture role (the runtime role cannot ALTER TABLE: not the owner);
- only triggers named by the pinned guard revisions themselves (no caller-supplied
  identifiers);
- ``ALTER TABLE`` is transactional, so the fixture transaction's rollback undoes it even if
  the test fails in between; nothing is ever disabled globally or committed. On normal
  exit every K2 trigger is back in its entry state, which outside any corruption scope is
  ENABLE ALWAYS (``test_history_immutability_database`` also checks the state after a
  rollback and after nested scopes).
- Code under ``src/`` never imports this module (an offline test enforces it).

``ALTER TABLE`` takes an ACCESS EXCLUSIVE lock held to the end of the transaction: use it
only in single-connection fixture transactions (the API tests join that same connection),
never in the committed concurrency suites. It fails loudly ("pending trigger events") if a
deferred constraint event is queued on the table; the seeded fixture transaction runs
``SET CONSTRAINTS ALL IMMEDIATE``, so none is. Tests asserting that K2
*refuses* the same changes live in ``test_history_immutability_database.py``.
"""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from rfq_copilot.schema_contract import CURRENT, contract_guards

TEST_DATABASE = "rfq_copilot_test"
GUARDS: dict[str, tuple[str, ...]] = {}
for _trigger, _table, *_ in contract_guards(CURRENT)[1]:
    GUARDS[_table] = (*GUARDS.get(_table, ()), _trigger)
ALL_GUARD_TRIGGERS = frozenset(t for triggers in GUARDS.values() for t in triggers)


@asynccontextmanager
async def corrupted(session, *tables: str):
    """Within the fixture transaction, let the owner tamper with these business tables.

    Nesting is allowed for different tables: each context disables only triggers that are
    ENABLE ALWAYS on entry and, on exit, restores exactly the states it found, so an outer
    scope keeps its own triggers disabled until it exits itself.
    """
    assert await session.scalar(text("SELECT current_database()")) == TEST_DATABASE
    assert await session.scalar(text("SELECT current_setting('transaction_read_only')")) == "off"
    assert session.in_transaction()  # the fixture's transaction: ends in rollback
    # KeyError: not a guarded table.
    triggers = [(table, trigger) for table in tables for trigger in GUARDS[table]]
    entry = await trigger_states(session)
    assert set(entry) == ALL_GUARD_TRIGGERS
    assert all(entry[trigger] == "A" for _, trigger in triggers), "already disabled"
    for table, trigger in triggers:
        await session.execute(text(f"ALTER TABLE business.{table} DISABLE TRIGGER {trigger}"))
    yield
    for table, trigger in triggers:
        await session.execute(text(f"ALTER TABLE business.{table} ENABLE ALWAYS TRIGGER {trigger}"))  # fmt: skip
    assert await trigger_states(session) == entry


async def trigger_states(session) -> dict[str, str]:
    """``tgenabled`` of every K2/K3 trigger as this transaction sees it ('A' = ALWAYS)."""
    rows = await session.execute(
        text(
            "SELECT g.tgname, g.tgenabled::text FROM pg_trigger g "
            "JOIN pg_class c ON c.oid = g.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'business' AND NOT g.tgisinternal"
        )
    )
    return dict(rows.all())


def k2_refusal(error: IntegrityError) -> str | None:
    """The K2/K3 trigger that refused a statement (SQLSTATE 23000 naming it), else None."""
    adapted = error.orig
    if getattr(adapted, "sqlstate", None) != "23000":
        return None
    return getattr(getattr(adapted, "orig", None), "constraint_name", None)
