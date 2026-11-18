"""Milestone 2 Step 2: customer RFQ creation, public tests (no PostgreSQL, no dataset).

Database behaviour (role re-check, atomicity, idempotency, isolation, audit) is in
test_customer_rfq_create_database.py and the runtime-role race in test_runtime_role_database.py.
"""

import pytest
from test_auth import SUBJECT, api, bearer, call
from test_auth import identity as identity
from test_auth import trust as trust

from rfq_copilot.api.app import create_app
from rfq_copilot.api.auth import CUSTOMER_CREATE_RFQ, CUSTOMER_VIEW_RFQ, VIEW_ASSIGNED_RFQS
from rfq_copilot.api.routers.rfqs import CREATE_MESSAGES, create_rejection, create_router
from rfq_copilot.api.schemas.rfq import RfqSubmissionRequest
from rfq_copilot.database import RUNTIME_INSERTABLE, RUNTIME_UPDATABLE
from rfq_copilot.repositories.idempotency import valid_key
from rfq_copilot.rfq_submission import request_digest

BODY = {"parts": [{"part_id": "P-1", "quantity": 2}]}
KEY = {"Idempotency-Key": "k-1"}
STAFF_AND_DECISION = (
    VIEW_ASSIGNED_RFQS, "create_rfq", "create_quote_draft", "approve_quote", "release_quote",
    "attach_rfq_document", "view_quote", CUSTOMER_VIEW_RFQ,
)  # fmt: skip


def paths(settings):
    return {(m.upper(), p) for p, item in create_app(settings).openapi()["paths"].items() for m in item}  # fmt: skip


@pytest.mark.parametrize("policy", ["v1", "v2"])
def test_the_create_route_exists_only_under_v3(trust, policy):
    assert ("POST", "/api/v1/rfqs") not in paths(trust.settings(permission_policy=policy))
    assert ("POST", "/api/v1/rfqs") in paths(trust.settings(permission_policy="v3"))


def test_the_route_requires_customer_create_rfq_and_no_other_grant():
    (route,) = create_router.routes
    required = {getattr(d.call, "required_permission", None) for d in route.dependant.dependencies}
    assert CUSTOMER_CREATE_RFQ in required


def test_unauthenticated_is_401_before_anything(trust, identity):
    app = api(trust, permission_policy="v3")
    assert call(app, "POST", "/api/v1/rfqs", json=BODY, headers=KEY).status_code == 401


def test_staff_and_decision_grants_never_open_the_route(trust, identity):
    identity.bindings["U-1"] = "CUST-A"
    identity.grants["U-1"] = frozenset(STAFF_AND_DECISION)
    app = api(trust, permission_policy="v3")
    response = call(app, "POST", "/api/v1/rfqs", json=BODY, headers={**KEY, **bearer(trust.mint(sub=SUBJECT))})  # fmt: skip
    assert response.status_code == 403 and response.json() == {"detail": "Forbidden."}


def test_the_body_cannot_name_a_customer_creator_or_status():
    for forged in ({"customer_id": "CUST-B"}, {"created_by": "U-X"}, {"status": "validated"}):
        with pytest.raises(ValueError):
            RfqSubmissionRequest.model_validate({**BODY, **forged})


@pytest.mark.parametrize("key", ["", " k", "k ", "k\n", "é", "x" * 257])
def test_invalid_idempotency_keys(key):
    assert not valid_key(key)


def test_valid_idempotency_keys():
    assert valid_key("k") and valid_key("x" * 256) and valid_key("order-42:retry")


def test_the_digest_binds_customer_and_request():
    request = RfqSubmissionRequest.model_validate(BODY)
    same = RfqSubmissionRequest.model_validate({"parts": [{"quantity": 2, "part_id": "P-1"}]})
    assert request_digest("C-1", request) == request_digest("C-1", same)
    assert request_digest("C-1", request) != request_digest("C-2", request)
    other = RfqSubmissionRequest.model_validate({"parts": [{"part_id": "P-1", "quantity": 3}]})
    assert request_digest("C-1", request) != request_digest("C-1", other)


def test_rejections_are_static_text():
    for reason in CREATE_MESSAGES:
        response = create_rejection(reason, 3)
        assert response.status_code == (409 if reason == "idempotency_key_conflict" else 422)
        assert b'"code":"RFQ_REJECTED"' in response.body


def test_runtime_rfq_writes_are_insert_only():
    assert {"rfqs", "rfq_line_items"} <= RUNTIME_INSERTABLE
    assert not {c for c in RUNTIME_UPDATABLE if c.startswith(("rfqs.", "rfq_line_items."))}
