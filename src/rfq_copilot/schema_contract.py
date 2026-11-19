"""Pinned database contracts: which schema and which post-baseline data are trusted.

The seeding preflight (``seeding/safety.py``), the import verifier (``seeding/loader.py``),
the integrity audit (``integrity.py``) and ``migration_safety.include_name`` read the
active contract from here.

- ``CURRENT`` (Checkpoint A4, written 2026-11-18): head ``20261118_01``, WORKFLOW plus the
  quotation-drafting state on ``workflow_runs`` (``drafting``, ``draft_attempt_count``, the
  ``quote_id`` link, three CHECKs, two indexes). Same overlays as WORKFLOW. Its measured digest and catalog counts are pinned below and verified by the guarded test lifecycle.
- ``WORKFLOW`` (Checkpoints A1/A2, 2026-11-17): head ``20261117_02``, the workflow queue
  columns, CHECKs and active-run index and the ``rfq_workflow_agent`` role value; accepts
  ``rbac-policy-v2``, ``-v3`` and ``-v4``. Measured; kept as a record.
- ``CUSTOMER`` (Milestone 2 Step 1): head ``20261117_01``, K5 plus the ``customer`` role
  value; accepts ``rbac-policy-v2`` and ``rbac-policy-v3``. Kept as a record.
- ``K5`` (human decision, measured 2026-11-17): head ``20261116_04`` (decision-authority
  guards: decisions only by the ``rfq_quote_decision`` group, actor entitlement and
  separation, expiry, commit-time history/request binding, rejected-quote resubmission).
  Its two constraint triggers change the shape digest. Kept as a record; its guards remain
  the active guard allowlist.
- ``K4`` (integrity, kept as a record): head ``20261116_03`` (human lifecycle
  guards: submit, approve, reject, release) on top of K3. K4 adds no table, column,
  constraint or index, so K3's measured shape digest and catalog counts hold by
  construction (the migration lifecycle test re-measures them). Guards: K2, K3 and K4, each
  file pinned independently in ``GUARD_FILE_SHA256``; a later guard revision may drop an
  earlier trigger (``DROPPED_TRIGGERS``). NEXT's digest is still unmeasured (fails closed).
- ``K3`` (integrity, measured 2026-11-16): head ``20261116_01``, kept as a record.
- ``K2`` (integrity, measured 2026-11-16): head ``20261115_01``, kept as a record.
- ``ACTIVATED_06`` (active 2026-11-11 to integrity): head ``20261111_06`` (rfq_assignments,
  token_revocations, the upload-idempotency operation, the identity_operator role value),
  the ORM models (36 tables), and the SHA-256-pinned ``rbac-policy-v2`` overlay
  (``seeding/policy.py``) as the only accepted post-seed permission data. Kept as a record.
- ``PREVIOUS``: the contract before the authorization hardening activation (head ``20261110_01``), kept as
  a record and for tests; it accepts no overlay.
- ``NEXT``: the deferred multi-customer revision ``20261111_05`` (original parent
  ``20261111_06``) joined with CURRENT by the schema-free merge ``20261116_02``. Not active.

A database whose revision differs from the active contract's reports "Alembic revision is
not current" until its upgrade is separately authorized and applied.
"""

import hashlib
import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "alembic" / "versions"
DEFERRED = ROOT / "alembic" / "proposed" / "deferred"
MULTI_CUSTOMER_INDEX = "uq_user_customer_bindings_active_user"
_METADATA: dict = {}


@dataclass(frozen=True)
class SchemaContract:
    name: str
    revision: str
    shape_digest: str
    catalog_counts: Mapping[str, int]
    policy_overlays: frozenset[str] = frozenset()
    # NEXT (20261111_05): several active bindings per user; the one-active index is gone.
    multi_customer_bindings: bool = False
    version_locations: tuple[Path, ...] = (VERSIONS,)
    # The revisions whose FUNCTIONS/TRIGGERS, together, are the only routines and triggers
    # allowed in the managed schemas; () means none at all.
    guards_revisions: tuple[str, ...] = ()

    def alembic_config(self):
        import os

        from alembic.config import Config

        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option(
            "version_locations", os.pathsep.join(str(p) for p in self.version_locations)
        )
        return config

    def metadata(self):
        """The ORM metadata, without the one-active-binding index under multi-customer."""
        from sqlalchemy import MetaData

        from rfq_copilot.models import Base

        if not self.multi_customer_bindings:
            return Base.metadata
        if self.name not in _METADATA:
            copy = MetaData(naming_convention=Base.metadata.naming_convention)
            for table in Base.metadata.sorted_tables:
                table.to_metadata(copy)
            bindings = copy.tables["business.user_customer_bindings"]
            bindings.indexes = {i for i in bindings.indexes if i.name != MULTI_CUSTOMER_INDEX}
            _METADATA[self.name] = copy
        return _METADATA[self.name]


PREVIOUS = SchemaContract(
    name="approved-20261110_01",
    revision="20261110_01",
    shape_digest="3c9f9e4462e955f7af30bc4f383d6875286e1afd6cb5bc24726d1784eb52b482",
    catalog_counts=MappingProxyType({"p": 34, "f": 66, "u": 14, "c": 210}),
)

ACTIVATED_06 = SchemaContract(
    name="activated-20261111_06",
    revision="20261111_06",
    shape_digest="513626616a200ac09f87b0543d35da048431586058055b7d07013ffe8aa6319f",
    # PREVIOUS + rfq_assignments (1 PK, 3 FK, 3 CHECK) + token_revocations (1 PK, 2 FK,
    # 5 CHECK); 04 and 06 replace one CHECK each.
    catalog_counts=MappingProxyType({"p": 36, "f": 71, "u": 14, "c": 218}),
    policy_overlays=frozenset({"rbac-policy-v2"}),
)

# Placeholders that are never a SHA-256 or a real count: every shape, catalog and guard
# comparison fails closed until the user measures the values on the guarded test database
# (or the file with ``shasum -a 256``) and pins them here.
UNMEASURED = "MEASURE_ON_TEST_DATABASE"
UNMEASURED_COUNTS = MappingProxyType({"unmeasured": -1})

K2 = SchemaContract(
    name="k2-20261115_01",
    revision="20261115_01",
    # ACTIVATED_06 + quote_versions.created_xact; measured on the guarded test DB 2026-11-16.
    shape_digest="312f13380c6a52e89410a769982233190b9add04faca4425b95f655865203610",
    catalog_counts=ACTIVATED_06.catalog_counts,  # K2 adds no constraint
    policy_overlays=ACTIVATED_06.policy_overlays,
    guards_revisions=("20261115_01",),
)

K3 = SchemaContract(
    name="k3-20261116_01",
    revision="20261116_01",
    # K2 + the K3 calculation-record, version-evidence and guard changes. Measured on the
    # guarded test DB 2026-11-16 (two lifecycles after the downgrade fix, identical).
    shape_digest="c5ad9cd71c5e96a5cd9b33a1fd9857db8953fceb41873b600c06a387d7c53778",
    catalog_counts=MappingProxyType({"p": 36, "f": 72, "u": 17, "c": 227}),
    policy_overlays=K2.policy_overlays,
    guards_revisions=("20261115_01", "20261116_01"),
)

K4 = SchemaContract(
    name="k4-20261116_03",
    revision="20261116_03",
    # K3 + the K4 lifecycle triggers and functions only: no shape or catalog change.
    shape_digest=K3.shape_digest,
    catalog_counts=K3.catalog_counts,
    policy_overlays=K3.policy_overlays,
    guards_revisions=(*K3.guards_revisions, "20261116_03"),
)

K5 = SchemaContract(
    name="k5-20261116_04",
    revision="20261116_04",
    # K4 + the K5 decision-authority triggers and functions. No key/CHECK/index change (the
    # catalog counts hold), but the two deferred constraint triggers are pg_constraint rows
    # (contype 't') in the shape: measured on the guarded test DB (K5 preflight, 2026-11-17).
    shape_digest="9f84633742a079c0bc047ce907c4f92ea6a700cffde54dba6d326abf35dcd259",
    catalog_counts=K4.catalog_counts,
    policy_overlays=K4.policy_overlays,
    guards_revisions=(*K4.guards_revisions, "20261116_04"),
)

CUSTOMER = SchemaContract(
    name="customer-20261117_01",
    revision="20261117_01",
    # K5 + the customer role value: ck_roles_rule_01 keeps its name and type, so the shape
    # (names/types) and catalog counts hold by construction, as K4 on K3. Re-measured by
    # tests/test_policy_overlay_lifecycle.py before it is trusted. Accepts rbac-policy-v3
    # (a superset of v2) as well as v2 itself.
    shape_digest=K5.shape_digest,
    catalog_counts=K5.catalog_counts,
    policy_overlays=frozenset({"rbac-policy-v2", "rbac-policy-v3"}),
    guards_revisions=K5.guards_revisions,  # no trigger or function: not a guard revision
)

WORKFLOW = SchemaContract(
    name="workflow-20261117_02",
    revision="20261117_02",
    # CUSTOMER + 6 workflow_runs columns, 6 CHECKs, the active-run unique partial index and
    # the rfq_workflow_agent role value. Measured on the guarded test DB (2026-11-17).
    shape_digest="27930009ea9de18c7504bc7dda32f137e23800078bcba110ca7f1b522405208a",
    catalog_counts=MappingProxyType({"p": 36, "f": 72, "u": 17, "c": 233}),
    policy_overlays=frozenset({"rbac-policy-v2", "rbac-policy-v3", "rbac-policy-v4"}),
    guards_revisions=K5.guards_revisions,  # no trigger or function: not a guard revision
)

CURRENT = SchemaContract(
    name="drafting-20261118_01",
    revision="20261118_01",
    # WORKFLOW + draft_attempt_count, quote_id (one FK), rule_08/09/10 (three CHECKs; rule_01,
    # 03, 05 and 06 replaced in place) and two indexes (no constraint). Measure on the guarded
    # test DB with tests/test_policy_overlay_lifecycle.py -k current_contract_pins.
    shape_digest="ab32fc22717087839254a5a3b894ba2bb93b4d110655b51384e2338a4929bb72",
    catalog_counts=MappingProxyType({"p": 36, "f": 73, "u": 17, "c": 236}),
    policy_overlays=WORKFLOW.policy_overlays,
    guards_revisions=K5.guards_revisions,  # no trigger or function: not a guard revision
)

# Deferred 20261111_05 (multi-customer staff), integrated and verified by
# tests/test_multi_customer_lifecycle.py but NOT active. integrity restored its original
# parent 20261111_06 and joins it with CURRENT through the schema-free merge 20261116_02
# (alembic/proposed/deferred). Activating it means moving both revisions into
# alembic/versions/, dropping the index from the UserCustomerBinding model, setting
# _ACTIVE = NEXT (with version_locations=(VERSIONS,)) and updating the schema tests.
NEXT = SchemaContract(
    name="next-20261116_02",
    revision="20261116_02",
    # K4's measurement (3278543607ce…03e3) no longer holds: K5's two constraint triggers are in
    # the shape. Re-measure with tests/test_multi_customer_lifecycle.py (prints the digest).
    # The A1/A2 queue (20261117_02) measured 06da0203…7230d; A4 drafting (20261118_01) changes
    # the shape again: re-measure with tests/test_multi_customer_lifecycle.py -k next_contract_pins.
    shape_digest="f2381ec6490cc193aba3e2ed8011bd55ddd4ab56e8c952942924a40556acc41b",
    catalog_counts=CURRENT.catalog_counts,  # 05 drops an index only (verify_definitions checks)
    policy_overlays=CURRENT.policy_overlays,
    multi_customer_bindings=True,
    version_locations=(VERSIONS, DEFERRED),
    guards_revisions=CURRENT.guards_revisions,
)

_ACTIVE = CURRENT


def active() -> SchemaContract:
    return _ACTIVE


# SHA-256 of each guard revision file's exact bytes, pinned independently of the file, so an
# edited migration can never redefine the allowlist the schema preflight trusts.
GUARD_FILE_SHA256 = MappingProxyType(
    {
        "20261115_01": "10021900678979e4ce1c1d48c64fbcafa8251fc724c11a96659c702ab5bf082d",
        # Pin with: shasum -a 256 alembic/versions/20261116_01_k3_calculation_provenance.py
        "20261116_01": "f214da512cfc840133719572300a9862a9d0fcba5ae963d61e944bc552df675f",
        # Pin with: shasum -a 256 alembic/versions/20261116_03_k4_approval_lifecycle.py
        "20261116_03": "0f85654e8de8612564e72500c18ac43b36ceb38c5a4877fd868929c1efbf08d7",
        # Pin with: shasum -a 256 alembic/versions/20261116_04_k5_decision_authority.py
        # Re-pin required: the file changed (commit-time bindings); the old pin was c81f8433….
        "20261116_04": "a7dc970fd130c895c3f632edd92b384dd8ba9eef186ad475c38bede5f0e61797",
    }
)
TRIGGER_KINDS = frozenset({"before", "deferred"})


def _guard_module(revision: str):
    """The revision module, loaded only after its bytes match ``GUARD_FILE_SHA256``."""
    (path,) = VERSIONS.glob(f"{revision}_*.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != GUARD_FILE_SHA256.get(revision):
        raise ValueError(f"Guard revision {revision} differs from its pinned SHA-256.")
    spec = importlib.util.spec_from_file_location(f"rfq_copilot_revision_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@cache
def history_guards(revision: str) -> tuple[Mapping[str, str], tuple[tuple, ...]]:
    """``(FUNCTIONS, TRIGGERS)`` exactly as the revision file creates them.

    Read from the migration itself so the allowlist cannot drift from what was installed,
    after the file's bytes match ``GUARD_FILE_SHA256`` (``ValueError`` otherwise): FUNCTIONS
    maps name -> PL/pgSQL body (``pg_proc.prosrc``); TRIGGERS holds
    ``(trigger, table, function, events)`` (K2: BEFORE ... FOR EACH ROW) or
    ``(trigger, table, function, events, kind)`` (K3) triggers on ``business`` tables.
    """
    module = _guard_module(revision)
    return MappingProxyType(dict(module.FUNCTIONS)), tuple(module.TRIGGERS)


@cache
def dropped_triggers(revision: str) -> tuple[str, ...]:
    """Trigger names an (SHA-256-verified) revision drops: ``DROPPED_TRIGGERS`` (K4)."""
    return tuple(entry[0] for entry in getattr(_guard_module(revision), "DROPPED_TRIGGERS", ()))


def contract_guards(contract: SchemaContract) -> tuple[Mapping[str, str], tuple[tuple, ...]]:
    """The union of the contract's guard revisions: ``(functions, triggers)``, every trigger
    as ``(trigger, table, function, events, kind)``. A name defined twice, a trigger kind
    other than ``before``/``deferred`` or a trigger whose function no revision defines is
    a ``ValueError``: the allowlist is exact or it is nothing."""
    functions: dict[str, str] = {}
    triggers: dict[str, tuple] = {}
    for revision in contract.guards_revisions:
        defined, created = history_guards(revision)
        for name in dropped_triggers(revision):
            if name not in triggers:
                raise ValueError(f"Guard trigger {name} is dropped before it was created.")
            del triggers[name]
        for name, body in defined.items():
            if name in functions:
                raise ValueError(f"Guard function {name} is defined twice.")
            functions[name] = body
        for entry in created:
            trigger, table, function, events, kind = (*entry, "before")[:5]
            if trigger in triggers or kind not in TRIGGER_KINDS:
                raise ValueError(f"Guard trigger {trigger} is duplicated or of unknown kind.")
            triggers[trigger] = (trigger, table, function, tuple(events), kind)
    if any(t[2] not in functions for t in triggers.values()):
        raise ValueError("A guard trigger calls a function no guard revision defines.")
    return MappingProxyType(functions), tuple(triggers.values())
