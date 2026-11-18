"""Opt-in runtime-aware classification for ``python -m rfq_copilot.integrity --runtime``.

The strict seed audit stays the default and is unchanged. With ``--runtime`` every seeded
row is still verified exactly against the approved source; rows outside the seed are
accepted only in ``RUNTIME_TABLES`` and only when the audited write paths explain each one:

- users: ``identity_operator`` / ``customer`` accounts, each with exactly one successful
  ``operator_admin`` provisioning event (seeded active administrator approver, a
  previously provisioned operator as requester or the single bootstrap, a verification
  reference, a human subject). Only an operator with a deactivation event may be inactive.
- user_customer_bindings: each row's grant/revoke ``identity_admin`` history alternates,
  starts with ``granted``, and reproduces ``created_at`` and the current ``revoked_at``.
- rfqs / rfq_line_items / idempotency_records / audit_events: each customer-portal RFQ
  (``rfq_submission``) has contiguous lines matching ``part_ids``, exactly one
  ``create_rfq`` receipt and one ``rfq.create`` event agreeing on actor, customer, result
  and transaction time, by a provisioned customer whose binding to that customer was
  active at creation. A later revocation does not invalidate it.
- rfq_attachments: each customer-portal upload (POST /api/v1/rfqs/{id}/attachments) belongs
  to a runtime customer-portal RFQ explained above and carries exactly what
  ``repositories.attachments.add_attachment`` writes: a server-generated ``ATT-<32 hex>`` id,
  a ``pdf``/``csv`` type, ``storage_ref`` ``<id>.<type>``, ``customer_supplied``, no revision,
  never a trusted instruction. A row on a seeded RFQ or with any other shape fails.
- workflow_runs (Checkpoint A1): only after the single ``workflow.queue_activate`` event
  (by a seeded active administrator); the RFQs it lists as historical have no run, every
  later customer-portal RFQ has exactly one, created in the RFQ's own transaction by its
  creator with the code-pinned graph and rule versions, plus exactly one matching
  ``workflow.enqueue`` event. Lease, attempt and outcome fields must be consistent with
  the status, and each ``workflow.attempt`` event must belong to its run, by the run's
  bound service identity (or none), within the run's claim generations; a settled run's
  latest generation must have recorded exactly its status and disposition.
- users (Checkpoint A2): ``rfq_workflow_agent`` service accounts with exactly one
  ``service.provision`` event (machine ``svc|`` subject, rbac-policy-v4) and bindings only
  to the customer named there, the first granted in the provisioning transaction.
- quotes / quote_versions / quote_line_items / pricing_calculations (Checkpoint A4): each
  runtime quote is linked by exactly one ``completed`` / ``draft_created`` run whose
  Supervisor attempt recorded ``ready_for_draft``; created by the service identity bound to
  that run's customer, as an untouched version-1 draft (one complete version under the
  pinned rules, contiguous lines summing to its total, its own calculation record), with
  exactly one ``create_quote_draft`` receipt keyed ``wf-<run>``, one ``quote_draft.create``
  event and the run's settling ``workflow.draft`` event, all in the one draft transaction.
  Any later human lifecycle row on a runtime quote is not explained here and fails.
- audit_events: anything not explained above (or an operator-tool refusal) fails.

Every other table must still equal the seed exactly. Binding activity is replayed per
transaction timestamp: a grant or revoke stamped in the same instant as the RFQ counts as
active (the two transactions cannot be ordered); a revoke strictly earlier fails closed.
"""

import re
from collections import defaultdict
from typing import get_args

from rfq_copilot import identity_admin, operator_admin, quote_drafts, rfq_submission
from rfq_copilot import workflow_queue as wq
from rfq_copilot.attachments import DocumentType
from rfq_copilot.integrity import check
from rfq_copilot.repositories.attachments import SOURCE_ROLE as UPLOAD_SOURCE_ROLE

RUNTIME_TABLES = frozenset(
    {"users", "user_customer_bindings", "rfqs", "rfq_line_items", "rfq_attachments", "idempotency_records", "audit_events",
     "workflow_runs", "quotes", "quote_versions", "quote_line_items", "pricing_calculations"}
)  # fmt: skip
_RFQ_ID = re.compile(r"^RFQ-[0-9a-f]{32}$")
_ATTACHMENT_ID = re.compile(r"^ATT-[0-9A-F]{32}$")  # attachments.new_attachment_id
_CORRELATION = "identity-admin-"  # identity_admin._audit
_GRANT, _REVOKE = identity_admin.OPERATIONS["grant"], identity_admin.OPERATIONS["revoke"]
_OPERATOR, _CUSTOMER = operator_admin.OPERATOR_ROLE, operator_admin.CUSTOMER_ROLE
_SERVICE = operator_admin.SERVICE_ROLE
_PROVISION = {
    _OPERATOR: operator_admin.OPERATIONS["provision"],
    _CUSTOMER: operator_admin.OPERATIONS["provision-customer"],
    _SERVICE: operator_admin.OPERATIONS["provision-service"],
}
_V4 = "applied:rbac-policy-v4"
_DEACTIVATE = operator_admin.OPERATIONS["deactivate"]
_DENIABLE = frozenset({*operator_admin.OPERATIONS.values(), *identity_admin.OPERATIONS.values()})
_PROVISION_KEYS = {"new_user_id", "approved_by", "requested_by", "verification_ref"}

USERS, BINDINGS, RFQS = "business.users", "business.user_customer_bindings", "business.rfqs"
EVENTS, RECEIPTS, LINES = "business.audit_events", "business.idempotency_records", "business.rfq_line_items"  # fmt: skip
RUNS, ATTACHMENTS = "orchestration.workflow_runs", "business.rfq_attachments"
QUOTES, VERSIONS, QUOTE_LINES, CALCULATIONS = "business.quotes", "business.quote_versions", "business.quote_line_items", "business.pricing_calculations"  # fmt: skip


def _human(subject) -> bool:
    try:
        operator_admin._human_subject(subject)
    except identity_admin.Refused:
        return False
    return True


def _admin_event(event) -> bool:
    return (
        event["rfq_id"] is None
        and isinstance(event["correlation_id"], str)
        and event["correlation_id"].startswith(_CORRELATION)
    )


def _binding_timeline(events, binding):
    """[(instant, active_before, active_after, granted_here)], checked against the row."""
    groups = defaultdict(lambda: [0, 0, 0])  # grants, revokes, "granted" results
    for event in events:
        group = groups[event["occurred_at"]]
        if event["operation"] == _GRANT:
            group[0] += 1
            group[2] += event["event_data"]["result"] == "granted"
        else:
            group[1] += 1
    state, first_grant, last_revoke, timeline = False, None, None, []
    for instant in sorted(groups):
        grants, revokes, granted = groups[instant]
        # Within one instant the order is unknown: accept any alternating sequence.
        if state:
            check(revokes in (grants, grants + 1), BINDINGS, "binding history does not alternate")
            after = revokes == grants
        else:
            check(grants in (revokes, revokes + 1), BINDINGS, "binding history does not alternate")
            after = grants == revokes + 1
        if grants and first_grant is None:
            first_grant = instant
            check(granted == 1, BINDINGS, "first grant is not 'granted'")
        else:
            check(granted == 0, BINDINGS, "binding granted twice")
        if revokes:
            last_revoke = instant
        timeline.append((instant, state, after, grants > 0))
        state = after
    check(first_grant == binding["created_at"], BINDINGS, "created_at differs from its grant")
    check(binding["revoked_at"] == (None if state else last_revoke), BINDINGS, "revoked_at differs from its history")  # fmt: skip
    return timeline


def _active_at(timeline, instant) -> bool:
    state = False
    for when, before, after, granted in timeline:
        if when == instant:
            return before or granted  # ponytail: same-instant revoke tolerated, see docstring
        if when > instant:
            break
        state = after
    return state


def verify_runtime(extras: dict[str, list[dict]], users: dict[str, dict], overlay: str) -> dict:
    """Explain every non-seed row of RUNTIME_TABLES or raise IntegrityVerificationError.

    ``extras``: per table, the stored rows outside the seed (seed rows are verified by the
    caller). ``users``: every stored user by id. ``overlay``: the audited overlay state.
    """
    events = extras["audit_events"]
    explained = set()
    by_kind = defaultdict(list)
    for event in events:
        check(
            event["agent_name"] is None
            and (event["operation"] == quote_drafts.AUDIT_OPERATION or (event["quote_id"] is None and event["quote_version"] is None)),
            EVENTS,
            "unexplained audit event",
        )  # fmt: skip
        by_kind[(event["operation"], event["outcome"])].append(event)

    # --- Accounts ------------------------------------------------------------------------
    new_users = {u["user_id"]: u for u in extras["users"]}
    roles = {u["role"] for u in new_users.values()}
    check(roles <= set(_PROVISION), USERS, "runtime user has an unprovisionable role")
    if _SERVICE in roles:
        check(overlay == _V4, USERS, "service accounts need rbac-policy-v4")
    if _CUSTOMER in roles:
        check(overlay in ("applied:rbac-policy-v3", _V4), USERS, "customer accounts need rbac-policy-v3")  # fmt: skip
    elif roles:
        check(overlay.startswith("applied:"), USERS, "operator accounts need a policy overlay")

    provisions = {}
    for event in [e for role in _PROVISION for e in by_kind[(_PROVISION[role], "success")]]:
        target = event["event_data"].get("new_user_id")
        check(target in new_users and target not in provisions, EVENTS, "provisioning event without exactly one user")  # fmt: skip
        provisions[target] = event
    deactivated = {}
    for event in by_kind[(_DEACTIVATE, "success")]:
        data = event["event_data"]
        target = data.get("target_user_id") if set(data) == {"target_user_id", "requested_by", "reason"} else None  # fmt: skip
        approver = users.get(event["actor_id"])
        check(
            target in new_users and new_users[target]["role"] == _OPERATOR and target not in deactivated
            and approver is not None and approver["role"] == operator_admin.APPROVER_ROLE and _admin_event(event),
            EVENTS,
            "deactivation event is inconsistent",
        )  # fmt: skip
        deactivated[target] = event
        explained.add(event["event_id"])

    bootstraps = 0
    for user_id, user in new_users.items():
        event = provisions.get(user_id)
        check(event is not None, USERS, "runtime user without provisioning event")
        check(event["operation"] == _PROVISION[user["role"]], USERS, "role differs from its provisioning event")  # fmt: skip
        data = event["event_data"]
        operator, service = user["role"] == _OPERATOR, user["role"] == _SERVICE
        keys = _PROVISION_KEYS | ({"bootstrap"} if operator else {"customer_id"} if service else set())  # fmt: skip
        check(set(data) == keys, EVENTS, "provisioning event data differs")
        approver = users.get(data["approved_by"])
        check(
            approver is not None
            and data["approved_by"] not in new_users
            and approver["role"] == operator_admin.APPROVER_ROLE
            and approver["active"] is True,
            EVENTS,
            "provisioning approver is not an active seeded administrator",
        )
        check(
            isinstance(data["verification_ref"], str) and operator_admin.VERIFICATION_REF.match(data["verification_ref"]) is not None,
            EVENTS,
            "provisioning verification reference missing",
        )  # fmt: skip
        if service:  # a machine identity in the dedicated namespace, never a human subject
            try:
                operator_admin._service_subject(user["authentication_subject"])
            except identity_admin.Refused:
                check(False, USERS, "service account without a service subject")
        else:
            check(_human(user["authentication_subject"]), USERS, "runtime user has a machine subject")  # fmt: skip
        check(_admin_event(event) and event["customer_id"] is None, EVENTS, "provisioning event is inconsistent")  # fmt: skip
        requester = data["requested_by"]
        if operator and data["bootstrap"] is True:
            bootstraps += 1
            check(requester is None and event["actor_id"] == data["approved_by"], EVENTS, "bootstrap event is inconsistent")  # fmt: skip
        else:
            check(
                requester in new_users
                and requester in provisions
                and new_users[requester]["role"] == _OPERATOR
                and requester not in (user_id, data["approved_by"])
                and event["actor_id"] == requester
                and provisions[requester]["occurred_at"] <= event["occurred_at"]
                and (not operator or data["bootstrap"] is False),
                EVENTS,
                "provisioning requester is not an earlier operator",
            )
        check(user["active"] is (user_id not in deactivated), USERS, "account state differs from its audit history")  # fmt: skip
        explained.add(event["event_id"])
    # ponytail: one bootstrap ever; re-bootstrapping after every operator left needs a new rule.
    check(bootstraps <= 1, EVENTS, "more than one bootstrap provisioning")

    # --- Bindings ------------------------------------------------------------------------
    history = defaultdict(list)
    for event in by_kind[(_GRANT, "success")] + by_kind[(_REVOKE, "success")]:
        data, grant = event["event_data"], event["operation"] == _GRANT
        operator = event["actor_id"]
        check(
            set(data) == ({"target_user_id", "reason", "result"} if grant else {"target_user_id", "reason"})
            and (not grant or data["result"] in ("granted", "reactivated"))
            and data["reason"] in identity_admin.REASONS
            and event["customer_id"] is not None
            and _admin_event(event),
            EVENTS,
            "binding event is inconsistent",
        )  # fmt: skip
        check(
            operator in new_users
            and new_users[operator]["role"] == _OPERATOR
            and operator != data["target_user_id"]
            and provisions[operator]["occurred_at"] <= event["occurred_at"],
            EVENTS,
            "binding event not by a provisioned operator",
        )
        history[(data["target_user_id"], event["customer_id"])].append(event)
        explained.add(event["event_id"])
    bindings = {(b["user_id"], b["customer_id"]): b for b in extras["user_customer_bindings"]}
    check(set(bindings) == set(history), BINDINGS, "binding without matching grant history")
    timelines = {pair: _binding_timeline(history[pair], binding) for pair, binding in bindings.items()}  # fmt: skip
    services = {u: provisions[u] for u, user in new_users.items() if user["role"] == _SERVICE}
    for user_id, event in services.items():  # bound to exactly the provisioned customer, at once
        customer = event["event_data"]["customer_id"]
        check(
            {c for u, c in bindings if u == user_id} == {customer}
            and bindings[(user_id, customer)]["created_at"] == event["occurred_at"],
            BINDINGS,
            "service binding differs from its provisioning",
        )

    # --- Customer-portal RFQs ------------------------------------------------------------
    rfqs = {r["rfq_id"]: r for r in extras["rfqs"]}
    lines, receipts, creations = defaultdict(list), defaultdict(list), defaultdict(list)
    for line in extras["rfq_line_items"]:
        lines[line["rfq_id"]].append(line)
    draft_receipts = []
    for receipt in extras["idempotency_records"]:
        if receipt["operation"] == quote_drafts.OPERATION:  # A4: verified by _verify_drafts
            draft_receipts.append(receipt)
            continue
        check(receipt["operation"] == rfq_submission.OPERATION, RECEIPTS, "receipt outside the runtime vocabulary")  # fmt: skip
        receipts[receipt["correlation_id"]].append(receipt)
    for event in by_kind[(rfq_submission.AUDIT_OPERATION, "success")]:
        creations[event["rfq_id"]].append(event)
    check(set(lines) <= set(rfqs), LINES, "line item of a non-runtime RFQ")
    check(set(receipts) <= set(rfqs), RECEIPTS, "orphan receipt")
    check(set(creations) <= set(rfqs), EVENTS, "creation event without its RFQ")
    for rfq_id, rfq in rfqs.items():
        check(
            _RFQ_ID.match(rfq_id) is not None
            and rfq["source_type"] == rfq_submission.SOURCE_TYPE
            and rfq["status"] == "draft"
            and rfq["row_version"] == 1,
            RFQS,
            "runtime RFQ outside the customer-portal contract",
        )
        actor, customer, at = rfq["created_by"], rfq["customer_id"], rfq["submitted_at"]
        creator = new_users.get(actor)
        check(creator is not None and creator["role"] == _CUSTOMER, RFQS, "RFQ creator is not a provisioned customer")  # fmt: skip
        check(len(receipts[rfq_id]) == 1, RECEIPTS, "RFQ without exactly one receipt")
        receipt = receipts[rfq_id][0]
        check(
            (receipt["actor_id"], receipt["customer_id"], receipt["committed_at"]) == (actor, customer, at)
            and receipt["result_json"] == {"rfq_id": rfq_id, "status": "draft"},
            RECEIPTS,
            "receipt differs from its RFQ",
        )  # fmt: skip
        ordered = sorted(lines[rfq_id], key=lambda line: line["line_number"])
        count = len(ordered)
        check(
            count >= 1
            and [line["line_number"] for line in ordered] == list(range(1, count + 1))
            and rfq["part_ids"] == [line["part_id"] for line in ordered],
            LINES,
            "line items differ from their RFQ",
        )
        check(len(creations[rfq_id]) == 1, EVENTS, "RFQ without exactly one creation event")
        event = creations[rfq_id][0]
        check(
            (event["actor_id"], event["customer_id"], event["correlation_id"], event["occurred_at"]) == (actor, customer, rfq_id, at)
            and event["event_data"] == {"source_type": rfq_submission.SOURCE_TYPE, "line_count": count},
            EVENTS,
            "creation event differs from its RFQ",
        )  # fmt: skip
        check(_active_at(timelines.get((actor, customer), []), at), RFQS, "no active binding at creation")  # fmt: skip
        explained.add(event["event_id"])

    # --- Customer-portal attachments (only on the runtime RFQs explained above) ----------
    for row in extras["rfq_attachments"]:
        check(
            _ATTACHMENT_ID.match(row["attachment_id"]) is not None
            and row["rfq_id"] in rfqs
            and row["document_type"] in get_args(DocumentType)
            and row["storage_ref"] == f"{row['attachment_id']}.{row['document_type']}"
            and row["source_role"] == UPLOAD_SOURCE_ROLE
            and row["revision"] is None
            and row["is_trusted_instruction"] is False,
            ATTACHMENTS,
            "runtime attachment outside the customer-portal upload contract",
        )

    _verify_queue(extras["workflow_runs"], rfqs, by_kind, users, services, explained)
    _verify_drafts(extras, rfqs, by_kind, services, draft_receipts, explained)

    # --- Refusals by the operator tools, then nothing else -------------------------------
    for event in events:
        if event["outcome"] == "denied" and event["operation"] in _DENIABLE:
            data = event["event_data"]
            check(
                "refusal" in data
                and set(data) <= {"refusal", "target_user_id"}
                and (event["actor_id"] is None or event["actor_id"] in users)
                and _admin_event(event),
                EVENTS,
                "refusal event is inconsistent",
            )
            explained.add(event["event_id"])
    check(all(e["event_id"] in explained for e in events), EVENTS, "unexplained audit event")

    return {
        "services": len(services),
        "workflow_runs": len(extras["workflow_runs"]),
        "quotes": len(extras["quotes"]),
        "operators": sum(u["role"] == _OPERATOR for u in new_users.values()),
        "customers": sum(u["role"] == _CUSTOMER for u in new_users.values()),
        "bindings": len(bindings),
        "revoked_bindings": sum(b["revoked_at"] is not None for b in bindings.values()),
        "rfqs": len(rfqs),
        "rfq_line_items": len(extras["rfq_line_items"]),
        "rfq_attachments": len(extras["rfq_attachments"]),
        "idempotency_records": len(extras["idempotency_records"]),
        "audit_events": len(events),
    }


def _verify_queue(runs, rfqs, by_kind, users, services, explained) -> None:
    """Checkpoint A1: the activation boundary, each run's provenance and its attempt trail."""
    activations = by_kind[(wq.ACTIVATION_OPERATION, "success")]
    check(len(activations) <= 1, EVENTS, "more than one queue activation")
    historical = None
    for event in activations:
        data, approver = event["event_data"], users.get(event["actor_id"])
        ids = data.get("historical_rfq_ids") if set(data) == {"historical_rfq_ids", "graph_version"} else None  # fmt: skip
        check(
            isinstance(ids, list)
            and ids == sorted(set(ids))
            and set(ids) <= set(rfqs)
            and data["graph_version"] == wq.GRAPH_VERSION
            and approver is not None
            and approver["user_id"] not in services
            and approver["role"] == wq.APPROVER_ROLE
            and (event["customer_id"], event["rfq_id"], event["correlation_id"]) == (None, None, wq.ACTIVATION_CORRELATION),
            EVENTS,
            "queue activation event is inconsistent",
        )  # fmt: skip
        historical = set(ids)
        explained.add(event["event_id"])

    enqueued = defaultdict(list)
    for event in by_kind[(wq.ENQUEUE_OPERATION, "success")]:
        enqueued[event["rfq_id"]].append(event)
    attempts = defaultdict(list)
    for event in by_kind[(wq.ATTEMPT_OPERATION, "success")] + by_kind[(wq.ATTEMPT_OPERATION, "error")]:  # fmt: skip
        attempts[event["event_data"].get("workflow_run_id")].append(event)
    by_rfq = defaultdict(list)
    for run in runs:
        by_rfq[run["rfq_id"]].append(run)
    check(set(by_rfq) <= set(rfqs), RUNS, "workflow run of a non-portal RFQ")
    for rfq_id, rfq in rfqs.items():  # the boundary: set membership, never a timestamp
        wanted = 0 if historical is None or rfq_id in historical else 1
        check(len(by_rfq[rfq_id]) == wanted and len(enqueued[rfq_id]) == wanted, RUNS, "RFQ without exactly its processing request")  # fmt: skip
    check(set(enqueued) <= set(by_rfq), EVENTS, "enqueue event without its run")
    check(set(attempts) <= {str(r["workflow_run_id"]) for r in runs}, EVENTS, "attempt event without its run")  # fmt: skip
    drafts = defaultdict(list)  # A4: workflow.draft events
    for event in by_kind[(wq.DRAFT_OPERATION, "success")] + by_kind[(wq.DRAFT_OPERATION, "error")]:  # fmt: skip
        drafts[event["event_data"].get("workflow_run_id")].append(event)
    check(set(drafts) <= {str(r["workflow_run_id"]) for r in runs}, EVENTS, "draft event without its run")  # fmt: skip

    for run in runs:
        rfq, run_id = rfqs[run["rfq_id"]], str(run["workflow_run_id"])
        status, disposition = run["status"], run["disposition"]
        check(
            (run["customer_id"], run["initiated_by"], run["correlation_id"], run["created_at"])
            == (rfq["customer_id"], rfq["created_by"], rfq["rfq_id"], rfq["submitted_at"])
            and (run["graph_version"], run["rule_versions"], run["source_snapshot_versions"], run["checkpoint_ref"])
            == (wq.GRAPH_VERSION, wq.RULE_VERSIONS, {}, None),
            RUNS,
            "workflow run differs from its RFQ",
        )  # fmt: skip
        leased = run["lease_owner"] is not None
        check(
            disposition in wq.STATUS_DISPOSITIONS.get(status, ())
            and leased == (status in ("running", "drafting"))
            and (status != "running" or run["attempt_count"] >= 1)
            and (status != "drafting" or run["draft_attempt_count"] >= 1)
            and (status != "failed" or run["last_error"] is not None)
            and (run["quote_id"] is not None) == (status == "completed" and disposition == "draft_created")
            and run["claim_generation"] >= run["attempt_count"] + run["draft_attempt_count"],
            RUNS,
            "workflow run state is inconsistent",
        )  # fmt: skip
        (event,) = enqueued[rfq["rfq_id"]]
        check(
            (event["actor_id"], event["customer_id"], event["correlation_id"], event["occurred_at"], event["event_data"])
            == (rfq["created_by"], rfq["customer_id"], rfq["rfq_id"], rfq["submitted_at"], {"workflow_run_id": run_id}),
            EVENTS,
            "enqueue event differs from its run",
        )  # fmt: skip
        explained.add(event["event_id"])
        trail = attempts.get(run_id, [])
        for attempt in trail:
            data, actor = attempt["event_data"], attempt["actor_id"]
            service = services.get(actor)
            check(
                set(data) == wq.ATTEMPT_KEYS
                and (attempt["customer_id"], attempt["rfq_id"], attempt["correlation_id"]) == (run["customer_id"], run["rfq_id"], run["rfq_id"])
                and (actor is None or (service is not None and service["event_data"]["customer_id"] == run["customer_id"]))
                and isinstance(data["claim_generation"], int) and 1 <= data["claim_generation"] <= run["claim_generation"]
                and isinstance(data["attempt"], int) and 0 <= data["attempt"] <= run["attempt_count"]
                and data["disposition"] in wq.STATUS_DISPOSITIONS.get(data["status"], ())
                and attempt["agent_name"] is None,
                EVENTS,
                "attempt event is inconsistent",
            )  # fmt: skip
            explained.add(attempt["event_id"])
        draft_trail = drafts.get(run_id, [])
        for event in draft_trail:  # A4: the drafting phase, by the run's own service identity
            data, actor = event["event_data"], event["actor_id"]
            service = services.get(actor)
            check(
                set(data) == wq.DRAFT_KEYS
                and (event["customer_id"], event["rfq_id"], event["correlation_id"]) == (run["customer_id"], run["rfq_id"], run["rfq_id"])
                and (actor is None or (service is not None and service["event_data"]["customer_id"] == run["customer_id"]))
                and isinstance(data["claim_generation"], int) and 1 <= data["claim_generation"] <= run["claim_generation"]
                and isinstance(data["draft_attempt"], int) and 0 <= data["draft_attempt"] <= run["draft_attempt_count"]
                and data["disposition"] in wq.STATUS_DISPOSITIONS.get(data["status"], ())
                and (data["quote_id"] is not None) == (data["disposition"] == "draft_created")
                and data["quote_id"] in (None, run["quote_id"])  # never another or a missing quote
                and event["agent_name"] is None and event["quote_id"] is None,
                EVENTS,
                "draft event is inconsistent",
            )  # fmt: skip
            explained.add(event["event_id"])
        if draft_trail or run["draft_attempt_count"]:  # drafted only after A3 said ready
            check(
                any((a["event_data"]["status"], a["event_data"]["disposition"]) == ("completed", "ready_for_draft") for a in trail),
                RUNS,
                "drafting without a ready Supervisor outcome",
            )  # fmt: skip
        if status not in ("queued", "running", "drafting"):  # settled: the latest claim says so
            final = [a["event_data"] for a in trail + draft_trail if a["event_data"]["claim_generation"] == run["claim_generation"]]  # fmt: skip
            check(
                any((d["status"], d["disposition"], d["error"], d.get("quote_id")) == (status, disposition, run["last_error"], run["quote_id"]) for d in final),
                RUNS,
                "settled run without its attempt record",
            )  # fmt: skip


def _verify_drafts(extras, rfqs, by_kind, services, receipts, explained) -> None:
    """Checkpoint A4: every runtime quotation row is one service-created draft of one run."""
    runs = {r["quote_id"]: r for r in extras["workflow_runs"] if r["quote_id"] is not None}
    quotes = {q["quote_id"]: q for q in extras["quotes"]}
    check(set(quotes) == set(runs), QUOTES, "runtime quote without exactly its workflow run")
    versions, lines = defaultdict(list), defaultdict(list)
    for version in extras["quote_versions"]:
        versions[version["quote_id"]].append(version)
    for line in extras["quote_line_items"]:
        lines[(line["quote_id"], line["quote_version"])].append(line)
    calculations = {c["calculation_record_id"]: c for c in extras["pricing_calculations"]}
    check(set(versions) <= set(quotes), VERSIONS, "runtime version of a non-runtime quote")
    check({q for q, _ in lines} <= set(quotes), QUOTE_LINES, "runtime line of a non-runtime quote")  # fmt: skip
    referenced = {v["calculation_record_id"] for vs in versions.values() for v in vs}
    check(set(calculations) == referenced, CALCULATIONS, "calculation record without its draft")
    by_key = defaultdict(list)
    for receipt in receipts:
        by_key[receipt["idempotency_key"]].append(receipt)
    created = defaultdict(list)
    for event in by_kind[(quote_drafts.AUDIT_OPERATION, "success")]:
        created[event["quote_id"]].append(event)
    check(set(created) <= set(quotes), EVENTS, "draft creation event without its quote")
    check(len(by_key) == len(receipts) and set(by_key) == {wq.draft_key(r["workflow_run_id"]) for r in runs.values()}, RECEIPTS, "draft receipt without exactly its run")  # fmt: skip
    pinned = (wq.RULE_VERSIONS["manufacturing"], wq.RULE_VERSIONS["pricing"], wq.RULE_VERSIONS["approval"], quote_drafts.WORKFLOW_RULE_VERSION)  # fmt: skip

    for quote_id, quote in quotes.items():
        run, at, creator = runs[quote_id], quote["created_at"], quote["created_by"]
        service = services.get(creator)
        check(
            quote["rfq_id"] == run["rfq_id"] and run["rfq_id"] in rfqs
            and service is not None and service["event_data"]["customer_id"] == run["customer_id"]
            and (quote["status"], quote["current_version"], quote["row_version"]) == ("draft", 1, 1)
            and all(quote[c] is None for c in ("submitted_by", "approved_version", "approver_id", "approval_timestamp", "rejection_reason", "released_by", "released_at", "legacy_idempotency_key")),
            QUOTES,
            "runtime quote is not an untouched service draft of its run",
        )  # fmt: skip
        check(len(versions[quote_id]) == 1, VERSIONS, "runtime quote without exactly one version")
        (version,) = versions[quote_id]
        calculation = calculations.get(version["calculation_record_id"])
        check(
            (version["version"], version["is_current"], version["approval_valid"], version["snapshot_completeness"]) == (1, True, False, "complete")
            and (version["created_by"], version["created_at"], version["rfq_id"]) == (creator, at, quote["rfq_id"])
            and (version["manufacturing_rule_version"], version["pricing_rule_version"], version["approval_rule_version"], version["workflow_rule_version"]) == pinned
            and calculation is not None
            and (calculation["rfq_id"], calculation["created_by"], calculation["created_at"]) == (quote["rfq_id"], creator, at)
            and (calculation["calculation_id"], calculation["quote_total_usd"], calculation["pricing_rule_version"]) == (version["calculation_id"], version["total_usd"], version["pricing_rule_version"]),
            VERSIONS,
            "draft version differs from its calculation or pinned rules",
        )  # fmt: skip
        ordered = sorted(lines[(quote_id, 1)], key=lambda line: line["line_number"])
        check(
            ordered
            and [line["line_number"] for line in ordered] == list(range(1, len(ordered) + 1))
            and sum(line["line_total_usd"] for line in ordered) == version["total_usd"]
            and not any((q, v) for q, v in lines if q == quote_id and v != 1),
            QUOTE_LINES,
            "draft lines differ from their version",
        )
        (receipt,) = by_key[wq.draft_key(run["workflow_run_id"])]
        check(
            (receipt["actor_id"], receipt["customer_id"], receipt["correlation_id"], receipt["committed_at"]) == (creator, run["customer_id"], run["rfq_id"], at)
            and receipt["result_json"].get("quote_id") == quote_id,
            RECEIPTS,
            "draft receipt differs from its quote",
        )  # fmt: skip
        check(len(created[quote_id]) == 1, EVENTS, "draft quote without exactly one creation event")
        (event,) = created[quote_id]
        check(
            (event["actor_id"], event["customer_id"], event["rfq_id"], event["quote_version"], event["correlation_id"], event["occurred_at"]) == (creator, run["customer_id"], run["rfq_id"], 1, run["rfq_id"], at)
            and event["event_data"].get("rule_versions") == list(pinned)
            and event["event_data"].get("calculation_id") == version["calculation_id"],
            EVENTS,
            "draft creation event differs from its quote",
        )  # fmt: skip
        explained.add(event["event_id"])
