# Implementation limits and deployment decisions

The full local customer-to-released-quotation demonstration is implemented, including flat
panels and L-brackets. These limits describe current guarantees and decisions, not a backlog
of already completed frontend, submission or background-processing work.

## Identity, scope and human authority

Local sign-in uses short-lived development JWTs for provisioned identities. Production login,
refresh/logout lifecycle and delegated service issuance remain deployment work. The worker's
existing signing-key provider is explicitly development-only. A customer role and distinct
customer permissions are implemented; staff and service roles do not imply unrestricted access.

CURRENT revision `20261118_01` retains one active customer binding per user. Multi-customer staff
activation remains deferred in [the migration proposal](../../alembic/proposed/README.md).
Selecting a header alone cannot change this schema or grant access.

Required engineering review fails closed without verified completion evidence. Approval
attests to source context and requires independent authority; an environment flag or model
assessment cannot complete that review. Automatic expiry/supersession transitions remain
incomplete, although validity is checked before binding approval and release. Released
history can remain readable after its validity date.

## Evidence and recovery

Stored hashes, calculation records and audit trails establish consistency and provenance
relationships, not cryptographically signed specialist authorship. The drafter recomputes
stored requirements. Durable signed specialist evidence remains incomplete.

PostgreSQL queue claims, leases, generation fences and idempotent writes support multiple
consumers and restart recovery. Process-local Supervisor custody and recovery metadata are
not cross-worker continuation checkpoints. Interrupted evaluations may rerun model/read-only
tool execution; there is no exactly-once inference guarantee.

## Manufacturing and planning

Explicit arbitrary requested-operation lists remain outside the Part contract. Supported
routing is derived from pinned manufacturing rules. Weekly capacity and inventory checks
provide planning estimates, without reservation or shipment guarantees. Source freshness,
planning horizons and operational snapshot selection need explicit operator policy.

A read-only MCP snapshot and business-write revalidation are distinct guarantees. Quotation
persistence and human decisions recheck stored requirements, current policy and evidence.
The private frozen reference retains a documented hole-count validation limitation;
application validation enforces the configured scalar limit independently.

## Operations and public reproducibility

Foreground local services require manual startup/restart. Production TLS, secrets management,
identity integration, process supervision, monitoring, retention and recovery acceptance
remain outstanding. Gateway forwarding timeouts do not cover all body-reception time;
review ingress protection before broader network exposure. Unexpected internal exception
logs require an export/redaction policy; optional Supervisor telemetry has a separate,
metadata-only allowlist.

The public smoke selection is dataset-free. Full database regression requires the private
source package and isolated test roles/database. Live server/model/export checks require
separate opt-in authorization. Offline/mocked success does not prove live readiness.

The historical Langfuse credential finding remains a publication blocker until its owner
confirms the key was fictitious or revoked. Current placeholder configuration does not
erase historical exposure; no history rewriting is part of routine cleanup.
