"""Pinned database contracts: which schema and which post-baseline data are trusted.

The seeding preflight (``seeding/safety.py``), the import verifier (``seeding/loader.py``),
the integrity audit (``integrity.py``) and ``migration_safety.include_name`` read the
active contract from here.

- ``CURRENT`` (active since the Phase 3.5 code activation, 2026-11-11): head
  ``20261111_06`` (rfq_assignments, token_revocations, the upload-idempotency operation,
  the identity_operator role value), the ORM models (36 tables), and the SHA-256-pinned
  ``rbac-policy-v2`` overlay (``seeding/policy.py``) as the only accepted post-seed
  permission data. Revision ``20261111_05`` stays deferred in ``alembic/proposed/``.
- ``PREVIOUS``: the contract before activation (head ``20261110_01``), kept as a record
  and for tests; it accepts no overlay. The development database is still at this
  revision until its upgrade is separately authorized, so its integrity check reports
  "Alembic revision is not current" until then.
"""

from collections.abc import Mapping
from dataclasses import dataclass
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

CURRENT = SchemaContract(
    name="activated-20261111_06",
    revision="20261111_06",
    shape_digest="513626616a200ac09f87b0543d35da048431586058055b7d07013ffe8aa6319f",
    # PREVIOUS + rfq_assignments (1 PK, 3 FK, 3 CHECK) + token_revocations (1 PK, 2 FK,
    # 5 CHECK); 04 and 06 replace one CHECK each.
    catalog_counts=MappingProxyType({"p": 36, "f": 71, "u": 14, "c": 218}),
    policy_overlays=frozenset({"rbac-policy-v2"}),
)

# Deferred 20261111_05 (multi-customer staff), integrated and verified by
# tests/test_multi_customer_lifecycle.py but NOT active: activating it means moving the
# revision into alembic/versions/, dropping the index from the UserCustomerBinding model,
# setting _ACTIVE = NEXT (with version_locations=(VERSIONS,)) and updating the schema tests.
NEXT = SchemaContract(
    name="next-20261111_05",
    revision="20261111_05",
    shape_digest="fc06095a3a6aec7a460720e7e52b1e5ff02ad894e31152117d65b9b021386f6e",
    catalog_counts=MappingProxyType({"p": 36, "f": 71, "u": 14, "c": 218}),  # index only
    policy_overlays=frozenset({"rbac-policy-v2"}),
    multi_customer_bindings=True,
    version_locations=(VERSIONS, DEFERRED),
)

_ACTIVE = CURRENT


def active() -> SchemaContract:
    return _ACTIVE
