# Deferred migrations — NOT ACTIVE

2026-11-11: the approved Phase 3.5 revisions `20261111_02`, `_03`, `_04` and `_06` were
moved to `alembic/versions/` (active head `20261111_06`, contract `CURRENT` in
`rfq_copilot/schema_contract.py`). They have not been applied to the development database.

Only `deferred/20261111_05_multi_customer_bindings.py` remains here (multi-customer
staff, not approved; it revises `20261111_06`). Alembic does not load this folder.

Permission data is not a migration: the pinned `rbac-policy-v2` overlay
(`src/rfq_copilot/policies/`, `seeding/policy.py`) is applied after seeding.
