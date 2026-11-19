# Deferred migrations — not active

The active development contract is CURRENT, revision `20261118_01`, defined by
[`schema_contract.py`](../../src/rfq_copilot/schema_contract.py). Active revisions remain in
`alembic/versions/`; the normal Alembic configuration does not load this deferred directory.

The `NEXT` contract and its regression tests additionally inspect:

- `deferred/20261111_05_multi_customer_bindings.py`: proposed multi-customer staff bindings,
  with its original parent `20261111_06`.
- `deferred/20261116_02_merge_multi_customer.py`: schema-free merge of that proposal and CURRENT
  (`20261118_01`), preserving both revision histories.

These files are required by deferred-contract tests. Do not delete, move, apply or rewrite
them as repository cleanup. Their presence does not approve multi-customer activation.

Permission data is separate from migration history. The pinned `rbac-policy-v2` overlay
lives in `src/rfq_copilot/policies/` and is registered through `seeding/policy.py` after
baseline seeding, with explicit target confirmation.
