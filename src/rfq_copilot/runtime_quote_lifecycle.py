"""Explain supported human activity before validating each original workflow draft.

No seed exemption, status allowlist shortcut or new transition policy: reconstruct the
service's row-version sequence and bind every step to its request, history, event and
receipt. Historical expiry is checked at the action time, never at audit time.
"""

import hashlib
import re
from collections import defaultdict
from itertools import product
from types import SimpleNamespace

from rfq_copilot import quote_approval as approval
from rfq_copilot import quote_approval_policy as policy
from rfq_copilot import quote_drafts as drafts
from rfq_copilot import quote_evidence as evidence
from rfq_copilot import quote_versions as revisions
from rfq_copilot.api.auth import Principal
from rfq_copilot.canonical import fingerprint
from rfq_copilot.integrity import check
from rfq_copilot.repositories.idempotency import valid_key

_SHA = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = {
    "submitted": approval.SUBMIT,
    "approved": approval.APPROVE,
    "rejected": approval.REJECT,
    "released": approval.RELEASE,
}
_RESET = (
    "submitted_by",
    "approved_version",
    "approver_id",
    "approval_timestamp",
    "rejection_reason",
    "released_by",
    "released_at",
    "legacy_idempotency_key",
)


def _require(condition, message, table="quotes"):
    check(condition, f"business.{table}", message)


def _one(rows, message, table):
    _require(len(rows) == 1, message, table)
    return rows[0]


def verify_lifecycles(extras, users, authority, timelines, explained):
    """Return the original draft projection only after every later mutation is explained."""
    from rfq_copilot.runtime_integrity import _active_at, _human

    quotes = {q["quote_id"]: q for q in extras["quotes"]}
    versions, histories, requests = defaultdict(list), defaultdict(list), defaultdict(list)
    events, receipts = defaultdict(list), defaultdict(list)
    calculations = {c["calculation_record_id"]: c for c in extras["pricing_calculations"]}
    for table, grouped in (
        ("quote_versions", versions),
        ("approval_history", histories),
        ("approval_requests", requests),
    ):
        for row in extras.get(table, []):
            _require(row["quote_id"] in quotes, "orphan lifecycle row", table)
            grouped[row["quote_id"]].append(row)
    for event in extras["audit_events"]:
        if event["operation"] in {*approval.AUDIT.values(), *revisions.CREATION_OPERATIONS}:
            _require(event["quote_id"] in quotes, "orphan lifecycle event", "audit_events")
            _require(event["outcome"] == "success", "unsupported lifecycle outcome", "audit_events")
            events[(event["quote_id"], event["quote_version"], event["operation"])].append(event)
    for receipt in extras["idempotency_records"]:
        if receipt["operation"] in {*approval.AUDIT, revisions.OPERATION}:
            result = receipt["result_json"]
            _require(
                isinstance(result, dict) and result.get("quote_id") in quotes,
                "orphan lifecycle receipt",
                "idempotency_records",
            )
            receipts[(result["quote_id"], result.get("version"), receipt["operation"])].append(
                receipt
            )

    bindings = {
        (b["user_id"], b["customer_id"]): b for b in authority.get("user_customer_bindings", [])
    }
    grants = {
        (g["role"], g["permission"]) for g in authority.get("role_permissions", []) if g["allowed"]
    }

    def human(actor, customer, operation, at):
        user = users.get(actor)
        _require(
            user is not None
            and isinstance(user.get("authentication_subject"), str)
            and _human(user["authentication_subject"])
            and (user["role"], operation) in grants,
            "invalid lifecycle actor or grant",
            "approval_history",
        )
        # Seed identities are protected exactly. Runtime deactivation may follow a valid
        # historical action; the account checks in verify_runtime explain that event.
        inactive_later = any(
            e["operation"] == "operator.deactivate"
            and e["outcome"] == "success"
            and e["event_data"].get("target_user_id") == actor
            and e["occurred_at"] >= at
            for e in extras["audit_events"]
        )
        _require(user["active"] or inactive_later, "inactive lifecycle actor", "approval_history")
        pair, binding = (actor, customer), bindings.get((actor, customer))
        active = (
            _active_at(timelines[pair], at)
            if pair in timelines
            else (
                binding is not None
                and binding["created_at"] <= at
                and (binding["revoked_at"] is None or binding["revoked_at"] >= at)
            )
        )
        _require(active, "lifecycle actor without historical customer binding", "approval_history")
        if operation in {approval.APPROVE, approval.REJECT}:
            _require(
                user["role"] == approval.APPROVER_ROLE, "invalid decision role", "approval_history"
            )

    consumed_events, consumed_receipts = set(), set()
    initial_quotes, initial_versions, initial_calculations = [], [], set()
    version_keys = {(v["quote_id"], v["version"]) for vs in versions.values() for v in vs}
    _require(
        all(
            (line["quote_id"], line["quote_version"]) in version_keys
            for line in extras["quote_line_items"]
        ),
        "orphan quotation line",
        "quote_line_items",
    )
    _require(
        all(
            line["quote_line_id"]
            == f"{line['quote_id']}-V{line['quote_version']}-L{line['line_number']}"
            for line in extras["quote_line_items"]
        ),
        "forged quotation line identity",
        "quote_line_items",
    )
    for quote_id, quote in quotes.items():
        vs = sorted(versions[quote_id], key=lambda v: v["version"])
        _require(
            vs and [v["version"] for v in vs] == list(range(1, len(vs) + 1)),
            "noncontiguous quotation versions",
            "quote_versions",
        )
        customer = next(
            (r["customer_id"] for r in extras["rfqs"] if r["rfq_id"] == quote["rfq_id"]), None
        )
        _require(customer is not None, "lifecycle quotation without runtime RFQ")
        state, row_version = "draft", 1
        expected = {key: None for key in _RESET}
        known_requests, known_histories = set(), set()
        for number, version in enumerate(vs, 1):
            _require(
                version["is_current"] is (number == len(vs)),
                "corrupted current-version flag",
                "quote_versions",
            )
            _require(
                version["quote_version_id"] == f"{quote_id}-V{number}",
                "wrong version identity",
                "quote_versions",
            )
            if number > 1:
                _require(state in revisions.REVISABLE, "impossible revision transition")
                human(version["created_by"], customer, revisions.OPERATION, version["created_at"])
                event = _one(
                    events[(quote_id, number, revisions.AUDIT_OPERATION)],
                    "revision without exactly one event",
                    "audit_events",
                )
                receipt = _one(
                    receipts[(quote_id, number, revisions.OPERATION)],
                    "revision without exactly one receipt",
                    "idempotency_records",
                )
                result, data = receipt["result_json"], event["event_data"]
                _require(
                    data.get("previous_version") == number - 1
                    and result.get("previous_version") == number - 1
                    and data.get("evaluation_fingerprint")
                    != vs[number - 2]["evaluation_fingerprint"],
                    "unexplained or unchanged revision",
                    "quote_versions",
                )
                pins = [
                    version[k]
                    for k in (
                        "manufacturing_rule_version",
                        "pricing_rule_version",
                        "approval_rule_version",
                        "workflow_rule_version",
                    )
                ]
                _require(
                    pins
                    == [
                        vs[0][k]
                        for k in (
                            "manufacturing_rule_version",
                            "pricing_rule_version",
                            "approval_rule_version",
                            "workflow_rule_version",
                        )
                    ],
                    "runtime revision changed pinned rules",
                    "quote_versions",
                )
                expected_result = {
                    "quote_id": quote_id,
                    "rfq_id": quote["rfq_id"],
                    "customer_id": customer,
                    "version": number,
                    "status": "revised_draft",
                    "currency": version["currency"],
                    "total_usd": str(version["total_usd"]),
                    "calculation_id": version["calculation_id"],
                    "evaluation_fingerprint": version["evaluation_fingerprint"],
                    "rule_versions": pins,
                    "expiry_date": version["expiry_date"].isoformat(),
                    "human_approval_required": True,
                    "approved": False,
                    "released": False,
                }
                _require(
                    all(result.get(k) == value for k, value in expected_result.items())
                    and all(
                        data.get(k) == result.get(k)
                        for k in (
                            "evaluation_fingerprint",
                            "calculation_id",
                            "rule_versions",
                            "outcome",
                        )
                    ),
                    "revision result differs from immutable version",
                    "idempotency_records",
                )
                _bound_step(
                    event,
                    receipt,
                    quote,
                    version,
                    customer,
                    version["created_by"],
                    version["created_at"],
                )
                consumed_events.add(event["event_id"])
                consumed_receipts.add(receipt["receipt_id"])
                row_version += 1
                state = "revised_draft"
                expected.update(submitted_by=None, rejection_reason=None)

            version_requests = [r for r in requests[quote_id] if r["quote_version"] == number]
            hs = [h for h in histories[quote_id] if h["quote_version"] == number]
            _require(
                len(version_requests) <= 1, "multiple requests for one version", "approval_requests"
            )
            if not version_requests:
                _require(
                    not hs and version["approval_valid"] is False,
                    "approval without its request",
                    "approval_history",
                )
                continue
            request = version_requests[0]
            request_id = request["approval_request_id"]
            known_requests.add(request_id)
            _require(
                request["required_role"] == approval.APPROVER_ROLE
                and request["approval_rule_version"] == version["approval_rule_version"]
                and request["status"] in {"pending", "approved", "rejected"},
                "unsupported approval request",
                "approval_requests",
            )
            actions = ["submitted"]
            if request["status"] != "pending":
                actions.append(request["status"])
            if any(h["action"] == "released" for h in hs):
                actions.append("released")
            _require(
                len(hs) == len(actions),
                "missing or duplicate lifecycle history",
                "approval_history",
            )
            for action in actions:
                history = _one(
                    [h for h in hs if h["action"] == action],
                    "missing or duplicate action history",
                    "approval_history",
                )
                at, actor = history["action_at"], history["actor_id"]
                _require(
                    history["approval_request_id"] == request_id,
                    "wrong request association",
                    "approval_history",
                )
                known_histories.add(history["approval_id"])
                operation = _ACTIONS[action]
                human(actor, customer, operation, at)
                event = _one(
                    events[(quote_id, number, approval.AUDIT[operation])],
                    "missing or duplicate lifecycle audit",
                    "audit_events",
                )
                receipt = _one(
                    receipts[(quote_id, number, operation)],
                    "missing or duplicate lifecycle receipt",
                    "idempotency_records",
                )
                _bound_step(event, receipt, quote, version, customer, actor, at)
                data, extra = event["event_data"], {}
                classification = data.get("classification")
                _require(
                    _SHA.fullmatch(str(data.get("assessment_fingerprint", ""))) is not None,
                    "missing assessment fingerprint",
                    "audit_events",
                )
                if action == "submitted":
                    _require(
                        state in policy.ASSESSABLE
                        and actor == request["requested_by"]
                        and at == request["requested_at"]
                        and history["reason"] == f"classification={classification}",
                        "invalid submission history",
                        "approval_history",
                    )
                    state = "pending_approval"
                    expected["submitted_by"] = actor
                elif action in {"approved", "rejected"}:
                    _require(
                        state == "pending_approval"
                        and request["resolved_by"] == actor
                        and request["resolved_at"] == at
                        and actor
                        not in {
                            quote["created_by"],
                            version["created_by"],
                            request["requested_by"],
                        },
                        "invalid decision or separation of duties",
                        "approval_history",
                    )
                    state = action
                    if action == "approved":
                        attested = {
                            "rfq_source_sha256": version["rfq_source_sha256"],
                            "requested_delivery_date": None
                            if version["requested_delivery_date"] is None
                            else version["requested_delivery_date"].isoformat(),
                            "no_contradictions": True,
                        }
                        _require(
                            fingerprint(data.get("attestation")) == fingerprint(attested)
                            and history["reason"] == approval.ATTESTED + fingerprint(attested),
                            "missing or inconsistent human attestation",
                            "approval_history",
                        )
                        extra = {"attestation": attested}
                        expected.update(
                            approved_version=number, approver_id=actor, approval_timestamp=at
                        )
                    else:
                        try:
                            approval._valid_reason(history["reason"])
                        except drafts.DraftRefused:
                            _require(False, "invalid rejection reason", "approval_history")
                        extra = {
                            "reason_sha256": hashlib.sha256(history["reason"].encode()).hexdigest()
                        }
                        _require(
                            data.get("reason_sha256") == extra["reason_sha256"],
                            "rejection reason digest mismatch",
                            "audit_events",
                        )
                        expected["rejection_reason"] = history["reason"]
                else:
                    _require(
                        state == "approved"
                        and actor != expected["approver_id"]
                        and data.get("approver_id") == expected["approver_id"]
                        and data.get("approval_rule_version") == request["approval_rule_version"]
                        and history["reason"] == "released",
                        "invalid release or separation of duties",
                        "approval_history",
                    )
                    state = "released"
                    expected.update(released_by=actor, released_at=at)
                row_version += 1
                expected_decision = {
                    "quote_id": quote_id,
                    "rfq_id": quote["rfq_id"],
                    "customer_id": customer,
                    "version": number,
                    "action": action,
                    "status": state,
                    "approval_request_id": request_id,
                    "quote_row_version": row_version,
                    "actor_id": actor,
                    "assessment_fingerprint": data["assessment_fingerprint"],
                }
                _require(
                    fingerprint(receipt["result_json"]) == fingerprint(expected_decision)
                    and data.get("approval_request_id") == request_id
                    and receipt["request_sha256"]
                    == approval.decision_digest(
                        Principal(customer, user_id=actor), quote_id, number, operation, extra
                    ),
                    "decision receipt or digest mismatch",
                    "idempotency_records",
                )
                consumed_events.add(event["event_id"])
                consumed_receipts.add(receipt["receipt_id"])
                # Transaction timestamps may overlap or run backwards across sessions
                # waiting for the quote lock. Receipt row versions establish causal
                # ordering; each step's history/event/receipt timestamps must agree.
            _require(
                version["approval_valid"] is (request["status"] == "approved"),
                "approval flag differs from decision",
                "quote_versions",
            )
            if request["status"] == "pending":
                _require(
                    request["resolved_by"] is None and request["resolved_at"] is None,
                    "pending request is resolved",
                    "approval_requests",
                )
        _require(
            known_requests == {r["approval_request_id"] for r in requests[quote_id]}
            and known_histories == {h["approval_id"] for h in histories[quote_id]},
            "unexplained approval rows",
            "approval_history",
        )
        _require(
            (quote["status"], quote["current_version"], quote["row_version"])
            == (state, len(vs), row_version)
            and all(quote[key] == value for key, value in expected.items()),
            "unexplained lifecycle quotation mutation",
        )
        initial_quotes.append(
            {
                **quote,
                **dict.fromkeys(_RESET),
                "status": "draft",
                "current_version": 1,
                "row_version": 1,
            }
        )
        initial_versions.append({**vs[0], "is_current": True, "approval_valid": False})
        initial_calculations.add(vs[0]["calculation_record_id"])

    expected_events = {
        e["event_id"]
        for group in events.values()
        for e in group
        if e["operation"] != drafts.AUDIT_OPERATION
    }
    _require(consumed_events == expected_events, "unexplained lifecycle audit", "audit_events")
    _require(
        consumed_receipts == {r["receipt_id"] for group in receipts.values() for r in group},
        "unexplained lifecycle receipt",
        "idempotency_records",
    )
    referenced = {v["calculation_record_id"] for group in versions.values() for v in group}
    _require(
        set(calculations) == referenced, "calculation without a version", "pricing_calculations"
    )
    for record_id, record in calculations.items():
        first = min(
            (
                v
                for group in versions.values()
                for v in group
                if v["calculation_record_id"] == record_id
            ),
            key=lambda v: v["version"],
        )
        _require(
            (record["rfq_id"], record["created_by"], record["created_at"])
            == (first["rfq_id"], first["created_by"], first["created_at"]),
            "calculation origin differs from its first version",
            "pricing_calculations",
        )
        _require(
            record["source_snapshot_versions"] == first["source_snapshot_versions"],
            "calculation snapshot origin differs from first version",
            "pricing_calculations",
        )
    explained.update(consumed_events)
    return {
        **extras,
        "quotes": initial_quotes,
        "quote_versions": initial_versions,
        "quote_line_items": [
            line for line in extras["quote_line_items"] if line["quote_version"] == 1
        ],
        "pricing_calculations": [
            c for key, c in calculations.items() if key in initial_calculations
        ],
    }


def _bound_step(event, receipt, quote, version, customer, actor, at):
    _require(
        (event["actor_id"], event["customer_id"], event["rfq_id"], event["occurred_at"])
        == (actor, customer, quote["rfq_id"], at)
        and (event["quote_id"], event["quote_version"]) == (quote["quote_id"], version["version"])
        and event["agent_name"] is None
        and isinstance(event["correlation_id"], str)
        and bool(event["correlation_id"].strip()),
        "forged lifecycle audit actor or time",
        "audit_events",
    )
    _require(
        (
            receipt["actor_id"],
            receipt["customer_id"],
            receipt["committed_at"],
            receipt["correlation_id"],
        )
        == (actor, customer, at, event["correlation_id"])
        and valid_key(receipt["idempotency_key"])
        and _SHA.fullmatch(receipt["request_sha256"]) is not None,
        "forged lifecycle receipt",
        "idempotency_records",
    )


async def verify_evidence(session, extras):
    """Recompute immutable version evidence using existing policy checks, without locks.

    The integrity audit already owns a read-only repeatable snapshot. No assessment
    function that uses FOR SHARE is called here; no external service is invoked.
    """
    from rfq_copilot.mcp.runtime import pinned_rules
    from rfq_copilot.repositories import get_rfq
    from rfq_copilot.services.evaluation import evaluate_stored_rfq

    quotes = {q["quote_id"]: q for q in extras["quotes"]}
    rfqs = {r["rfq_id"]: r for r in extras["rfqs"]}
    records = {
        r["calculation_record_id"]: SimpleNamespace(**r) for r in extras["pricing_calculations"]
    }
    rules = await drafts.pinned_evaluation_rules(session) if quotes else None
    approvers = (
        tuple(
            r
            for r in await policy._granted_roles(session, policy.APPROVE_PERMISSION)
            if r == policy.APPROVER_ROLE
        )
        if quotes
        else ()
    )
    engineers = (
        await policy._granted_roles(session, policy.ENGINEERING_PERMISSION) if quotes else ()
    )
    for row in extras["quote_versions"]:
        version, quote = SimpleNamespace(**row), quotes[row["quote_id"]]
        customer = rfqs[quote["rfq_id"]]["customer_id"]
        event = _one(
            [
                e
                for e in extras["audit_events"]
                if e["quote_id"] == version.quote_id
                and e["quote_version"] == version.version
                and e["operation"] in revisions.CREATION_OPERATIONS
            ],
            "missing version provenance",
            "audit_events",
        )["event_data"]
        pins = tuple(
            row[k]
            for k in (
                "manufacturing_rule_version",
                "pricing_rule_version",
                "approval_rule_version",
                "workflow_rule_version",
            )
        )
        workflow = await pinned_rules(session, version.workflow_rule_version)
        try:
            context = policy._recorded_context(event, version, pins)
        except drafts.DraftRefused:
            _require(False, "invalid recorded version context", "audit_events")
        details = await get_rfq(session, quote["rfq_id"], customer_id=customer)
        evaluation = await evaluate_stored_rfq(
            session, quote["rfq_id"], customer_id=customer, **rules, **context, contradictions=()
        )
        _require(evaluation is not None, "missing version evaluation", "quote_versions")
        lines = sorted(
            [
                line
                for line in extras["quote_line_items"]
                if (line["quote_id"], line["quote_version"]) == (version.quote_id, version.version)
            ],
            key=lambda line: line["line_number"],
        )
        values = [
            tuple(
                line[k]
                for k in (
                    "line_number",
                    "part_id",
                    "quantity",
                    "line_total_usd",
                    "part_json",
                    "cost_breakdown_json",
                )
            )
            for line in lines
        ]
        findings = policy.integrity_findings(
            evaluation, event, version, values, rules["pricing_rules"]
        )
        dates = evidence.validity_dates(workflow)
        findings += policy.evidence_findings(
            quote_rfq_id=quote["rfq_id"],
            version=version,
            record=records.get(version.calculation_record_id),
            event=event,
            context=context,
            evaluation=evaluation,
            rfq_source=evidence.rfq_source_sha256(quote["rfq_id"], customer, details.runtime_lines),
            rules_digest=evidence.rules_sha256(rules, workflow),
            expected_expiry=None
            if dates is None
            else evidence.expiry_date(version.created_at, dates),
            pricing_rules=rules["pricing_rules"],
        )
        _require(
            not findings and version.snapshot_completeness == evidence.COMPLETE,
            "version evidence or commercial content changed",
            "quote_versions",
        )
        record = records[version.calculation_record_id]
        _require(
            record.parts == [line["part_json"] for line in lines],
            "calculation parts differ from version",
            "pricing_calculations",
        )
        requirements = policy.policy_requirements(evaluation, approvers, engineers)
        classification = policy.classify(requirements, findings)
        if version.version > 1:
            receipt = _one(
                [
                    r
                    for r in extras["idempotency_records"]
                    if r["operation"] == revisions.OPERATION
                    and r["result_json"].get("quote_id") == version.quote_id
                    and r["result_json"].get("version") == version.version
                ],
                "missing revision receipt",
                "idempotency_records",
            )
            quotation, pricing = evaluation.quotation, evaluation.quotation.pricing
            result = revisions.QuoteRevision(
                quote_id=version.quote_id,
                rfq_id=quote["rfq_id"],
                customer_id=customer,
                version=version.version,
                status=revisions.REVISED,
                currency=pricing.currency,
                total_usd=quotation.quote_total_usd,
                calculation_id=pricing.calculation_id,
                evaluation_fingerprint=evaluation.evaluation_fingerprint,
                outcome=evaluation.outcome,
                manager_review_required=evaluation.manager_review_required,
                manager_review_reasons=evaluation.manager_review_reasons,
                rule_versions=pins,
                expiry_date=version.expiry_date,
                previous_version=version.version - 1,
            )
            _require(
                fingerprint(receipt["result_json"]) == fingerprint(result.to_json()),
                "forged revision result",
                "idempotency_records",
            )
            _require(
                type(event.get("assignment_enforced")) is bool,
                "missing revision assignment context",
                "audit_events",
            )
            principal = Principal(
                customer,
                user_id=version.created_by,
                assignment_required=event["assignment_enforced"],
            )
            inputs = {**context, "contradictions": (), "expected_evaluation_fingerprint": None}
            _require(
                fingerprint(event)
                == fingerprint(
                    {
                        **drafts.event_data(result, principal, inputs),
                        "previous_version": version.version - 1,
                    }
                ),
                "forged revision event payload",
                "audit_events",
            )
            # The service records resolved context, while the original digest can name
            # either the selected pin/date/fingerprint or its documented None default.
            candidates = (
                revisions.revision_digest(
                    principal,
                    version.quote_id,
                    version.version - 1,
                    {
                        **inputs,
                        "horizon_start": horizon,
                        "inventory_snapshot": inventory,
                        "capacity_snapshot": capacity,
                        "expected_evaluation_fingerprint": expected,
                    },
                )
                for horizon, inventory, capacity, expected in product(
                    (None, context["horizon_start"]),
                    (None, context["inventory_snapshot"]),
                    (None, context["capacity_snapshot"]),
                    (None, evaluation.evaluation_fingerprint),
                )
            )
            _require(
                receipt["request_sha256"] in candidates,
                "forged revision request digest",
                "idempotency_records",
            )
        for step in extras["audit_events"]:
            if (step["quote_id"], step["quote_version"]) != (version.quote_id, version.version):
                continue
            if step["operation"] not in approval.AUDIT.values():
                continue
            operation = next(op for op, name in approval.AUDIT.items() if name == step["operation"])
            _require(
                step["event_data"].get("classification") == classification,
                "forged lifecycle classification",
                "audit_events",
            )
            expired = evidence.is_expired(version.expiry_date, step["occurred_at"])
            _require(not expired, "lifecycle action on expired quotation", "approval_history")
            if operation != approval.REJECT:
                _require(
                    classification in policy.REVIEWABLE,
                    "policy-blocked lifecycle action",
                    "approval_history",
                )
            if operation in {approval.APPROVE, approval.RELEASE}:
                _require(
                    set(policy.binding_blockers(version, findings, expired))
                    == {approval.ATTESTATION_BLOCKER}
                    and not any(r.kind == "engineering_review" for r in requirements.reviews),
                    "unverified evidence or engineering review",
                    "approval_history",
                )
            receipt = _one(
                [
                    r
                    for r in extras["idempotency_records"]
                    if r["operation"] == operation
                    and r["result_json"].get("quote_id") == version.quote_id
                    and r["result_json"].get("version") == version.version
                ],
                "missing assessment receipt",
                "idempotency_records",
            )
            before = {
                approval.SUBMIT: "draft" if version.version == 1 else "revised_draft",
                approval.APPROVE: "pending_approval",
                approval.REJECT: "pending_approval",
                approval.RELEASE: "approved",
            }[operation]
            fields = dict(
                quote_id=version.quote_id,
                rfq_id=quote["rfq_id"],
                customer_id=customer,
                assessed_version=version.version,
                quote_row_version=receipt["result_json"]["quote_row_version"] - 1,
                quote_status=before,
                rule_versions=pins,
                approval_rule_fingerprint=evaluation.approval_rule_fingerprint,
                evaluation_fingerprint=evaluation.evaluation_fingerprint,
                recorded_evaluation_fingerprint=policy.recorded_fingerprint(event, version),
                calculation_id=evaluation.quotation.pricing.calculation_id,
                policy=requirements,
                integrity_findings=findings,
                classification=classification,
                human_review="eligible"
                if classification in policy.REVIEWABLE and not expired
                else "not_eligible",
                binding_approval_blockers=policy.binding_blockers(version, findings, expired),
            )
            data = step["event_data"]
            _require(
                data["assessment_fingerprint"]
                == fingerprint(
                    {
                        "assessment_version": policy.ASSESSMENT_VERSION,
                        **fields,
                    }
                ),
                "forged lifecycle assessment fingerprint",
                "audit_events",
            )
            common = {"approval_request_id", "assessment_fingerprint", "classification"}
            additions = {
                approval.SUBMIT: {"reviews"},
                approval.APPROVE: {"attestation", "manager_review_reasons"},
                approval.REJECT: {"reason_sha256"},
                approval.RELEASE: {"approver_id", "approval_rule_version"},
            }
            _require(
                set(data) == common | additions[operation],
                "unsupported lifecycle event payload",
                "audit_events",
            )
            if operation == approval.SUBMIT:
                _require(
                    data["reviews"] == [r.kind for r in requirements.reviews],
                    "forged submission reviews",
                    "audit_events",
                )
            if operation == approval.APPROVE:
                _require(
                    data["manager_review_reasons"]
                    == [
                        reason
                        for r in requirements.reviews
                        if r.kind == "manager_review"
                        for reason in r.reasons
                    ],
                    "forged manager review",
                    "audit_events",
                )
