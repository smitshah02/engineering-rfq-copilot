"""RFQ Intake validation and clarification (Day 37): is the extracted RFQ complete,
consistent and ready to hand to manufacturing evaluation? Not feasibility, not price.

    IntakeResult (Day 36) + source text + pinned manufacturing rules [+ catalog executor]
        -> validate_intake -> IntakeValidation (findings, clarifications, readiness)

- Required fields come only from the pinned manufacturing rule document (``mandatory_fields``,
  ``height_required``, ``weld_length_required``), passed by the trusted caller as
  ``RuleVersionDetails`` (loaded with ``mcp.runtime.pinned_rules``). Without it completeness is
  ``rules_not_checked`` and nothing can be ready.
- Catalog: the read-only ``search_materials`` MCP tool through a trusted ``ToolExecutor``
  (``graph.mcp_executor``: delegated credential, authenticated gateway, RBAC). A material id
  is set only for one exact catalog row; a finish id only when the catalog lists that finish
  for the matched material. No executor -> ``not_checked``; a failure -> ``unavailable``.
- Contradictions are scanned in the part's own source segment across clauses and sentences.
  Corrections are reported with their evidence and never resolved automatically.
- Clarification questions are rendered from findings only (no model), one per part and field.
- ``ready_for_manufacturing_evaluation`` means complete and catalog-recognized, not feasible.
"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from rfq_copilot.api.schemas.reads import MAX_OFFSET, MAX_PAGE_SIZE, MaterialListQuery
from rfq_copilot.api.schemas.rfq import PartSpecification
from rfq_copilot.grounding import PART_KIND
from rfq_copilot.intake import (  # the Day 36 rules themselves: one implementation each
    _CORRECTION,
    _COUNT_TOKEN,
    _COUNTS,
    _FINISH_TERM,
    _GRADE,
    _GRADE_LEAD,
    _MATERIAL_TERM,
    _MEASURE,
    _MINUS,
    _NEGATION,
    _NOT_LATER,
    _OTHER_UNITS,
    _PART_NOUN,
    _QUANTITY_CUE,
    _UNITS,
    FieldEvidence,
    IntakePart,
    IntakeResult,
    _attribution_problem,
    _clause,
    _context_problem,
    _count,
    _length,
)
from rfq_copilot.mcp.client import CONTRACTS
from rfq_copilot.repositories.results import RuleVersionDetails
from rfq_copilot.services.materials import require_manufacturing_rules

ToolExecutor = Callable[[str, BaseModel], Awaitable[BaseModel]]  # as graph.ToolExecutor
CATALOG_TOOL = "search_materials"
MAX_CATALOG_CALLS = 20
DEFAULT_TIMEOUT = 30.0

Readiness = Literal[
    "needs_clarification", "requires_review", "ready_for_manufacturing_evaluation", "not_evaluated"
]  # fmt: skip
FindingKind = Literal[
    "missing",  # required by the pinned rules and not verified in the source
    "ambiguous",  # stated, but not as one firm value (alternatives, unit, wording)
    "contradiction",  # the part's source states two different values for one field
    "ownership_unverified",  # the value cannot be tied to one part
    "invalid",  # stated, but not a valid value (negative, unsupported format, ...)
    "unknown_operation",  # an operation the intake vocabulary does not recognize
    "unsupported_requirement",  # listed in the pinned rules' unsupported_operations
    "engineering_review",  # e.g. a tolerance standard to translate
    "catalog_no_match",
    "catalog_ambiguous",
    "catalog_not_checked",
    "catalog_unavailable",
    "rules_not_checked",
    "injection_suspected",
    "no_parts",
]
CatalogStatus = Literal["matched", "no_match", "ambiguous", "not_checked", "unavailable", "not_applicable"]  # fmt: skip
_STRICT = ConfigDict(extra="forbid", frozen=True)


class Finding(BaseModel):
    model_config = _STRICT
    part_index: int | None  # None: RFQ-level
    field: str | None
    kind: FindingKind
    detail: str | None = None  # the Day 36 reason code or a fixed code
    evidence: tuple[str, ...] = ()  # exact source text
    spans: tuple[tuple[int, int], ...] = ()
    action: Literal["clarify", "review"]


class CatalogCheck(BaseModel):
    """Provenance of a catalog lookup: tool, query and the exact outcome."""

    model_config = _STRICT
    status: CatalogStatus
    tool: Literal["search_materials"] | None = None
    query: tuple[tuple[str, str], ...] = ()
    material_id: str | None = None
    finish_id: str | None = None
    options: tuple[str, ...] = ()  # catalog grades offered when the grade is missing


class OperationCheck(BaseModel):
    model_config = _STRICT
    text: str
    status: Literal["recognized_not_checked", "unknown", "unsupported"]
    code: str | None = None  # catalog operation id or the pinned rule's unsupported name


class Clarification(BaseModel):
    model_config = _STRICT
    id: str  # stable: "<part index>:<field>:<kind>", for answering and re-validation
    part_index: int | None
    part_label: str | None
    field: str | None
    kind: FindingKind
    question: str


class PartValidation(BaseModel):
    model_config = _STRICT
    index: int
    label: str | None
    readiness: Readiness
    part_type: str | None = None
    verified: tuple[str, ...] = ()  # intake fields verified and not contradicted
    # Values the model missed but the part's own segment states exactly once (same Day 36
    # attribution, negation and alternative rules): provenance "source_scan", never model text.
    derived: tuple[FieldEvidence, ...] = ()
    findings: tuple[Finding, ...] = ()
    material: CatalogCheck
    finish: CatalogCheck
    operations: tuple[OperationCheck, ...] = ()
    # Built only when ready: every required field verified and catalog-recognized.
    specification: PartSpecification | None = None


class IntakeValidation(BaseModel):
    """Intake readiness. Never a manufacturing feasibility, inventory or price decision."""

    model_config = _STRICT
    source_id: str
    rfq_id: str | None
    readiness: Readiness
    manufacturing_rule_version: str | None = None
    manufacturing_rule_fingerprint: str | None = None
    parts: tuple[PartValidation, ...] = ()
    findings: tuple[Finding, ...] = ()  # RFQ-level
    clarifications: tuple[Clarification, ...] = ()
    feasibility_evaluated: Literal[False] = False


# --- vocabulary (catalog-aligned recognition, not decisions) --------------------------------

# Contract PartType values and their plain words (the same vocabulary as grounding.PART_KIND).
_PART_TYPES = (
    (r"\bl[\s_-]?brackets?\b", "l_bracket"),
    (r"\bu[\s_-]?brackets?\b", "u_bracket"),
    (r"\bmounting[\s_-]plates?\b|\bplates?\b", "mounting_plate"),
    (r"\bflat[\s_-]panels?\b|\bpanels?\b", "flat_panel"),
    (r"\benclosures?\b", "enclosure"),
    (r"\b(?:fabricated[\s_-])?assembl(?:y|ies)\b", "fabricated_assembly"),
    (r"\bbrackets?\b", "bracket"),  # L or U is not stated: ambiguous
)
# master_data/materials.csv categories. Plain "steel" is mild, stainless or galvanized: ambiguous.
_CATEGORIES = (
    (r"\bstainless(?:\s+steel)?\b", "stainless_steel"),
    (r"\bgalvani[sz]ed\s+steel\b", "galvanized_steel"),
    (r"\b(?:mild|carbon)\s+steel\b|\bcrs\b|\bhrs\b|\b(?:cold|hot)[\s-]rolled\b", "mild_steel"),
    (r"\balumin(?:um|ium)\b", "aluminum"),
    (r"\bcopper\b", "copper"),
    (r"\bbrass\b", "brass"),
    (r"\bsteel\b", "steel"),  # ambiguous
)
# master_data/finishes.csv ids.
_FINISHES = (
    (
        r"\bunfinished\b|\bno\s+(?:finish|coating)\b|\bnone\b|\bbare\b|\braw\b|\bmill\s+finish\b",
        "none",
    ),  # fmt: skip
    (r"\bpowder[\s-]?coat\w*", "powder_coat"),
    (r"\banodi[sz]\w*", "anodized"),
    (r"\bpassivat\w*", "passivated"),
    (r"\bgalvani[sz]\w*", "galvanized"),
    (r"\bbrush\w*", "brushed"),
    (r"\bpaint\w*", "painted"),
)
# master_data/operations.csv ids (no MCP operation catalog exists: recognition only).
_OPERATIONS = (
    (r"\blaser\b", "laser"),
    (r"\bpunch\w*", "punch"),
    (r"\bbend\w*|\bbent\b|\bpress[\s-]?brake\b|\bfold\w*", "bend"),
    (r"\bweld\w*|\bmig\b|\btig\b", "weld"),
    (r"\bmachin\w*|\bmill(?:ed|ing)\b", "machine"),
    (r"\binspect\w*", "inspect"),
    (r"\bpack\w*", "pack"),
)
# The pinned rules' unsupported_operations names, as customers write them.
_UNSUPPORTED = {
    "casting": r"\bcast(?:ing|ings)?\b",
    "forging": r"\bforg(?:e|ed|ing)\b",
    "additive_manufacturing": r"\b3d[\s-]?print\w*|\badditive\b",
}
_FIELD_OF = {  # PartSpecification field -> intake field (None: derived or catalog)
    "length_mm": "length", "width_mm": "width", "height_mm": "height",
    "thickness_mm": "thickness", "tolerance_mm": "tolerance", "quantity": "quantity",
    "part_type": None, "material_id": None, "finish_id": None, "weld_length_mm": None,
}  # fmt: skip
_LABELS = {
    "part_id": "part number or name",
    "part_type": "part type (flat panel, mounting plate, L-bracket, U-bracket, enclosure or fabricated assembly)",  # fmt: skip
    "material": "material", "material_id": "material and grade (for example aluminum 5052-H32)",
    "material_grade": "material grade", "finish": "finish", "finish_id": "finish (or confirm that no finish is needed)",  # fmt: skip
    "length": "length", "width": "width", "height": "height", "thickness": "thickness",
    "tolerance": "tolerance (for example ±0.1 mm)", "quantity": "quantity",
    "hole_count": "number of holes", "weld_length_mm": "total weld length",
    "requested_delivery_date": "requested delivery date", "operations": "operation",
}  # fmt: skip
_SCANNED = ("quantity", "hole_count", "thickness", "width", "length", "height", "tolerance", "finish")  # fmt: skip


def _codes(table, text: str) -> list[str]:
    """Codes named in ``text``; the first matching pattern wins for overlapping words."""
    found, taken = [], []
    for pattern, code in table:
        for m in re.finditer(pattern, text, re.I):
            if not any(a < m.end() and m.start() < b for a, b in taken):
                taken.append(m.span())
                found.append(code)
    return list(dict.fromkeys(found))


# --- contradiction scan --------------------------------------------------------------------


def _scan(field: str, text: str, scope: tuple[int, int]):
    """Distinct firm values the part's own source attributes to ``field`` (Day 36 rules), and
    the spans of values stated only as alternatives ("100 or 150")."""
    signed = text.translate(_MINUS)
    values: dict[object, list[tuple[int, int]]] = {}
    options: list[tuple[int, int]] = []
    negated: dict[object, list[tuple[int, int]]] = {}

    def add(value, span):
        if value is None:
            return
        problem = _context_problem(field, text, span)
        if problem is None:
            values.setdefault(value, []).append(span)
        elif _attribution_problem(field, text, span) is not None:
            return
        elif problem == "alternatives":
            options.append(span)
        elif problem == "negated_or_conditional":  # "Do not powder coat" (D37-04)
            negated.setdefault(value, []).append(span)

    if field in _COUNTS:
        for m in _COUNT_TOKEN.finditer(signed, *scope):
            add(_count(m.group(), _COUNTS[field])[0], m.span())
    elif field == "finish":
        for m in _FINISH_TERM.finditer(text, *scope):
            if re.match(r"\s+steel\b", text[m.end() :], re.I):
                continue  # "galvanized steel" is a material
            codes = _codes(_FINISHES, m.group())
            if codes:
                add(codes[0], m.span())
    else:
        for m in _MEASURE.finditer(signed, *scope):
            head = signed[: m.start(2)]
            if not m.group(3) or head.endswith(("-", "+")) and not head.endswith(("+/-", "+-")):
                continue
            add(_length(field, signed[m.start(2) : m.end()])[0], m.span())
    return values, options, negated


def _negated(text: str, span: tuple[int, int]) -> bool:
    begin, end = _clause(text, span)
    rest = f"{text[begin : span[0]]} {text[span[1] : end]}"
    return _NEGATION.search(_NOT_LATER.sub(" by ", rest)) is not None


def _scan_material(text: str, scope: tuple[int, int]):
    """Material categories the part's segment states (D37-02), split into firm and negated."""
    values: dict[object, list[tuple[int, int]]] = {}
    negated: dict[object, list[tuple[int, int]]] = {}
    taken: list[tuple[int, int]] = []
    for pattern, code in _CATEGORIES:
        for m in re.finditer(pattern, text[: scope[1]], re.I):
            if m.start() < scope[0] or any(a < m.end() and m.start() < b for a, b in taken):
                continue
            taken.append(m.span())
            if code == "steel" or _attribution_problem("material", text, m.span()) is not None:
                continue
            problem = _context_problem("material", text, m.span())
            if problem is None:
                values.setdefault(code, []).append(m.span())
            elif problem == "negated_or_conditional":
                negated.setdefault(code, []).append(m.span())
    return values, [], negated


_TOKEN = re.compile(r"(?<![\w./-])[A-Za-z0-9](?:[\w./-]*[A-Za-z0-9])?(?![\w/-])")
_GRADE_CUE = re.compile(r"(?i)\b(?:grades?|alloy|temper)\b")


_MATERIAL_NEXT = re.compile(rf"\s*(?:{_MATERIAL_TERM.pattern.removeprefix('(?i)')})", re.I)


def _scan_grade(text: str, scope: tuple[int, int], labels: frozenset[str] = frozenset()):
    """Catalog-shaped grades the part's segment states (D37-03, D37-09). A token counts only
    next to a material word, before ("aluminum 6061-T6") or after ("6061-T6 aluminum"), or
    in a clause naming a grade, when it is not a size, a count, a date or a part label
    ("P-100:", "BRK-200", "25 aluminum panels", "300 mm")."""
    values: dict[object, list[tuple[int, int]]] = {}
    negated: dict[object, list[tuple[int, int]]] = {}
    for m in _TOKEN.finditer(text, *scope):
        token = m.group()
        if not re.search(r"\d", token) or not _GRADE.fullmatch(token):
            continue
        begin, end = _clause(text, m.span())
        after = text[m.end() : end]
        if re.match(r"\s*:", after) or re.match(rf"(?i)\s*(?:{_UNITS}|{_OTHER_UNITS})(?![a-z])", after):  # fmt: skip
            continue
        if token.casefold() in labels:
            continue  # the customer's part identifier
        led = _GRADE_LEAD.search(text[begin : m.start()])
        # Grade-first: a code right before a material word. A bare number there is a count
        # when it reads as a quantity ("25 aluminum panels"), so it is not taken as a grade.
        trails = _MATERIAL_NEXT.match(after) and not (token.isdigit() and _attribution_problem("quantity", text, m.span()) is None)  # fmt: skip
        led = led or trails
        counted = _QUANTITY_CUE.search(after) or re.match(rf"\s*(?:[\w-]+\s+){{0,2}}?{_PART_NOUN}", after)  # fmt: skip
        if not led and not (_GRADE_CUE.search(text[begin:end]) and not counted):
            continue
        (negated if _negated(text, m.span()) else values).setdefault(token.casefold(), []).append(m.span())  # fmt: skip
    return values, [], negated


def _unsupported_in_source(text: str, spans, unsupported: set[str]):
    """Unsupported operations (pinned rules) the source requests, independent of the model;
    negated or prohibited mentions are not requests (D37-01)."""
    for name, pattern in _UNSUPPORTED.items():
        if name not in unsupported:
            continue
        for a, b in spans:
            for m in re.finditer(pattern, text[:b], re.I):
                if m.start() >= a and not _negated(text, m.span()):
                    yield name, m.span()


# --- RFQ-wide requirements and omitted operation requests (D37-07, D37-08) -------------

_GLOBAL = re.compile(r"(?i)\b(?:all|every|each|both|any)\b")
_SENTENCE_END = re.compile(r"[!?\n;]|\.(?!\d)")


def _sentence(text: str, span: tuple[int, int]) -> str:
    begin = max((m.end() for m in _SENTENCE_END.finditer(text, 0, span[0])), default=0)
    after = _SENTENCE_END.search(text, span[1])
    return text[begin : after.start() if after else len(text)]


def _rfq_requirements(text: str, gaps, labels: frozenset[str] = frozenset()) -> list[dict]:
    """Material, grade and finish statements outside every part's segment.

    A sentence with an explicit quantifier (all, every, each, both, any) applies to every
    part; otherwise its scope is unclear. (A statement limited to one part names its label,
    which starts or interleaves a segment, so it is never in this text.) Values are
    compared with each part's own, never copied into parts."""
    found = []
    for a, b in gaps:
        scans = {"material": _scan_material(text, (a, b)), "material_grade": _scan_grade(text, (a, b), labels), "finish": _scan("finish", text, (a, b))}  # fmt: skip
        for field, (values, _, negated) in scans.items():
            for prohibited, table in ((False, values), (True, negated)):
                for value, spans in table.items():
                    for span in spans:
                        scope = None if _GLOBAL.search(_sentence(text, span)) else "unclear"
                        found.append({"field": field, "value": value, "span": span, "prohibited": prohibited, "scope": scope})  # fmt: skip
    return found


def _part_value(field: str, verified: dict):
    f = verified.get(field)
    if field == "material":
        codes = [c for c in _codes(_CATEGORIES, f.value) if c != "steel"] if f else []
        return (codes[0], f) if len(codes) == 1 else (None, f)
    if field == "finish":
        codes = _codes(_FINISHES, f.value) if f else []
        return (codes[0], f) if len(codes) == 1 else (None, f)
    if f is None and "material" in verified:  # a grade inside the material text
        tokens = [t for t in verified["material"].value.split() if re.search(r"\d", t) and _GRADE.fullmatch(t)]  # fmt: skip
        return (tokens[0].casefold(), verified["material"]) if len(tokens) == 1 else (None, None)
    return (str(f.normalized).casefold(), f) if f else (None, None)


def _rfq_conflicts(index: int, verified: dict, requirements, text: str) -> list[Finding]:
    found = []
    for r in requirements:
        mine, evidence = _part_value(r["field"], verified)
        if mine is None:
            continue  # nothing stated for the part: the RFQ-wide value is not copied in
        clash = (mine == r["value"]) if r["prohibited"] else (mine != r["value"])
        if not clash:
            continue
        spans = tuple(s for s in (evidence.span, r["span"]) if s)
        common = {"part_index": index, "field": r["field"], "action": "clarify", "spans": spans, "evidence": tuple(text[a:b] for a, b in spans)}  # fmt: skip
        if r["scope"] == "unclear":
            found.append(Finding(kind="ownership_unverified", detail="rfq_wide_scope_unclear", **common))  # fmt: skip
        else:
            detail = "rfq_wide_prohibited" if r["prohibited"] else "rfq_wide_conflict"
            found.append(Finding(kind="contradiction", detail=detail, **common))
    return found


_REQUEST = re.compile(
    r"(?i)\b(?:also|please|then)\s+(?P<verb>[a-z]{3,})\s+(?:the|all|each|these|those|both|its|every)\b"
    r"|\b(?:requires?|required|needs?|needed|including|includes?|plus|with)\s+(?P<gerund>[a-z]{3,}ing)\b(?=\s*(?:$|[,.;:!?)]|\b(?:of|on|for|to|at|the|all|each|both)\b))"
)
# Request verbs that are not manufacturing operations (narrow and maintainable on purpose).
_NOT_OPERATIONS = frozenset(
    "make use send provide quote ship deliver include see note check confirm attach call "
    "contact let give keep list review advise consider find read sign return".split()
)


def _operation_requests(text: str, spans, verified_ops: list[str]):
    """Explicit operation requests in the source that are not recognized, finish, material or
    already-extracted words: unknown operations the model may have omitted (D37-08)."""
    for a, b in spans:
        for m in _REQUEST.finditer(text, a, b):
            word = (m.group("verb") or m.group("gerund")).casefold()
            span = m.span("verb") if m.group("verb") else m.span("gerund")
            if word in _NOT_OPERATIONS or _negated(text, span):
                continue
            if _codes(_OPERATIONS, word) or any(re.search(pat, word, re.I) for pat in _UNSUPPORTED.values()):  # fmt: skip
                continue  # recognized, or unsupported (the D37-01 scan reports it)
            if _FINISH_TERM.search(word) or _MATERIAL_TERM.search(word):
                continue
            if any(word in v.casefold() or v.casefold().startswith(word[:4]) for v in verified_ops):
                continue  # already extracted and classified
            yield word, span


# --- validation ----------------------------------------------------------------------------


def _clarify(part, field, kind, detail=None, fields=()) -> Finding:
    return Finding(
        part_index=part, field=field, kind=kind, detail=detail, action="clarify",
        evidence=tuple(f.evidence for f in fields), spans=tuple(f.span for f in fields if f.span),
    )  # fmt: skip


def _review(part, field, kind, detail=None, evidence=()) -> Finding:
    return Finding(part_index=part, field=field, kind=kind, detail=detail, evidence=tuple(evidence), action="review")  # fmt: skip


def _field_finding(index: int, f: FieldEvidence) -> Finding | None:
    """A non-verified Day 36 field as a validation finding (None: a model error, not asked)."""
    if f.status == "verified" or f.field in ("description", "notes", "general_notes"):
        return None  # free text is informational; the part type is asked separately
    if f.reason == "part_association_unverified" or f.reason == "shared_evidence":
        return _clarify(index, f.field, "ownership_unverified", f.reason, [f])
    if f.reason == "superseded_or_contradicted":
        return _clarify(index, f.field, "contradiction", "correction_stated", [f])
    if f.status == "ambiguous":
        return _clarify(index, f.field, "ambiguous", f.reason, [f])
    if f.status == "invalid":
        return _clarify(index, f.field, "invalid", f.reason, [f])
    return None  # unverified: the model's claim had no source support; never asked about


class _Catalog:
    """Bounded, sequential, read-only catalog access; every failure is ``unavailable``."""

    def __init__(self, execute: ToolExecutor | None) -> None:
        self.execute, self.calls, self.failed, self.reason = execute, 0, False, None

    def _unavailable(self, key, reason: str):
        self.reason = reason
        return "unavailable", key, ()

    async def search(self, **query: str) -> tuple[CatalogStatus, tuple, tuple]:
        """Every page of one query (D37-05), each row checked against the query (D37-06).
        Incomplete, inconsistent or failed results are never a match."""
        key = tuple(sorted(query.items()))
        if self.execute is None:
            return "not_checked", key, ()
        contract, items, offset = CONTRACTS[CATALOG_TOOL], [], 0
        while True:
            if self.failed:
                return self._unavailable(key, self.reason or "failed")
            if self.calls >= MAX_CATALOG_CALLS:
                return self._unavailable(key, "call_budget")
            self.calls += 1
            try:
                request = MaterialListQuery(**query, limit=MAX_PAGE_SIZE, offset=offset)
                raw = await self.execute(CATALOG_TOOL, request)
                data = raw.model_dump(mode="json") if isinstance(raw, BaseModel) else raw
                page = contract.output_model.model_validate(data)
            except Exception:  # McpCallError, invalid output, executor fault: never surfaced
                self.failed = True
                return self._unavailable(key, "failed")
            if page.offset != offset or any(not _row_matches(row, query) for row in page.items):
                self.failed = True  # a provider that answers another question is not trusted
                return self._unavailable(key, "inconsistent_response")
            items.extend(page.items)
            if not page.has_more:
                return "matched", key, tuple(items)
            offset += len(page.items)
            if not page.items or offset > MAX_OFFSET:
                return self._unavailable(key, "incomplete_results")


def _row_matches(row, query: dict) -> bool:
    if row.category != query.get("category"):
        return False
    return "grade" not in query or row.grade.casefold() == query["grade"].casefold()


async def _material(cat: _Catalog, verified: dict, index: int, findings: list) -> CatalogCheck:
    material = verified.get("material")
    if material is None:
        return CatalogCheck(status="not_applicable")
    categories = [c for c in _codes(_CATEGORIES, material.value)]
    if len(categories) != 1 or categories[0] == "steel":
        findings.append(_clarify(index, "material", "ambiguous", "material_category_unclear", [material]))  # fmt: skip
        return CatalogCheck(status="not_applicable")
    query = {"category": categories[0]}
    grade = verified.get("material_grade")
    if grade is None:  # a code-shaped grade inside the verified material text: "aluminum 5052-H32"
        tokens = [t for t in material.value.split() if re.search(r"\d", t) and _GRADE.fullmatch(t)]
        if len(tokens) == 1:
            grade = material.model_copy(update={"field": "material_grade", "value": tokens[0], "normalized": tokens[0]})  # fmt: skip
    if grade is not None:
        query["grade"] = str(grade.normalized)
    status, key, items = await cat.search(**query)
    check = {"tool": CATALOG_TOOL if status != "not_checked" else None, "query": key}
    if status in ("not_checked", "unavailable"):
        findings.append(_review(index, "material_id", f"catalog_{status}", cat.reason if status == "unavailable" else None))  # fmt: skip
        return CatalogCheck(status=status, **check)
    rows = [i for i in items if grade is None or i.grade.casefold() == str(grade.normalized).casefold()]  # fmt: skip
    if grade is None:
        options = tuple(sorted({i.grade for i in items}))
        if not options:
            findings.append(_review(index, "material_id", "catalog_no_match", None, [material.evidence]))  # fmt: skip
            return CatalogCheck(status="no_match", **check)
        findings.append(_clarify(index, "material_grade", "missing", "grade_required", [material]))
        return CatalogCheck(status="ambiguous", options=options, **check)
    if len(rows) == 1:
        return CatalogCheck(status="matched", material_id=rows[0].material_id, **check)
    kind = "catalog_no_match" if not rows else "catalog_ambiguous"
    findings.append(_review(index, "material_id", kind, None, [grade.evidence]))
    return CatalogCheck(status="no_match" if not rows else "ambiguous", **check)


async def _finish(cat, verified, material: CatalogCheck, index, findings) -> CatalogCheck:
    finish = verified.get("finish")
    if finish is None:
        return CatalogCheck(status="not_applicable")
    codes = _codes(_FINISHES, finish.value)
    if len(codes) != 1:
        kind = "ambiguous" if codes else "engineering_review"
        detail = "several_finishes" if codes else "finish_not_in_catalog_vocabulary"
        findings.append(_clarify(index, "finish", kind, detail, [finish]) if codes else _review(index, "finish_id", kind, detail, [finish.evidence]))  # fmt: skip
        return CatalogCheck(status="not_applicable")
    if material.status != "matched":
        if material.status in ("not_checked", "unavailable"):
            return CatalogCheck(status=material.status)  # already reported for the material
        return CatalogCheck(status="not_checked")
    query = dict(material.query) | {"finish_id": codes[0]}
    status, key, items = await cat.search(**query)
    check = {"tool": CATALOG_TOOL, "query": key}
    if status == "unavailable":
        findings.append(_review(index, "finish_id", "catalog_unavailable", cat.reason))
        return CatalogCheck(status=status, **check)
    if any(i.material_id == material.material_id for i in items):
        return CatalogCheck(status="matched", finish_id=codes[0], **check)
    # Not listed for this material: incompatible or unknown. Feasibility is not decided here.
    findings.append(_review(index, "finish_id", "catalog_no_match", codes[0], [finish.evidence]))
    return CatalogCheck(status="no_match", **check)


def _operations(part: IntakePart, verified: dict, index: int, findings: list, rules):
    unsupported = set(rules.document.get("unsupported_operations", ())) if rules else set()
    checks = []
    for f in part.fields:
        if f.field != "operations" or f.status != "verified":
            continue
        hit = next((n for n, p in _UNSUPPORTED.items() if n in unsupported and re.search(p, f.value, re.I)), None)  # fmt: skip
        if hit:
            checks.append(OperationCheck(text=f.value, status="unsupported", code=hit))
            findings.append(_review(index, "operations", "unsupported_requirement", hit, [f.evidence]))  # fmt: skip
            continue
        codes = _codes(_OPERATIONS, f.value)
        if len(codes) == 1:
            checks.append(OperationCheck(text=f.value, status="recognized_not_checked", code=codes[0]))  # fmt: skip
        else:
            checks.append(OperationCheck(text=f.value, status="unknown"))
            findings.append(_clarify(index, "operations", "unknown_operation", None, [f]))
    finish, material = verified.get("finish"), verified.get("material")
    if "anodize_steel" in unsupported and finish and material:
        if "anodized" in _codes(_FINISHES, finish.value) and re.search(r"\bsteel\b", material.value, re.I):  # fmt: skip
            findings.append(_review(index, "finish_id", "unsupported_requirement", "anodize_steel", [finish.evidence]))  # fmt: skip
    return tuple(checks)


def _part_type(part: IntakePart, text: str, verified: dict, index, findings, rules) -> str | None:
    sources = [verified["description"].value] if "description" in verified else []
    if part.scope is not None:
        sources += [m.group() for m in PART_KIND.finditer(text, *part.scope)]
    codes = list(dict.fromkeys(c for s in sources for c in _codes(_PART_TYPES, s)))
    allowed = set(rules.document["allowed_part_types"]) if rules else None
    if len(codes) == 1 and codes[0] != "bracket":
        if allowed is not None and codes[0] not in allowed:
            findings.append(_review(index, "part_type", "unsupported_requirement", codes[0]))
            return None
        return codes[0]
    if codes:
        detail = "bracket_type_unclear" if codes == ["bracket"] else "several_part_types"
        findings.append(Finding(part_index=index, field="part_type", kind="ambiguous", detail=detail, action="clarify"))  # fmt: skip
    return None


async def _validate_part(part: IntakePart, result: IntakeResult, text: str, rules, cat, requirements=()) -> PartValidation:  # fmt: skip
    i, findings = part.index, []
    verified = {f.field: f for f in part.fields if f.status == "verified"}
    for f in part.fields:
        finding = _field_finding(i, f)
        if finding is not None:
            findings.append(finding)
    scans = {}
    if part.scope is not None:  # cross-clause contradictions inside the part's own segment
        for field in (*_SCANNED, "material", "material_grade"):
            if field == "material":
                scans[field] = _scan_material(text, part.scope)
            elif field == "material_grade":
                labels = frozenset(p.label.casefold() for p in result.parts if p.label)
                scans[field] = _scan_grade(text, part.scope, labels)
            else:
                scans[field] = _scan(field, text, part.scope)
            values, options, negated = scans[field]
            clash = [v for v in negated if v in values]  # a verified value is always in values
            if clash:  # requested and prohibited, in different clauses or sentences (D37-04)
                spans = tuple(s for v in clash for s in (*values.get(v, ()), *negated[v]))
                findings.append(Finding(
                    part_index=i, field=field, kind="contradiction", action="clarify",
                    detail="requested_and_prohibited",
                    evidence=tuple(text[a:b] for a, b in spans), spans=spans,
                ))  # fmt: skip
                verified.pop(field, None)
            if options and not values and field not in {f.field for f in findings}:
                findings.append(Finding(
                    part_index=i, field=field, kind="ambiguous", detail="alternatives", action="clarify",
                    evidence=tuple(text[a:b] for a, b in options), spans=tuple(options),
                ))  # fmt: skip
            if len(values) > 1:
                spans = tuple(s for spans in values.values() for s in spans)
                corrected = any(_CORRECTION.search(text[max(0, a - 80) : b + 40]) for a, b in spans)  # fmt: skip
                findings.append(Finding(
                    part_index=i, field=field, kind="contradiction", action="clarify",
                    detail="correction_stated" if corrected else "conflicting_values",
                    evidence=tuple(text[a:b] for a, b in spans), spans=spans,
                ))  # fmt: skip
                verified.pop(field, None)
    derived = []
    if part.scope is not None:
        for field, (values, options, _) in scans.items():
            if field == "material_grade":
                continue  # grades are looked up only from verified text, never derived
            # Two or more values already raised a contradiction finding above.
            if field in verified or field in {f.field for f in findings} or not values or options:  # fmt: skip
                continue
            value, spans = next(iter(values.items()))
            if len(spans) != 1:
                continue  # stated twice: not one firm value
            # A rejected proposal that carries customer meaning (negated, ambiguous, invalid)
            # already raised a finding above and is skipped; an unsupported one is model error.
            a, b = spans[0]
            f = FieldEvidence(field=field, value=text[a:b], evidence=text[a:b], span=(a, b), status="verified", normalized=value, transformation="source_scan")  # fmt: skip
            verified[field] = f
            derived.append(f)
    part_type = _part_type(part, text, verified, i, findings, rules)
    if rules is not None:
        unsupported = set(rules.document.get("unsupported_operations", ()))
        for name, (a, b) in _unsupported_in_source(text, [part.scope] if part.scope else [], unsupported):  # fmt: skip
            findings.append(Finding(part_index=i, field="operations", kind="unsupported_requirement", detail=name, evidence=(text[a:b],), spans=((a, b),), action="review"))  # fmt: skip
    if part.scope is not None:
        for conflict in _rfq_conflicts(i, verified, requirements, text):
            findings.append(conflict)
            verified.pop(conflict.field, None)  # contested: no catalog lookup, no spec value
        ops = [f.value for f in part.fields if f.field == "operations" and f.status == "verified"]
        for word, (a, b) in _operation_requests(text, [part.scope], ops):
            findings.append(Finding(part_index=i, field="operations", kind="unknown_operation", detail="source_request", evidence=(text[a:b],), spans=((a, b),), action="clarify"))  # fmt: skip
    contradicted = {f.field for f in findings if f.kind == "contradiction"}
    if contradicted & {"material", "material_grade"}:
        verified.pop("material", None)  # no catalog id from a contested material or grade
        verified.pop("material_grade", None)
    material = await _material(cat, verified, i, findings)
    finish = await _finish(cat, verified, material, i, findings)
    operations = _operations(part, verified, i, findings, rules)
    tol = verified.get("tolerance")
    if tol is not None and not isinstance(tol.normalized, Decimal):
        findings.append(_review(i, "tolerance", "engineering_review", "tolerance_standard", [tol.evidence]))  # fmt: skip
    values = {
        "part_type": part_type,
        "material_id": material.material_id,
        "finish_id": finish.finish_id,
        "tolerance_mm": tol.normalized if tol and isinstance(tol.normalized, Decimal) else None,
        **{
            s: verified[f].normalized
            for s, f in _FIELD_OF.items()
            if f and f != "tolerance" and f in verified
        },  # fmt: skip
    }
    if "hole_count" in verified:
        values["hole_count"] = verified["hole_count"].normalized
    raised = {f.field for f in findings}
    raised |= {"finish_id"} if "finish" in raised else set()
    raised |= {"material_id"} if raised & {"material", "material_grade"} else set()
    if rules is None:
        findings.append(_review(i, None, "rules_not_checked"))
    else:
        required = [f for f in rules.document["mandatory_fields"]]
        required += ["height_mm"] if part_type in rules.document["height_required"] else []
        required += ["weld_length_mm"] if part_type in rules.document["weld_length_required"] else []  # fmt: skip
        for spec_field in required:
            intake_field = _FIELD_OF.get(spec_field)
            if values.get(spec_field) is not None or {spec_field, intake_field} & raised:
                continue
            if spec_field == "material_id" and ({"material", "material_grade"} & raised or material.status != "not_applicable"):  # fmt: skip
                continue  # already asked or reported through the catalog
            if spec_field == "finish_id" and finish.status != "not_applicable":
                continue
            findings.append(Finding(part_index=i, field=intake_field or spec_field, kind="missing", action="clarify"))  # fmt: skip
    if not part.label_verified:
        findings.append(Finding(part_index=i, field="part_id", kind="missing", detail="no_identifier", action="clarify"))  # fmt: skip
    findings = list(dict.fromkeys(findings))
    if any(f.action == "clarify" for f in findings):
        readiness = "needs_clarification"
    elif findings:
        readiness = "requires_review"
    else:
        readiness = "ready_for_manufacturing_evaluation"
    spec = None
    if readiness == "ready_for_manufacturing_evaluation":
        try:
            spec = PartSpecification(part_id=part.label, **{k: v for k, v in values.items() if v is not None})  # fmt: skip
        except ValidationError:
            findings.append(_review(i, None, "engineering_review", "specification_invalid"))
            readiness = "requires_review"
    contradicted = {f.field for f in findings if f.kind == "contradiction"}
    return PartValidation(
        index=i, label=part.label, readiness=readiness, part_type=part_type,
        verified=tuple(f for f in verified if f not in contradicted), findings=tuple(findings),
        material=material, finish=finish, operations=operations, specification=spec,
        derived=tuple(derived),
    )  # fmt: skip


def _who(p: PartValidation | None, single: bool) -> str:
    if p is None:
        return "Your request"
    if single:
        return "Your part"
    return f"Part '{p.label}'" if p.label else f"The part listed as number {p.index}"


def _question(f: Finding, p: PartValidation | None, single: bool) -> str:
    who, label = _who(p, single), _LABELS.get(f.field, f.field or "detail")
    shown = ", ".join(f"'{e[:80]}'" for e in dict.fromkeys(f.evidence))
    if f.kind == "no_parts":
        return "We could not identify the parts you need. Please list each part with its quantity, material, dimensions and finish."  # fmt: skip
    if f.kind == "contradiction" and f.detail in (
        "requested_and_prohibited",
        "rfq_wide_prohibited",
    ):
        return f"{who}: the {label} {shown} is both requested and ruled out in your request. Is it required?"  # fmt: skip
    if f.kind == "contradiction" and len(set(f.evidence)) <= 1:
        return f"{who}: your request mentions a correction. Please confirm the {label} ({shown})."
    if f.kind == "contradiction":
        extra = " (a correction is mentioned)" if f.detail == "correction_stated" else ""
        return f"{who}: the {label} is stated in more than one way ({shown}){extra}. Which value is correct?"  # fmt: skip
    if f.kind == "ownership_unverified":
        return f"{who}: please confirm which part {shown} applies to."
    if f.kind == "invalid":
        return f"{who}: the {label} {shown} is not a valid value. Please restate it."
    if f.kind == "unknown_operation":
        return f"{who}: please describe the operation {shown} in more detail."
    if f.kind == "missing" and f.field == "material_grade" and p and p.material.options:
        return f"{who}: which grade do you need? Available: {', '.join(p.material.options)}."
    if f.kind == "missing":
        return f"{who}: please provide the {label}."
    detail = {
        "alternatives": f"more than one option is given for the {label} ({shown}). Which one do you need?",  # fmt: skip
        "multiple_values": f"more than one option is given for the {label} ({shown}). Which one do you need?",  # fmt: skip
        "unit_not_stated": f"please state the unit for the {label} ({shown}).",
        "negated_or_conditional": f"please confirm whether {shown} is required for the {label}.",
        "field_not_attributable": f"please confirm what {shown} refers to.",
        "value_reused": f"please confirm what {shown} refers to.",
        "material_category_unclear": f"please specify the material type for {shown} (for example mild steel, stainless steel or galvanized steel).",  # fmt: skip
        "bracket_type_unclear": "is it an L-bracket or a U-bracket?",
        "several_part_types": "several part types are mentioned. Which one is this part?",
        "several_finishes": f"more than one finish is mentioned ({shown}). Which one do you need?",
    }
    return f"{who}: {detail.get(f.detail, f'please clarify the {label} ({shown}).')}"


def _clarifications(parts, rfq_findings, single: bool) -> tuple[Clarification, ...]:
    """One question per (part, field): the most specific finding wins; no duplicates."""
    rank = {"contradiction": 0, "ownership_unverified": 1, "invalid": 2, "ambiguous": 3, "unknown_operation": 4, "missing": 5, "no_parts": 6}  # fmt: skip
    chosen: dict[tuple, tuple[Finding, PartValidation | None]] = {}
    for p, findings in [(None, rfq_findings), *((p, p.findings) for p in parts)]:
        for f in findings:
            if f.action != "clarify":
                continue
            key = (f.part_index, f.field)
            order = (rank[f.kind], -len(set(f.evidence)))  # the most informative finding
            if key in chosen and f.kind == chosen[key][0].kind == "unknown_operation":
                merged = tuple(dict.fromkeys((*chosen[key][0].evidence, *f.evidence)))
                chosen[key] = (chosen[key][0].model_copy(update={"evidence": merged}), p)
            elif key not in chosen or order < (
                rank[chosen[key][0].kind],
                -len(set(chosen[key][0].evidence)),
            ):
                chosen[key] = (f, p)
    return tuple(
        Clarification(
            id=f"{f.part_index or 0}:{f.field or '-'}:{f.kind}", part_index=f.part_index,
            part_label=p.label if p else None, field=f.field, kind=f.kind,
            question=_question(f, p, single),
        )
        for f, p in chosen.values()
    )  # fmt: skip


async def validate_intake(
    result: IntakeResult,
    text: str,
    *,
    rules: RuleVersionDetails | None = None,
    catalog: ToolExecutor | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> IntakeValidation:
    """Deterministic Intake validation of a Day 36 result against its own source text.

    ``text`` must be the exact text the result was extracted from (sha256-checked); ``rules``
    is the pinned manufacturing rule version; ``catalog`` is a trusted read-only executor."""
    if not isinstance(result, IntakeResult):
        raise ValueError("validate_intake needs an IntakeResult")
    base = {"source_id": result.source_id, "rfq_id": result.rfq_id}
    if result.status == "error":
        return IntakeValidation(**base, readiness="not_evaluated")
    if not isinstance(text, str) or result.source_sha256 != hashlib.sha256(text.encode()).hexdigest():  # fmt: skip
        raise ValueError("text is not the source of this IntakeResult")
    if rules is not None:
        require_manufacturing_rules(rules)
        base |= {"manufacturing_rule_version": rules.rule_version, "manufacturing_rule_fingerprint": rules.fingerprint}  # fmt: skip
    rfq_findings: list[Finding] = []
    if not result.parts:
        rfq_findings.append(Finding(part_index=None, field=None, kind="no_parts", action="clarify"))
    if result.injection_suspected:
        rfq_findings.append(_review(None, None, "injection_suspected"))
    covered = sorted(p.scope for p in result.parts if p.scope is not None)
    gaps, at = [], 0  # text outside every part's own segment
    for a, b in covered:
        gaps += [(at, a)] if a > at else []
        at = max(at, b)
    gaps += [(at, len(text))] if at < len(text) else []
    labels = frozenset(p.label.casefold() for p in result.parts if p.label)
    requirements = _rfq_requirements(text, gaps, labels) if covered else []
    for word, (a, b) in _operation_requests(text, gaps, []):  # RFQ-wide unknown operations
        rfq_findings.append(Finding(part_index=None, field="operations", kind="unknown_operation", detail="source_request", evidence=(text[a:b],), spans=((a, b),), action="clarify"))  # fmt: skip
    if rules is not None:  # unsupported requests outside every part's own segment (D37-01)
        unsupported = set(rules.document.get("unsupported_operations", ()))
        for name, (a, b) in _unsupported_in_source(text, gaps, unsupported):
            rfq_findings.append(Finding(part_index=None, field="operations", kind="unsupported_requirement", detail=name, evidence=(text[a:b],), spans=((a, b),), action="review"))  # fmt: skip
    for f in result.rfq_fields:
        finding = _field_finding(0, f)
        if finding is not None:
            rfq_findings.append(finding.model_copy(update={"part_index": None}))
    cat = _Catalog(catalog)
    try:
        async with asyncio.timeout(timeout_seconds):
            parts = [
                await _validate_part(p, result, text, rules, cat, requirements)
                for p in result.parts
            ]
    except TimeoutError:
        cat.execute, cat.failed = None, True
        parts = [
            p.model_copy(
                update={
                    "readiness": "requires_review"
                    if p.readiness == "ready_for_manufacturing_evaluation"
                    else p.readiness,
                    "specification": None,
                }
            )  # fmt: skip
            for p in [
                await _validate_part(p, result, text, rules, _Catalog(None)) for p in result.parts
            ]  # fmt: skip
        ]
        rfq_findings.append(_review(None, None, "catalog_unavailable", "timeout"))
    rfq_findings = list(dict.fromkeys(rfq_findings))
    blocking = {f.kind for f in rfq_findings} & {"unsupported_requirement", "unknown_operation"}
    if blocking:
        # RFQ-wide wording ("Cast all parts.", "Also knurl all edges.") may apply to every
        # part and cannot be safely assigned: no part is ready.
        level = "needs_clarification" if "unknown_operation" in blocking else "requires_review"
        ready = "ready_for_manufacturing_evaluation"
        parts = [
            p.model_copy(
                update={
                    "specification": None,
                    "readiness": level if p.readiness == ready else p.readiness,
                }
            )  # fmt: skip
            for p in parts
        ]
    every = [*rfq_findings, *(f for p in parts for f in p.findings)]
    if any(f.action == "clarify" for f in every):
        readiness = "needs_clarification"
    elif every:
        readiness = "requires_review"
    else:
        readiness = "ready_for_manufacturing_evaluation"
    return IntakeValidation(
        **base,
        readiness=readiness,
        parts=tuple(parts),
        findings=tuple(rfq_findings),
        clarifications=_clarifications(parts, rfq_findings, len(parts) == 1),
    )
