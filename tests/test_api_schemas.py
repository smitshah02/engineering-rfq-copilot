"""Day 17 API contract tests. Public smoke suite: no PostgreSQL, credentials or dataset.

Payloads are small synthetic examples, not dataset records.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from rfq_copilot.api.app import create_app
from rfq_copilot.api.config import ApiSettings
from rfq_copilot.api.errors import REDACTED, VALIDATION_ERROR_RESPONSES, to_issue
from rfq_copilot.api.schemas.common import PLAIN_DECIMAL
from rfq_copilot.api.schemas.rfq import (
    MAX_PARTS,
    PartSpecification,
    QuoteCalculationSummary,
    RfqReceipt,
    RfqSubmissionRequest,
)
from rfq_copilot.entities import Part

D = Decimal
DEV = ApiSettings(environment="development")
SECRET = "sk_live_9f8e7d6c5b4a"
FULL_PART = {
    "part_id": "TEST-PANEL-1",
    "part_type": "l_bracket",
    "material_id": "MAT-TEST",
    "finish_id": "none",
    "length_mm": 400,
    "width_mm": "250.5",
    "height_mm": 80,
    "thickness_mm": "2.0",
    "tolerance_mm": "0.5",
    "bend_radius_mm": "3.00",
    "weld_length_mm": 0,
    "quantity": 25,
    "hole_count": 0,
}


def submission(**overrides):
    return {"parts": [dict(FULL_PART)], **overrides}


def part(**overrides):
    return {**FULL_PART, **overrides}


def errors(model, payload):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return [(".".join(map(str, e["loc"])), e["type"]) for e in caught.value.errors()]


# --- A. Request contracts ----------------------------------------------------------------


def test_minimal_request_is_only_a_part_id():
    # Incomplete parts are structurally valid: the manufacturing validator turns missing
    # fields into a clarification finding, so the API must not reject them.
    request = RfqSubmissionRequest.model_validate({"parts": [{"part_id": "P-1"}]})
    assert request.parts[0].part_id == "P-1"
    assert request.parts[0].quantity is None and request.requested_delivery_date is None


def test_full_request_keeps_every_value_exactly():
    request = RfqSubmissionRequest.model_validate(
        submission(requested_delivery_date="2026-12-15", raw_request="Two brackets, see notes.")
    )
    spec = request.parts[0]
    assert spec.width_mm == D("250.5") and spec.length_mm == D(400)
    assert str(spec.thickness_mm) == "2.0" and str(spec.bend_radius_mm) == "3.00"
    assert (spec.quantity, spec.hole_count, spec.weld_length_mm) == (25, 0, D(0))
    assert request.requested_delivery_date == date(2026, 12, 15)
    assert request.raw_request == "Two brackets, see notes."


def test_valid_request_is_accepted_unchanged_by_the_service_part_contract():
    # Contract parity: what the API accepts, entities.Part.from_mapping accepts with
    # identical values, so the future endpoint needs no reinterpretation.
    spec = RfqSubmissionRequest.model_validate(submission()).parts[0]
    domain = Part.from_mapping(spec.model_dump(mode="json", exclude_none=True))
    for name, value in spec.model_dump(exclude_none=True).items():
        assert getattr(domain, name) == value


def test_null_and_missing_both_mean_not_stated():
    explicit = PartSpecification.model_validate({"part_id": "P-1", "material_id": None})
    assert explicit == PartSpecification.model_validate({"part_id": "P-1"})


def test_identifiers_are_kept_verbatim():
    spec = PartSpecification.model_validate({"part_id": " Mixed-Case_01 ", "material_id": "mat-x"})
    assert (spec.part_id, spec.material_id) == (" Mixed-Case_01 ", "mat-x")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, [("parts", "missing")]),
        ({"parts": []}, [("parts", "too_short")]),
        ({"parts": {"part_id": "P"}}, [("parts", "list_type")]),
        ({"parts": [{}]}, [("parts.0.part_id", "missing")]),
        ({"parts": [{"part_id": None}]}, [("parts.0.part_id", "string_type")]),
        ({"parts": [{"part_id": "   "}]}, [("parts.0.part_id", "blank_or_invalid_text")]),
        ({"parts": [{"part_id": "a\x00b"}]}, [("parts.0.part_id", "blank_or_invalid_text")]),
        ({"parts": [{"part_id": ""}]}, [("parts.0.part_id", "blank_or_invalid_text")]),
        ({"parts": [{"part_id": "x" * 65}]}, [("parts.0.part_id", "string_too_long")]),
        ({"parts": [{"part_id": 17}]}, [("parts.0.part_id", "string_type")]),
        ({"parts": [part(part_type="gearbox")]}, [("parts.0.part_type", "literal_error")]),
        ({"parts": [part(part_type="L_BRACKET")]}, [("parts.0.part_type", "literal_error")]),
        ({"parts": [part(material_id="")]}, [("parts.0.material_id", "blank_or_invalid_text")]),
        ({"parts": [part(colour="red")]}, [("parts.0.colour", "extra_forbidden")]),
        (
            {"parts": [part(requested_operations=["laser"])]},
            [("parts.0.requested_operations", "extra_forbidden")],
        ),
        (submission(customer_id="CUST-1"), [("customer_id", "extra_forbidden")]),
        (
            submission(pricing_rule_version="pricing-v9"),
            [("pricing_rule_version", "extra_forbidden")],
        ),
        (submission(approved=True), [("approved", "extra_forbidden")]),
        (submission(raw_request="  "), [("raw_request", "blank_or_invalid_text")]),
        (submission(raw_request="x" * 20_001), [("raw_request", "string_too_long")]),
        (
            submission(requested_delivery_date="15/12/2026"),
            [("requested_delivery_date", "date_format")],
        ),
        (
            submission(requested_delivery_date="2026-02-30"),
            [("requested_delivery_date", "date_format")],
        ),
        (
            submission(requested_delivery_date="2026-12-15T00:00:00"),
            [("requested_delivery_date", "date_format")],
        ),
        (
            submission(requested_delivery_date=1797292800),
            [("requested_delivery_date", "date_format")],
        ),
    ],
)
def test_structurally_invalid_requests(payload, expected):
    assert errors(RfqSubmissionRequest, payload) == expected


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("quantity", 0, "greater_than_equal"),
        ("quantity", -3, "greater_than_equal"),
        ("quantity", True, "int_type"),
        ("quantity", "25", "int_type"),
        ("quantity", 25.0, "int_type"),
        ("quantity", 2**31, "less_than_equal"),
        ("hole_count", -1, "greater_than_equal"),
        ("hole_count", False, "int_type"),
        ("length_mm", 0, "greater_than"),
        ("thickness_mm", "0.000", "greater_than"),
        ("tolerance_mm", "0", None),  # zero allowed where the contract minimum is 0
        ("weld_length_mm", 0, None),
        ("width_mm", -5, "greater_than"),
        ("bend_radius_mm", True, "exact_decimal_type"),
    ],
)
def test_count_and_measurement_bounds(field, value, code):
    payload = {"parts": [part(**{field: value})]}
    if code is None:
        RfqSubmissionRequest.model_validate(payload)
    else:
        assert errors(RfqSubmissionRequest, payload) == [(f"parts.0.{field}", code)]


def test_multiple_nested_errors_are_all_reported():
    payload = {"parts": [part(quantity=0), part(part_id="", length_mm=1.5)], "extra": 1}
    assert errors(RfqSubmissionRequest, payload) == [
        ("parts.0.quantity", "greater_than_equal"),
        ("parts.1.part_id", "blank_or_invalid_text"),
        ("parts.1.length_mm", "exact_decimal_type"),
        ("extra", "extra_forbidden"),
    ]


def test_part_ids_must_be_unique_within_an_rfq():
    payload = {"parts": [part(), part(part_id="OTHER"), part()]}
    with pytest.raises(ValidationError) as caught:
        RfqSubmissionRequest.model_validate(payload)
    (error,) = caught.value.errors()
    assert (error["loc"], error["type"]) == (("parts",), "duplicate_part_id")
    assert "parts[2]" in error["msg"] and "TEST-PANEL-1" not in error["msg"]


def test_part_count_limit():
    parts = [{"part_id": f"P-{i}"} for i in range(MAX_PARTS + 1)]
    assert errors(RfqSubmissionRequest, {"parts": parts}) == [("parts", "too_long")]
    RfqSubmissionRequest.model_validate({"parts": parts[:MAX_PARTS]})


def test_request_models_are_immutable():
    request = RfqSubmissionRequest.model_validate(submission())
    with pytest.raises(ValidationError):
        request.parts[0].quantity = 1


# --- Decimal and JSON safety -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "exact"),
    [
        (520, "520"),
        ("0.001", "0.001"),  # smallest step at scale 3
        ("999999999.999", "999999999.999"),  # largest Numeric(12, 3) value
        ("4.0", "4.0"),
        ("1.5000", "1.5000"),  # trailing zeros are within scale; kept, not rounded
        (D("2.25"), "2.25"),
    ],
)
def test_exact_measurements_survive_a_json_round_trip(value, exact):
    spec = PartSpecification.model_validate({"part_id": "P", "thickness_mm": value})
    wire = spec.model_dump_json()
    assert json.loads(wire)["thickness_mm"] == exact
    assert PartSpecification.model_validate_json(wire) == spec


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (1.5, "exact_decimal_type"),  # JSON float
        (2.0, "exact_decimal_type"),
        ("NaN", "exact_decimal_type"),
        ("Infinity", "exact_decimal_type"),
        (D("NaN"), "exact_decimal_type"),
        (D("-Infinity"), "exact_decimal_type"),
        ("1e3", "exact_decimal_type"),
        (D("1E+2"), "exact_decimal_type"),
        ("-1", "exact_decimal_type"),
        (" 1", "exact_decimal_type"),
        ("1.", "exact_decimal_type"),
        (".5", "exact_decimal_type"),
        ("", "exact_decimal_type"),
        ("four", "exact_decimal_type"),
        ([4], "exact_decimal_type"),
        ("0.0001", "decimal_max_places"),  # would need rounding: refused, not rounded
        ("1000000000", "decimal_whole_digits"),
    ],
)
def test_inexact_or_out_of_precision_measurements_are_rejected(value, code):
    assert errors(PartSpecification, {"part_id": "P", "thickness_mm": value}) == [
        ("thickness_mm", code)
    ]


def test_precision_limits_match_the_service_part_contract():
    for accepted in ("0.001", "999999999.999", "1.5000"):
        Part.from_mapping({"part_id": "P", "length_mm": accepted})
        PartSpecification.model_validate({"part_id": "P", "length_mm": accepted})
    for refused in ("0.0001", "1000000000"):
        with pytest.raises(ValueError):
            Part.from_mapping({"part_id": "P", "length_mm": refused})
        with pytest.raises(ValidationError):
            PartSpecification.model_validate({"part_id": "P", "length_mm": refused})


# --- B. Response contracts ---------------------------------------------------------------

CALCULATION = {
    "calculation_id": "CALC-TEST01",
    "pricing_rule_version": "pricing-test",
    "currency": "USD",
    "quote_total_usd": "1234.50",
    "lines": [
        {"part_id": "A", "line_total_usd": "1000.10"},
        {"part_id": "B", "line_total_usd": "234.40"},
    ],
}


def test_receipt_contract():
    receipt = RfqReceipt(rfq_id="RFQ-TEST", status="draft")
    assert json.loads(receipt.model_dump_json()) == {"rfq_id": "RFQ-TEST", "status": "draft"}
    assert errors(RfqReceipt, {"rfq_id": "RFQ-TEST", "status": "approved"}) == [
        ("status", "literal_error")
    ]
    assert errors(RfqReceipt, {"rfq_id": "R", "status": "draft", "customer_id": "C"}) == [
        ("customer_id", "extra_forbidden")
    ]


def test_quote_calculation_serializes_money_as_exact_strings():
    summary = QuoteCalculationSummary.model_validate(CALCULATION)
    assert json.loads(summary.model_dump_json()) == CALCULATION
    assert summary.quote_total_usd == D("1234.50")
    assert QuoteCalculationSummary.model_validate_json(summary.model_dump_json()) == summary


def test_money_keeps_the_scale_it_was_given_and_large_values_exactly():
    big = "123456789012345678901234.5678"
    summary = QuoteCalculationSummary.model_validate(
        {
            **CALCULATION,
            "quote_total_usd": big,
            "lines": [{"part_id": "A", "line_total_usd": D("0.10")}],
        }
    )
    wire = json.loads(summary.model_dump_json())
    assert wire["quote_total_usd"] == big and wire["lines"][0]["line_total_usd"] == "0.10"


@pytest.mark.parametrize("value", [1234.5, "-1.00", "NaN", D("Infinity"), "1e3", True])
def test_money_rejects_inexact_negative_and_non_finite_values(value):
    assert errors(QuoteCalculationSummary, {**CALCULATION, "quote_total_usd": value}) != []


@pytest.mark.parametrize(
    ("change", "location"),
    [
        ({"currency": "usd"}, "currency"),
        ({"lines": []}, "lines"),
        ({"approved": True}, "approved"),
        ({"calculation_id": ""}, "calculation_id"),
    ],
)
def test_quote_calculation_contract_violations(change, location):
    assert [loc for loc, _ in errors(QuoteCalculationSummary, {**CALCULATION, **change})] == [
        location
    ]


def test_schemas_declare_exact_wire_formats():
    part_schema = RfqSubmissionRequest.model_json_schema()["$defs"]["PartSpecification"]
    assert part_schema["additionalProperties"] is False
    assert part_schema["required"] == ["part_id"]
    assert part_schema["properties"]["length_mm"]["anyOf"] == [
        {"type": "integer", "exclusiveMinimum": 0},
        {"type": "string", "pattern": PLAIN_DECIMAL},
        {"type": "null"},
    ]
    assert part_schema["properties"]["quantity"]["anyOf"][0] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 2**31 - 1,
    }
    assert set(part_schema["properties"]["part_type"]["anyOf"][0]["enum"]) == {
        "flat_panel",
        "mounting_plate",
        "l_bracket",
        "u_bracket",
        "enclosure",
        "fabricated_assembly",
    }
    money = QuoteCalculationSummary.model_json_schema(mode="serialization")
    assert money["properties"]["quote_total_usd"] == {
        "type": "string",
        "pattern": PLAIN_DECIMAL,
        "title": "Quote Total Usd",
    }
    assert "number" not in json.dumps(RfqSubmissionRequest.model_json_schema())  # no floats


# --- C/D. HTTP validation through a test-only router -------------------------------------


class Accepted(BaseModel):
    """Test-only response: echoes the parsed request, proving the HTTP round trip."""

    request: RfqSubmissionRequest


def harness():
    calls: list[RfqSubmissionRequest] = []
    router = APIRouter(prefix="/test-only")

    @router.post("/rfq", responses=VALIDATION_ERROR_RESPONSES)
    async def parse(body: RfqSubmissionRequest) -> Accepted:
        calls.append(body)
        return Accepted(request=body)

    app = create_app(DEV)
    app.include_router(router)
    return TestClient(app), calls


def test_valid_request_reaches_the_handler_and_round_trips_exactly():
    client, calls = harness()
    response = client.post("/test-only/rfq", json=submission(requested_delivery_date="2026-12-15"))
    assert response.status_code == 200
    echoed = response.json()["request"]
    assert echoed["parts"][0]["width_mm"] == "250.5" and echoed["parts"][0]["quantity"] == 25
    assert echoed["requested_delivery_date"] == "2026-12-15"
    assert len(calls) == 1 and calls[0].parts[0].thickness_mm == D("2.0")


def test_invalid_request_returns_the_structured_422_without_reaching_the_handler():
    client, calls = harness()
    payload = {"parts": [part(quantity=0), part(part_id="", length_mm=1.5)], "customer_id": "C"}
    response = client.post("/test-only/rfq", json=payload)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "code": "REQUEST_VALIDATION_ERROR",
        "message": "Request validation failed.",
        "details": [
            {
                "field": "body.parts.0.quantity",
                "code": "greater_than_equal",
                "message": "Value is below the allowed minimum.",
            },
            {
                "field": "body.parts.1.part_id",
                "code": "blank_or_invalid_text",
                "message": "Text must be nonblank, without NUL.",
            },
            {
                "field": "body.parts.1.length_mm",
                "code": "exact_decimal_type",
                "message": "Expected an integer or a plain decimal string such as '4.0'.",
            },
            {
                # Unknown keys are client-controlled, so never reflected, even customer_id.
                "field": f"body.{REDACTED}",
                "code": "extra_forbidden",
                "message": "Unknown field.",
            },
        ],
    }
    assert calls == []


@pytest.mark.parametrize(
    ("content", "content_type", "code"),
    [
        (b'{"parts": [', "application/json", "json_invalid"),
        (b"", "application/json", "missing"),
        (b"[1, 2]", "application/json", "model_attributes_type"),
        (b"parts=1", "application/x-www-form-urlencoded", "model_attributes_type"),
    ],
)
def test_malformed_or_non_json_bodies(content, content_type, code):
    client, calls = harness()
    response = client.post(
        "/test-only/rfq", content=content, headers={"content-type": content_type}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "REQUEST_VALIDATION_ERROR"
    assert [d["code"] for d in body["details"]] == [code]
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"parts": [{"part_id": SECRET + "x" * 64}]},  # too long: value not echoed
        {"parts": [{"part_id": "P", "quantity": SECRET}]},  # wrong type
        {"parts": [{"part_id": "P", "thickness_mm": SECRET}]},  # bad decimal
        {"parts": [{"part_id": "P", "part_type": SECRET}]},  # bad enum
        {"parts": [{"part_id": "P"}], "raw_request": SECRET + "\x00"},  # invalid text
        {"parts": [{"part_id": "P"}], f"token={SECRET}": "x"},  # client-chosen key
        {"parts": [{"part_id": "P"}], "requested_delivery_date": SECRET},
    ],
)
def test_validation_errors_never_echo_submitted_values(payload):
    client, calls = harness()
    response = client.post("/test-only/rfq", json=payload)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert set(response.json()) == {"code", "message", "details"}
    for detail in response.json()["details"]:
        assert set(detail) == {"field", "code", "message"}  # no input, ctx or url
    assert calls == []


def test_malformed_json_containing_a_secret_is_not_echoed():
    client, _ = harness()
    response = client.post(
        "/test-only/rfq",
        content=f'{{"parts": [{{"part_id": "{SECRET}" oops'.encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422 and SECRET not in response.text


def test_issue_builder_selects_safe_fields_only():
    raw = {
        "type": "value_error",
        "loc": ("body", "parts", 0, "Bearer abc.def", "x"),
        "msg": "Value error, bad",
        "input": SECRET,
        "ctx": {"error": ValueError(SECRET)},
        "url": "https://errors.pydantic.dev/",
    }
    issue = to_issue(raw)
    assert issue.model_dump() == {
        "field": f"body.parts.0.{REDACTED}.{REDACTED}",
        "code": "value_error",
        "message": "Invalid value.",
    }
    trusted = to_issue({"type": "missing", "loc": ("body", "parts", 3, "quantity")})
    assert trusted.field == "body.parts.3.quantity"
    assert to_issue({"type": "Bad Type!", "loc": (True,)}).model_dump() == {
        "field": REDACTED,
        "code": "invalid",
        "message": "Invalid value.",
    }


def test_error_contract_is_documented_in_openapi_for_routers_that_opt_in():
    client, _ = harness()
    schema = client.get("/openapi.json").json()
    unprocessable = schema["paths"]["/test-only/rfq"]["post"]["responses"]["422"]
    assert unprocessable["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ValidationErrorResponse"
    }
    components = schema["components"]["schemas"]
    assert "HTTPValidationError" not in components
    assert components["ValidationErrorResponse"]["required"] == ["code", "message", "details"]
    assert components["ValidationIssue"]["additionalProperties"] is False
    assert {"RfqSubmissionRequest", "PartSpecification"} <= set(components)


# --- Day 16 behaviour preserved ----------------------------------------------------------


def test_production_application_registers_no_rfq_route_or_schema():
    for settings in (DEV, ApiSettings(environment="production")):
        schema = create_app(settings).openapi()
        # Day 18 added protected GET routes; still no RFQ submission route or request body.
        # Day 19's attachment upload is the only other operation.
        upload = "/api/v1/rfqs/{rfq_id}/attachments"
        assert all(
            set(ops) == ({"get", "post"} if path == upload else {"get"})
            for path, ops in schema["paths"].items()
        )
        assert not any("test-only" in path for path in schema["paths"])
        assert "RfqSubmissionRequest" not in schema["components"]["schemas"]
    with TestClient(create_app(DEV)) as client:
        response = client.post("/test-only/rfq", json=submission())
        assert response.status_code == 404 and response.json() == {"detail": "Not Found"}


def test_existing_404_and_405_semantics_are_unchanged():
    # The opaque 500 for response-model violations is covered by tests/test_api.py.
    client, _ = harness()
    assert client.get("/nope").json() == {"detail": "Not Found"}
    assert client.post("/health/live").status_code == 405
    assert client.get("/test-only/rfq").status_code == 405


# --- Security regressions (2026-11-10 audit) ---------------------------------------------
# Finding 1: unknown field names that look like identifiers were echoed in "field".
# Finding 2: raw Pydantic messages were echoed, including ValueError text with input.

TOKEN_KEY = "sk_live_9f8e7d6c5b4a"  # letters, digits and underscores only


class LeakyProbe(BaseModel):
    """Test-only model whose validator puts the input into its exception message."""

    model_config = {"extra": "forbid"}

    code: str

    @field_validator("code")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError(f"rejected secret value {value}")


def probe_harness():
    calls: list[LeakyProbe] = []
    router = APIRouter(prefix="/test-only")

    @router.post("/probe", responses=VALIDATION_ERROR_RESPONSES)
    async def probe(body: LeakyProbe) -> dict[str, str]:
        calls.append(body)
        return {"ok": "yes"}

    app = create_app(DEV)
    app.include_router(router)
    return TestClient(app), calls


def assert_no_leak(response, *secrets):
    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_ERROR"
    for secret in secrets:
        assert secret not in response.text


def test_token_shaped_unknown_top_level_key_is_redacted():
    client, calls = harness()
    response = client.post("/test-only/rfq", json={**submission(), TOKEN_KEY: 1})
    assert_no_leak(response, TOKEN_KEY)
    assert response.json()["details"] == [
        {"field": f"body.{REDACTED}", "code": "extra_forbidden", "message": "Unknown field."}
    ]
    assert calls == []


def test_nested_unknown_key_with_sensitive_text_is_redacted():
    client, calls = harness()
    key = "password_Hunter2_db_admin"
    response = client.post("/test-only/rfq", json={"parts": [part(**{key: "x"})]})
    assert_no_leak(response, key, "Hunter2")
    assert [d["field"] for d in response.json()["details"]] == [f"body.parts.0.{REDACTED}"]
    assert calls == []


def test_unknown_key_with_punctuation_is_redacted():
    client, _ = harness()
    key = "Authorization: Bearer eyJhbGciOi.abc"
    response = client.post("/test-only/rfq", json={**submission(), key: 1})
    assert_no_leak(response, "eyJhbGciOi", "Bearer")
    assert response.json()["details"][0]["field"] == f"body.{REDACTED}"


def test_custom_validator_message_with_input_is_not_echoed():
    client, calls = probe_harness()
    response = client.post("/test-only/probe", json={"code": SECRET})
    assert_no_leak(response, SECRET, "rejected secret value")
    # LeakyProbe is not a registered request model, so even its field name is redacted.
    assert response.json()["details"] == [
        {"field": f"body.{REDACTED}", "code": "value_error", "message": "Invalid value."}
    ]
    assert calls == []


def test_multiple_errors_stay_complete_and_safe():
    client, calls = harness()
    payload = {
        "parts": [part(quantity=SECRET, **{TOKEN_KEY: 1}), part(part_id=SECRET + "x" * 64)],
        TOKEN_KEY: SECRET,
    }
    response = client.post("/test-only/rfq", json=payload)
    assert_no_leak(response, SECRET, TOKEN_KEY)
    assert [(d["field"], d["code"]) for d in response.json()["details"]] == [
        ("body.parts.0.quantity", "int_type"),
        (f"body.parts.0.{REDACTED}", "extra_forbidden"),
        ("body.parts.1.part_id", "string_too_long"),
        (f"body.{REDACTED}", "extra_forbidden"),
    ]
    assert calls == []


def test_known_field_errors_stay_understandable():
    client, _ = harness()
    response = client.post(
        "/test-only/rfq",
        json={"parts": [part(quantity=0, thickness_mm=1.5, part_type="gearbox")]},
    )
    assert response.json()["details"] == [
        {
            "field": "body.parts.0.part_type",
            "code": "literal_error",
            "message": "Value is not one of the allowed options.",
        },
        {
            "field": "body.parts.0.thickness_mm",
            "code": "exact_decimal_type",
            "message": "Expected an integer or a plain decimal string such as '4.0'.",
        },
        {
            "field": "body.parts.0.quantity",
            "code": "greater_than_equal",
            "message": "Value is below the allowed minimum.",
        },
    ]


def test_duplicate_part_error_is_static_and_safe():
    client, calls = harness()
    duplicate = part(part_id=TOKEN_KEY)
    response = client.post("/test-only/rfq", json={"parts": [duplicate, duplicate]})
    assert_no_leak(response, TOKEN_KEY)
    assert response.json()["details"] == [
        {
            "field": "body.parts",
            "code": "duplicate_part_id",
            "message": "part_id values must be unique within an RFQ.",
        }
    ]
    assert calls == []


@pytest.mark.parametrize(
    "content",
    [
        f'{{"{TOKEN_KEY}": "{SECRET}"'.encode(),
        f'{{"parts": [{{"part_id": "{SECRET}"}}]}} trailing {TOKEN_KEY}'.encode(),
        f"{SECRET}".encode(),
    ],
)
def test_malformed_json_never_echoes_submitted_text(content):
    client, calls = harness()
    response = client.post(
        "/test-only/rfq", content=content, headers={"content-type": "application/json"}
    )
    assert_no_leak(response, SECRET, TOKEN_KEY)
    assert [(d["code"], d["message"]) for d in response.json()["details"]] == [
        ("json_invalid", "Request body is not valid JSON.")
    ]
    assert calls == []


def test_every_detail_uses_a_predefined_message():
    from rfq_copilot.api.errors import GENERIC_MESSAGE, MESSAGES

    client, _ = harness()
    payload = {"parts": [part(quantity=True, hole_count=-1, finish_id="", length_mm="1e3")]}
    for detail in client.post("/test-only/rfq", json=payload).json()["details"]:
        assert detail["message"] in {*MESSAGES.values(), GENERIC_MESSAGE}


def test_day16_endpoints_are_unchanged_by_the_handler():
    with TestClient(create_app(DEV)) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/api/v1/system/info").json()["api_version"] == "v1"
        assert client.get("/missing").json() == {"detail": "Not Found"}
        assert client.delete("/health/live").status_code == 405


# --- Security regressions, round 2 (2026-11-10 audit) ------------------------------------
# A: a custom error *type* built from input was copied into "code".
# B: an unknown key that happens to be a field name elsewhere was reflected.

LOWERCASE_SECRET = "secret_session_token_abcxyz"


class TypeLeakProbe(BaseModel):
    """Test-only model whose validator uses the submitted value as the error type."""

    model_config = {"extra": "forbid"}

    code: str

    @field_validator("code")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise PydanticCustomError(value, "static template")


def test_custom_error_type_from_input_never_becomes_the_public_code():
    router = APIRouter(prefix="/test-only")
    calls = []

    @router.post("/type-probe", responses=VALIDATION_ERROR_RESPONSES)
    async def type_probe(body: TypeLeakProbe) -> dict[str, str]:
        calls.append(body)
        return {"ok": "yes"}

    app = create_app(DEV)
    app.include_router(router)
    response = TestClient(app).post("/test-only/type-probe", json={"code": LOWERCASE_SECRET})
    assert_no_leak(response, LOWERCASE_SECRET, "static template")
    assert response.json()["details"] == [
        {"field": f"body.{REDACTED}", "code": "invalid", "message": "Invalid value."}
    ]
    assert calls == []


def test_only_allowlisted_codes_are_published():
    from rfq_copilot.api.errors import PUBLIC_CODES

    assert to_issue({"type": "value_error", "loc": ()}).code == "value_error"
    assert to_issue({"type": "missing", "loc": ()}).code == "missing"
    for unknown in ("secret_session_token", "assertion_error", "url_parsing", "x"):
        assert to_issue({"type": unknown, "loc": ()}).code == "invalid"
    assert "invalid" not in PUBLIC_CODES  # the fallback is not an echo of anything


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"parts": [part(raw_request="x")]}, f"body.parts.0.{REDACTED}"),  # root field, nested
        ({**submission(), "quantity": 5}, f"body.{REDACTED}"),  # part field, at root
        ({**submission(), "part_id": "P"}, f"body.{REDACTED}"),
        ({**submission(), TOKEN_KEY: 1}, f"body.{REDACTED}"),  # token-shaped
        ({"parts": [part(**{TOKEN_KEY: 1})]}, f"body.parts.0.{REDACTED}"),  # nested unknown
    ],
)
def test_unknown_keys_are_always_redacted_even_if_declared_elsewhere(payload, field):
    client, calls = harness()
    response = client.post("/test-only/rfq", json=payload)
    assert_no_leak(response, TOKEN_KEY)
    assert response.json()["details"] == [
        {"field": field, "code": "extra_forbidden", "message": "Unknown field."}
    ]
    assert calls == []


def test_declared_field_paths_stay_useful():
    client, _ = harness()
    response = client.post(
        "/test-only/rfq",
        json={"parts": [part(quantity=0)], "raw_request": " ", "requested_delivery_date": "x"},
    )
    assert [(d["field"], d["code"]) for d in response.json()["details"]] == [
        ("body.parts.0.quantity", "greater_than_equal"),
        ("body.requested_delivery_date", "date_format"),
        ("body.raw_request", "blank_or_invalid_text"),
    ]
