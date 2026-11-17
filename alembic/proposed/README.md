# Deferred migrations — NOT ACTIVE

2026-11-11: the approved Phase 3.5 revisions `20261111_02`, `_03`, `_04` and `_06` were
moved to `alembic/versions/` (active head `20261111_06`, contract `CURRENT` in
`rfq_copilot/schema_contract.py`). They have not been applied to the development database.

Deferred here (Alembic does not load this folder; only the `NEXT` contract does):

- `deferred/20261111_05_multi_customer_bindings.py`: multi-customer staff, not approved.
  It revises `20261111_06`, its original parent (restored on Day 54B: the Day 54A rebase
  onto K2 rewrote the parent of a revision the guarded test lifecycle had applied).
- `deferred/20261116_02_merge_multi_customer.py`: schema-free merge of `20261111_05` and
  the active head (`20261116_03`, K4 on K3), so NEXT has a single head without rewriting either history.

Permission data is not a migration: the pinned `rbac-policy-v2` overlay
(`src/rfq_copilot/policies/`, `seeding/policy.py`) is applied after seeding.
