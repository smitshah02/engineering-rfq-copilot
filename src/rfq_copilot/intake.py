"""RFQ Intake Agent foundation (Day 36): customer RFQ text -> provisional, evidence-checked
part extraction. Not validation, not feasibility, not a quote.

    START -> prepare_input -> extract -> verify -> END
       (any node) ---------------------------> safe_error -> END

- Input is text the authorized caller supplies, with a caller-assigned ``source_id`` (and
  optionally a trusted ``rfq_id``). No MCP tool is offered or called; attachment retrieval is
  deferred until an authorized intake tool exists.
- One model call (``llm.invoke_structured``) returns an ``ExtractionProposal``: per-part fields
  as ``{value, quote}`` strings. The proposal is untrusted. ``verify_extraction`` decides
  everything deterministically: the quote must occur in the source (its real span is
  recorded), inside the part's own segment when parts are labelled; the value must occur in
  the quote; numbers are parsed exactly and units converted only by the fixed table
  ``_UNIT_MM``. Anything else is kept with a non-verified status and a fixed reason, never
  silently trusted, defaulted or borrowed from another part.
- Results are always ``readiness="pending_validation"``. Completeness rules, clarification
  questions and catalog mapping are Day 37.
- Security: customer text goes only in an untrusted data block (``llm.build_messages``);
  credential-looking input or output fails as ``unsafe_content``; the proposal schema has
  no identity, customer, price or decision fields (``extra="forbid"``); state holds no
  credentials and the graph has no checkpointer; failures carry a fixed code only.
"""

import asyncio
import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, ValidationError

from rfq_copilot import llm
from rfq_copilot.api.schemas.common import INT32_MAX, Identifier
from rfq_copilot.api.schemas.rfq import MAX_PARTS, MAX_RAW_REQUEST_CHARS, PartSpecification
from rfq_copilot.grounding import ITEM_NUMBER, PART_KIND  # Phase 5 vocabulary, reused

RECURSION_LIMIT = 6  # longest path is 4 node steps
DEFAULT_TIMEOUT = 120.0  # seconds, the one model call included
MAX_LIST_ITEMS = 20

INSTRUCTIONS = (
    "You are the RFQ intake extractor for a sheet-metal manufacturer. Read the customer "
    "request in the untrusted data block and extract it; do not judge feasibility, price, "
    "stock or delivery.\n"
    "Parts: return exactly one entry per distinct requested part, in source order. One entry "
    "holds every stated field of that part. Never split one part into several entries and "
    "never add an entry for something that is not a requested part.\n"
    "Fields: use null for every field the request does not state for that part. When you "
    "fill a field, `value` is only that value in the customer's words (a number keeps its "
    "unit, e.g. '3 mm'; a material is e.g. 'aluminum'; a finish is e.g. 'brushed'), never a "
    "whole sentence, and `quote` is the short exact source phrase stating it (e.g. '3 mm "
    "thick'). Never copy a value from one part to another; never guess or use defaults. If "
    "the request gives alternatives or conflicting values, put all of them in `value` (e.g. "
    "'100 or 150').\n"
    "`label` is only an identifier the request uses for the part (e.g. 'Part A', 'item 2', "
    "'BRK-200'), else null; never a description or specification.\n\n"
    "Example request: 'Item 1: 20 galvanized steel mounting plates, 5 mm thick, 4 holes.' "
    'Example answer: {"parts": [{"label": "Item 1", "description": {"value": "mounting '
    'plates", "quote": "mounting plates"}, "quantity": {"value": "20", "quote": "20 '
    'galvanized steel mounting plates"}, "material": {"value": "galvanized steel", "quote": '
    '"galvanized steel"}, "material_grade": null, "thickness": {"value": "5 mm", "quote": '
    '"5 mm thick"}, "width": null, "length": null, "height": null, "hole_count": {"value": '
    '"4", "quote": "4 holes"}, "finish": null, "tolerance": null, "operations": [], "notes": '
    '[]}], "requested_delivery_date": null, "general_notes": []}'
)

_STRICT = ConfigDict(extra="forbid", frozen=True)


def _all_keys_required(schema: dict) -> None:
    # Shown to the provider only: every key must be emitted (null when not stated), which
    # makes small local models consider each field. Validation still accepts omitted keys.
    schema["required"] = list(schema.get("properties", {}))


_PROPOSAL = ConfigDict(extra="forbid", frozen=True, json_schema_extra=_all_keys_required)
_Text = Annotated[str, Field(strict=True, min_length=1, max_length=500)]


# --- model-facing proposal (untrusted) ---------------------------------------------------


class Evidenced(BaseModel):
    """One stated value and the exact source text that states it."""

    model_config = _STRICT
    value: StrictInt | Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    quote: _Text


_EvidenceList = Annotated[list[Evidenced], Field(max_length=MAX_LIST_ITEMS)]


class PartProposal(BaseModel):
    """One requested part. Use null for every field the request does not state for it."""

    model_config = _PROPOSAL
    label: Annotated[str, Field(strict=True, min_length=1, max_length=64)] | None = None
    description: Evidenced | None = Field(None, description="What the part is, e.g. 'brackets'")
    quantity: Evidenced | None = None
    material: Evidenced | None = Field(None, description="e.g. 'aluminum', 'stainless steel'")
    material_grade: Evidenced | None = Field(None, description="e.g. '6061-T6', '304'")
    thickness: Evidenced | None = None
    width: Evidenced | None = None
    length: Evidenced | None = None
    height: Evidenced | None = None
    hole_count: Evidenced | None = None
    finish: Evidenced | None = Field(None, description="As written, e.g. 'unfinished'")
    tolerance: Evidenced | None = None
    operations: _EvidenceList = Field([], description="Requested operations, e.g. 'bending'")
    notes: _EvidenceList = Field([], description="Other technical requirements for this part")


class ExtractionProposal(BaseModel):
    """Structured extraction of a customer RFQ."""

    model_config = _PROPOSAL
    parts: Annotated[list[PartProposal], Field(max_length=MAX_PARTS)]
    requested_delivery_date: Evidenced | None = Field(
        None, description="Only a delivery date the customer wrote"
    )
    general_notes: _EvidenceList = Field([], description="Requirements not tied to one part")


# --- verified result ---------------------------------------------------------------------

FieldStatus = Literal["verified", "ambiguous", "unverified", "invalid"]
Reason = Literal[
    "evidence_not_in_source",
    "evidence_outside_part",
    "value_not_in_evidence",
    "shared_evidence",
    "value_reused",
    "part_association_unverified",
    "field_not_attributable",
    "negated_or_conditional",
    "superseded_or_contradicted",
    "alternatives",
    "multiple_values",
    "unit_not_stated",
    "unsupported_format",
    "out_of_range",
    "precision_exceeds_contract",
]
Normalized = int | Decimal | date | str | None
ErrorCode = llm.FailureCode | Literal["invalid_input", "execution_limit", "internal_error"]


class FieldEvidence(BaseModel):
    """One extracted value with its provenance and verification outcome."""

    model_config = _STRICT
    field: str
    value: str  # as proposed by the model
    evidence: str  # the source text at ``span`` when found, else the model's quote
    span: tuple[int, int] | None  # [start, end) offsets into the caller's source text
    status: FieldStatus
    reason: Reason | None = None
    normalized: Normalized = None  # only when status == "verified"
    transformation: str | None = None  # the deterministic rule that produced ``normalized``


class IntakePart(BaseModel):
    model_config = _STRICT
    index: int  # 1-based order in the extraction; not a customer part number
    label: str | None  # customer's own name for the part, as proposed
    label_verified: bool
    fields: tuple[FieldEvidence, ...]
    missing: tuple[str, ...]  # scalar fields the extraction did not state
    scope: tuple[int, int] | None = None  # the part's source segment, when boundaries hold

    def get(self, name: str) -> FieldEvidence | None:
        return next((f for f in self.fields if f.field == name), None)


class IntakeResult(BaseModel):
    """Provisional extraction. Never validated for manufacturing and never quote-ready."""

    model_config = _STRICT
    status: Literal["extracted", "extracted_with_issues", "no_parts_found", "error"]
    source_id: str
    rfq_id: str | None = None
    readiness: Literal["pending_validation"] = "pending_validation"
    part_boundaries: Literal["single_part", "labelled", "unverified", "none"] = "none"
    injection_suspected: bool = False
    parts: tuple[IntakePart, ...] = ()
    rfq_fields: tuple[FieldEvidence, ...] = ()
    error: ErrorCode | None = None
    source_sha256: str | None = None  # of the verified source text (Day 37 validation input)
    discarded_empty_entries: int = 0  # model entries with no label and nothing stated


# --- deterministic verification ----------------------------------------------------------

SCALAR_FIELDS = (
    "description", "quantity", "material", "material_grade", "thickness", "width", "length",
    "height", "hole_count", "finish", "tolerance",
)  # fmt: skip
_COUNTS = {"quantity": 1, "hole_count": 0}  # field -> minimum
_LENGTHS = ("thickness", "width", "length", "height", "tolerance")
_CHOICE = ("material", "material_grade", "finish")  # "A or B" here is an undecided choice
# Exact conversions only. ponytail: add units with a documented exact factor when needed.
_UNIT_MM = {
    "mm": Decimal(1), "millimeter": Decimal(1), "millimeters": Decimal(1),
    "millimetre": Decimal(1), "millimetres": Decimal(1),
    "cm": Decimal(10), "in": Decimal("25.4"), "inch": Decimal("25.4"),
    "inches": Decimal("25.4"), '"': Decimal("25.4"),
}  # fmt: skip
_WORDS = dict(
    zip(
        "zero one two three four five six seven eight nine ten eleven twelve".split(),
        range(13),
        strict=True,
    )
)
_NO_FINISH = {"unfinished", "no finish", "none", "no coating", "bare", "raw", "mill finish"}
_UNITS = "|".join(map(re.escape, sorted(_UNIT_MM, key=len, reverse=True)))
# Never interpreted: gauge depends on material, and metres/feet/mils are not in _UNIT_MM.
_OTHER_UNITS = r"m|meters?|metres?|ft|foot|feet|gauge|ga|mils?|thou|um|µm"
_MEASURE = re.compile(
    rf"(?<![\w.,])(-?)(\d+(?:[.,]\d+)*)\s*(?:({_UNITS})|({_OTHER_UNITS})\b)?(?![a-z])",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"-?\d[\d,.]*\d|-?\d")
_ALTERNATIVE = re.compile(r"(?i)\b(?:or|either)\b|/")
# Every dash or minus that can sign a number becomes "-" (one char each, so offsets hold):
# U+2212 minus, figure/en/em dash, small and full-width hyphen-minus.
_MINUS = str.maketrans(dict.fromkeys("\u2212\u2012\u2013\u2014\ufe63\uff0d", "-"))
_TOLERANCE_PREFIX = re.compile(r"^\s*(?:±|\+/-|\+-)\s*")
_BAD_NUMBER = re.compile(r"\+\s*\d")  # an explicit "+5" is never silently read as 5
# Clause context of a value occurrence (D36-01/02/03/07).
_CLAUSE_END = re.compile(r"(?i)[,;\n()\[\]!?]|\.(?!\d)|\b(?:and|with|plus|but|while)\b")
_NEGATION = re.compile(
    r"(?i)\b(?:not|never|without|avoid\w*|exclud\w*|except|instead|unless|if|optional\w*"
    r"|maybe|perhaps|possibly|prefer\w*|cannot|nor)\b"
    r"|\bno\b(?!\.|\s*\d)|n'?t\b"
)
_NOT_LATER = re.compile(r"(?i)\b(?:no|not)\s+later\s+than\b")  # a deadline, not a negation
_CHOICE_WORDS = re.compile(r"(?i)\b(?:or|either|between|alternatively)\b|(?<=\w)/(?=\w)")
_RANGE = re.compile(r"(?i)\d\s*(?:-|to)\s*\d")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CUES = {
    "thickness": r"\bthick(?:ness)?\b|\bt\s*=",
    "width": r"\bwide\b|\bwidth\b|\bw\s*=",
    "length": r"\blong\b|\blength\b|\bl\s*=",
    "height": r"\bhigh\b|\bheight\b|\btall\b|\bh\s*=",
    "tolerance": r"\btol(?:erance)?s?\b|±|\+/-|\+-",
    "hole_count": r"\bholes?\b",
    "requested_delivery_date": r"\bdeliver\w*|\bdue\b|\bship\w*|\bby\b|\bbefore\b"
    r"|\barriv\w*|\blater\s+than\b",
}
_CUES = {k: re.compile(v, re.IGNORECASE) for k, v in _CUES.items()}
_DIMENSIONS = ("thickness", "width", "length", "height")
_PART_NOUN = f"(?i:{PART_KIND.pattern.removeprefix('(?i)')})"
_QUANTITY_CUE = re.compile(
    r"(?i)\b(?:qty|quantity|quantities|pcs|pieces?|units?|off|ea|each|sets?)\b"
)
_FREE_TEXT = ("notes", "general_notes")  # verbatim requirements: negation is content there
# A customer part identifier, not a description: "Part A", "item 2", "P/N 7731", "BRK-200".
_KEYWORD_ID = re.compile(
    r"(?i)(?:part|item|line|pos(?:ition)?|p/n|pn|drawing|dwg)(?:\s*(?:no\.?|number|#))?"
    r"\s*[:#]?\s*[a-z0-9][\w./-]{0,31}"
)
_CODE_ID = re.compile(r"(?i)(?=[\w./-]*\d)[a-z0-9][\w./-]{0,31}|[a-z]{1,2}")
_PART_MARKER = re.compile(r"\b(?i:part)\s*#?\s*([A-Z]|\d+)\b")
# A code-style label introducing a part: letters and digits, then a colon (D36-13).
_CODE_LABEL = re.compile(
    r"(?<![\w./-])((?=[\w./-]*[A-Za-z])(?=[\w./-]*\d)[A-Za-z0-9][\w./-]{0,31})\s*:"
)
# Narrow recognition aligned with the catalog (master_data materials/finishes), D36-10.
# Not catalog mapping: an unrecognized word is simply not verified as that field.
_MATERIAL_TERM = re.compile(
    r"(?i)\b(?:alumin(?:um|ium)|steel|stainless|copper|brass|bronze|titanium|crs|hrs"
    r"|cold[\s-]rolled|hot[\s-]rolled)\b"
)
_FINISH_TERM = re.compile(
    r"(?i)\b(?:powder[\s-]?coat\w*|anodi[sz]\w*|passivat\w*|galvani[sz]\w*|brush\w*"
    r"|paint\w*|polish\w*|plat(?:ed|ing)|e-?coat\w*|bead[\s-]?blast\w*|chromat\w*"
    r"|unfinished|finish|bare|raw|none)\b|\bno\s+coating\b"
)
_GRADE = re.compile(r"(?:[A-Za-z]{1,3}-?)?\d[\w.-]*|[A-Z]{2,4}")  # 304, 6061-T6, A36, C110, CRS
# A source that marks any statement as wrong or replaced: nothing in it is trusted (D36-12).
# A whole count: no sign before it, and not part of a decimal or a code ("5052-H32", "3mm").
_COUNT_TOKEN = re.compile(
    rf"(?i)(?<![\w.,+±-])(\d{{1,3}}(?:,\d{{3}})+|\d+)(?![.,]?\d)(?!-?[^\W\d])(?!-\d\d-\d\d)|\b({'|'.join(_WORDS)})\b"
)
_GRADE_LEAD = re.compile(rf"(?:{_MATERIAL_TERM.pattern.removeprefix('(?i)')}|\bgrade|\balloy|\btype)\s*$", re.I)  # fmt: skip
_STANDARD = re.compile(r"(?i)\b(?:ISO|DIN|ASME|ANSI|JIS|BS|EN)\b")  # a named tolerance standard
_NOT_A_VALUE = ("description", "operations", "notes", "general_notes")
_CORRECTION = re.compile(
    r"(?i)\b(?:wrong|incorrect|mistaken?|typo|correction|corrected|revis\w*|supersed\w*"
    r"|amend\w*|disregard\w*|cancel\w*|obsolete|outdated)\b|\bscratch\s+that\b"
)
_INJECTION = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:instructions?|rules|prompt)\b"
    r"|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\bnew\s+instructions\b|\bact\s+as\b"
    r"|\b(?:call|invoke|run|use)\s+(?:the\s+)?[\w-]+\s+tool\b|\bcustomer_id\b|\bapprove\b"
)
_LENGTH_SCALE = TypeAdapter(Annotated[Decimal, Field(max_digits=12, decimal_places=3)])


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _pattern(quote: str) -> re.Pattern:
    # Case-insensitive, whitespace-tolerant, whole words at both ends ("2 mm" never matches
    # inside "12 mm"); spans still point into the original text.
    body = r"\s+".join(map(re.escape, quote.split()))
    return re.compile(rf"(?<![\w.]){body}(?![\w])" if quote.strip() else r"(?!)", re.IGNORECASE)


def _identifier(label: str) -> bool:
    shaped = _KEYWORD_ID.fullmatch(label.strip()) or _CODE_ID.fullmatch(label.strip())
    return bool(shaped) and re.search(r"[^\W\d_]", label) is not None


def _label_spans(label: str | None, text: str) -> list[tuple[int, int]]:
    """Source occurrences of an identifier-shaped label (D36-06); [] for anything else."""
    if label is None or not _identifier(label):
        return []
    return [m.span() for m in _pattern(label).finditer(text)]


def _several_parts_named(text: str) -> bool:
    """The source itself names more than one part (Phase 5 multi-item rule, plus "Part X")."""
    codes = {c.casefold() for c in _CODE_LABEL.findall(text)}  # "BRK-200:", "BRK-201:" (D36-13)
    if (
        len(set(ITEM_NUMBER.findall(text))) > 1
        or len(set(_PART_MARKER.findall(text))) > 1
        or len(codes) > 1
    ):
        return True
    kinds = {re.sub(r"[\s_]+", " ", k.lower()).rstrip("s").split()[-1] for k in PART_KIND.findall(text)}  # fmt: skip
    return len(kinds) > 1


def _segments(text: str, parts: list[PartProposal]):
    """Per-part [start, end) source ranges, or None when boundaries cannot be established."""
    if not parts:
        return "none", None
    if len(parts) == 1:
        if _several_parts_named(text):  # one entry for a multi-part source (D36-05)
            return "unverified", None
        return "single_part", [(0, len(text))]
    labels = [p.label for p in parts]
    if None in labels or len({_norm(x) for x in labels}) != len(labels):
        return "unverified", None
    # A boundary needs identifier-shaped labels found in the source. A label may repeat inside
    # its own section ("Part B: 3 mm ... Correction: Part B must be 4 mm"), but another part's
    # label between two of its occurrences ("Part B: same as Part A") makes the split unsafe.
    found = sorted((a, i) for i, label in enumerate(labels) for a, _ in _label_spans(label, text))
    owners = [i for _, i in found]
    runs = [i for j, i in enumerate(owners) if j == 0 or owners[j - 1] != i]
    if set(runs) != set(range(len(labels))) or len(runs) != len(labels):
        return "unverified", None
    firsts = {i: a for a, i in reversed(found)}
    order = sorted(firsts, key=firsts.get)
    ends = {i: firsts[n] for i, n in zip(order, order[1:])} | {order[-1]: len(text)}
    return "labelled", [(firsts[i], ends[i]) for i in range(len(labels))]


def _count(value: str, minimum: int):
    value = value.translate(_MINUS)
    if any(c.isdigit() and not c.isascii() for c in value) or _BAD_NUMBER.search(value):
        return None, "unsupported_format", None
    tokens = re.findall(r"-?\d[\d,.]*\d|-?\d|[a-z]+", value.casefold())
    numbers = [t for t in tokens if t in _WORDS or t[-1].isdigit()]
    if len(numbers) != 1:
        return None, "multiple_values" if numbers else "unsupported_format", None
    token = numbers[0]
    if token in _WORDS:
        number, rule = _WORDS[token], "number_word"
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+|\d+", token):
        number, rule = int(token.replace(",", "")), "integer"
    elif token.startswith("-"):
        return None, "out_of_range", None
    else:
        return None, "unsupported_format", None  # decimals, "1,00", ...
    if not minimum <= number <= INT32_MAX:
        return None, "out_of_range", None
    return number, None, rule


def _length(field: str, value: str):
    text = value.translate(_MINUS).casefold()
    if field == "tolerance":
        text = _TOLERANCE_PREFIX.sub("", text)
    if any(c.isdigit() and not c.isascii() for c in text) or _BAD_NUMBER.search(text):
        return None, "unsupported_format", None
    if len(_NUMBER.findall(text)) > 1:
        return None, "multiple_values", None
    match = _MEASURE.search(text)
    if match is None:
        return None, "unsupported_format", None
    sign, digits, unit, other = match.groups()
    if other or not re.fullmatch(r"\d+(?:\.\d+)?", digits):
        return None, "unsupported_format", None  # gauge, metres, "0,5", "1,200"
    if unit is None:
        return None, "unit_not_stated", None
    number = Decimal(digits)
    if sign or (number == 0 and field != "tolerance"):
        return None, "out_of_range", None
    mm = number * _UNIT_MM[unit]
    try:
        _LENGTH_SCALE.validate_python(mm)
    except ValidationError:
        return None, "precision_exceeds_contract", None
    rule = "mm" if _UNIT_MM[unit] == 1 else f"{unit} x {_UNIT_MM[unit]} -> mm"
    return mm, None, rule


def _delivery(value: str):
    text = value.strip()
    candidates = [(text, "%d %B %Y"), (text, "%B %d, %Y"), (text, "%B %d %Y")]
    iso = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if len(iso) == 1:
        candidates.append((iso[0], "%Y-%m-%d"))
    for candidate, fmt in candidates:
        try:
            return datetime.strptime(candidate, fmt).date(), None, f"date {fmt}"
        except ValueError:
            pass
    return None, "unsupported_format", None


def _occurrences(field: str, value: str, text: str, span: tuple[int, int], numeric: bool):
    """Where the proposed value occurs inside the evidence span (numbers as whole numbers,
    never one preceded by a minus sign; lengths with the same unit factor)."""
    start, end = span
    signed = text.translate(_MINUS)
    found = []
    if field in _COUNTS:
        target = _count(value, 0)[0]
        # Searched in the full string, so the lookbehind sees a sign just before the span.
        for m in _COUNT_TOKEN.finditer(signed, start, end):
            number = int(m.group(1).replace(",", "")) if m.group(1) else _WORDS[m.group(2).lower()]
            if number == target:
                found.append(m.span())
    elif numeric:
        own = _MEASURE.search(_TOLERANCE_PREFIX.sub("", value.translate(_MINUS)))
        for m in _MEASURE.finditer(signed, start, end):
            # The sign is read from the source just before the digits, even outside the quote
            # (D36-09): "-3", "\u22123", "+3" never verify "3"; "±"/"+/-"/"+-" only a tolerance.
            head = signed[: m.start(2)]
            plus_minus = head.endswith(("+/-", "+-", "±"))
            signed_value = plus_minus or head.endswith(("-", "+"))
            if (
                (signed_value and not (plus_minus and field == "tolerance"))
                or m.group(4)
                or not m.group(3)
                or not own
                or not own.group(3)
            ):
                continue
            same_unit = _UNIT_MM[m.group(3).lower()] == _UNIT_MM[own.group(3).lower()]
            if same_unit and m.group(2) == own.group(2):
                found.append(m.span())
    else:
        found = [m.span() for m in _pattern(value).finditer(text, start, end)]
    return found


def _clause(text: str, at: tuple[int, int]) -> tuple[int, int]:
    begin = max((m.end() for m in _CLAUSE_END.finditer(text, 0, at[0])), default=0)
    after = _CLAUSE_END.search(text, at[1])
    return begin, after.start() if after else len(text)


def _context_problem(field: str, text: str, at: tuple[int, int]) -> Reason | None:
    """Why the clause around one value occurrence does not state this field as a firm,
    single request (D36-01 attribution, D36-02 alternatives, D36-03/07 negation)."""
    begin, end = _clause(text, at)
    rest = f"{text[begin : at[0]]} {text[at[1] : end]}"
    clause = text[begin:end]
    if field not in _FREE_TEXT and _NEGATION.search(_NOT_LATER.sub(" by ", rest)):
        return "negated_or_conditional"
    plain = _ISO_DATE.sub(" ", re.sub(r"(?i)\bp/n\b|\+/-", " ", clause.translate(_MINUS)))
    if field not in _FREE_TEXT and (_CHOICE_WORDS.search(plain) or _RANGE.search(plain)):
        return "alternatives"
    return _attribution_problem(field, text, at)


def _attribution_problem(field: str, text: str, at: tuple[int, int]) -> Reason | None:
    """Whether the clause around ``at`` states this field at all (D36-01/10, D36-07 cues)."""
    begin, end = _clause(text, at)
    clause = text[begin:end]
    cue = _CUES.get(field)
    if field in _DIMENSIONS:
        others = [_CUES[f] for f in (*_DIMENSIONS, "tolerance", "hole_count") if f != field]
        measures = [m for m in _MEASURE.finditer(clause.translate(_MINUS)) if m.group(3) or m.group(4)]  # fmt: skip
        product = re.search(r"\d\s*[x×*]\s*\d", clause, re.IGNORECASE)  # "80 x 40 x 3 mm"
        if not cue.search(clause) or any(o.search(clause) for o in others) or len(measures) != 1 or product:  # fmt: skip
            return "field_not_attributable"
    elif field in ("tolerance", "hole_count", "requested_delivery_date"):
        if not cue.search(clause):
            return "field_not_attributable"
    elif field in ("material", "material_grade", "finish"):
        said = text[at[0] : at[1]]
        if field == "material":
            base = re.sub(r"(?i)galvani[sz]ed\s+steel", "steel", said)  # a catalog category
            ok = _MATERIAL_TERM.search(base) and not _FINISH_TERM.search(base)
        elif field == "finish":
            ok = _FINISH_TERM.search(said) and not _MATERIAL_TERM.search(said)
        else:
            measure = _MEASURE.fullmatch(said.strip())  # "3 mm" is a size, "304" a grade
            ok = _GRADE.fullmatch(said.strip()) and not (
                measure and (measure.group(3) or measure.group(4))
            )
        if not ok:
            return "field_not_attributable"  # "black powder coating" is not a material
    elif field == "quantity":
        if _GRADE_LEAD.search(text[begin : at[0]]):
            return "field_not_attributable"  # "steel 1018", "grade 304" are grades
        after = text[at[1] : end]
        measure = re.match(rf"(?i)\s*(?:{_UNITS}|{_OTHER_UNITS})(?![a-z])", after)
        counted = _QUANTITY_CUE.search(clause) or re.match(rf"\s*(?:[\w-]+\s+){{0,3}}?{_PART_NOUN}", after)  # fmt: skip
        if _CUES["hole_count"].search(clause) or measure or not counted:
            return "field_not_attributable"  # "2 bends", "3 mm", "4 holes" are not quantities
    return None


def _check(field: str, item: Evidenced, text: str, segment: tuple[int, int] | None):
    value = str(item.value)
    found = _pattern(item.quote).search(text)
    if found is not None and segment is not None:
        found = _pattern(item.quote).search(text, *segment)
        if found is None:
            return FieldEvidence(field=field, value=value, evidence=item.quote, span=None, status="unverified", reason="evidence_outside_part")  # fmt: skip
    if found is None:
        return FieldEvidence(field=field, value=value, evidence=item.quote, span=None, status="unverified", reason="evidence_not_in_source")  # fmt: skip
    base = {"field": field, "value": value, "evidence": found.group(), "span": found.span()}
    if field in _CHOICE and (_ALTERNATIVE.search(value) or _ALTERNATIVE.search(found.group())):
        return FieldEvidence(**base, status="ambiguous", reason="alternatives")
    normalized, reason, rule = value.strip(), None, "verbatim"
    if field in _COUNTS:
        normalized, reason, rule = _count(value, _COUNTS[field])
    elif field in _LENGTHS:
        normalized, reason, rule = _length(field, value)
        if field == "tolerance" and reason and _STANDARD.search(value):
            normalized, reason, rule = value.strip(), None, "verbatim"  # e.g. "ISO 2768-m"
    elif field == "requested_delivery_date":
        normalized, reason, rule = _delivery(value)
    elif field == "finish" and _norm(value) in _NO_FINISH:
        normalized, rule = "none", "explicit no-finish -> none"
    if reason is not None:  # never interpreted, whatever the evidence says
        status = "ambiguous" if reason in ("multiple_values", "unit_not_stated") else "invalid"
        return FieldEvidence(**base, status=status, reason=reason)
    numeric = field in _LENGTHS and rule != "verbatim"
    places = _occurrences(field, value, text, found.span(), numeric)
    if not places:
        return FieldEvidence(**base, status="unverified", reason="value_not_in_evidence")
    problems = [_context_problem(field, text, at) for at in places]
    if None not in problems:  # no occurrence states the field as a firm, single request
        return FieldEvidence(**base, status="ambiguous", reason=problems[0])
    return FieldEvidence(**base, status="verified", normalized=normalized, transformation=rule)


def _stated(item: Evidenced | None) -> bool:
    # A "null"/"null" pair is a model placeholder for "not stated" (as ``llm._EMPTY``).
    return item is not None and not (_norm(str(item.value)) == _norm(item.quote) == "null")


def _items(owner: BaseModel, names: tuple[str, ...]):
    for name in names:
        entry = getattr(owner, name)
        for item in entry if isinstance(entry, list) else [entry]:
            if _stated(item):
                yield name, item


def _rejoin(value: str, source: str) -> str:
    """Undo a model's split digits ("30 0 mm", "BRK- 201") only when the original text is not
    in ``source`` and the rejoined text is: the result is the customer's own wording."""
    joined = re.sub(r"(?<=[\d-])\s+(?=\d)", "", value)
    if joined != value and _norm(value) not in _norm(source) and _norm(joined) in _norm(source):
        return joined
    return value


def _rejoined(part: PartProposal, text: str) -> PartProposal:
    update = {}
    if part.label is not None:
        update["label"] = _rejoin(part.label, text)
    for name in (*SCALAR_FIELDS, "operations", "notes"):
        entry = getattr(part, name)
        items = entry if isinstance(entry, list) else [entry] if entry is not None else []
        fixed = [
            i.model_copy(update={"value": _rejoin(i.value, i.quote)}) if isinstance(i.value, str) else i
            for i in items
        ]  # fmt: skip
        update[name] = fixed if isinstance(entry, list) else (fixed[0] if fixed else None)
    return part.model_copy(update=update)


def _label_conflict(label: str | None, checked: list[list[FieldEvidence]]) -> bool:
    """The label is (part of) a verified structured value: "200" in "200 aluminum brackets",
    or a grade "6061-T6" proposed as a label. Free-text fields are not values."""
    if label is None:
        return False
    key = _norm(label)

    def within(a: str, b: str) -> bool:
        return re.search(rf"(?<![\w./-]){re.escape(a)}(?![\w./-])", b) is not None  # "BRK-200"

    return any(
        f.status == "verified" and f.field not in _NOT_A_VALUE
        and (within(key, _norm(f.value)) or within(_norm(f.value), key))
        for fields in checked for f in fields
    )  # fmt: skip


def _downgrade(f: FieldEvidence, reason: Reason) -> FieldEvidence:
    if f.status != "verified":
        return f  # already untrusted for a more specific reason
    update = {"status": "ambiguous", "reason": reason, "normalized": None, "transformation": None}
    return f.model_copy(update=update)


def _check_parts(proposal: ExtractionProposal, text: str, segments) -> list[list[FieldEvidence]]:
    names = (*SCALAR_FIELDS, "operations", "notes")
    checked = [
        [
            _check(n, item, text, segments[i] if segments else None)
            for n, item in _items(part, names)
        ]
        for i, part in enumerate(proposal.parts)
    ]
    for fields in checked:
        # One stated value under two fields of a part ("3 mm thick" as thickness and height,
        # or as finish) is a field-assignment guess: trust neither.
        seen: dict[str, int] = {}
        for f in fields:
            if f.field in SCALAR_FIELDS:
                seen[_norm(f.value)] = seen.get(_norm(f.value), 0) + 1
        for j, f in enumerate(fields):
            if f.field in SCALAR_FIELDS and seen[_norm(f.value)] > 1:
                fields[j] = _downgrade(f, "value_reused")
    if segments is None and len(proposal.parts) > 1:
        # Without established boundaries, one source span cited by several parts for the same
        # field is a shared or leaked value: never trust it for any of them.
        owners: dict[tuple[str, tuple[int, int]], set[int]] = {}
        for i, fields in enumerate(checked):
            for f in fields:
                if f.span:
                    owners.setdefault((f.field, f.span), set()).add(i)
        for fields in checked:
            for j, f in enumerate(fields):
                if f.span and len(owners[(f.field, f.span)]) > 1:
                    fields[j] = _downgrade(f, "shared_evidence")
    return checked


def verify_extraction(
    proposal: ExtractionProposal, text: str, source_id: str, rfq_id: str | None = None
) -> IntakeResult:
    """Deterministically check every proposed value against the caller's source text."""
    # An entry with no label and nothing stated carries no information; small local models
    # emit them spuriously. Dropping one cannot move a value or merge parts. It is counted.
    names = (*SCALAR_FIELDS, "operations", "notes")
    kept = [_rejoined(p, text) for p in proposal.parts if p.label is not None or next(_items(p, names), None)]  # fmt: skip
    discarded = len(proposal.parts) - len(kept)
    proposal = proposal.model_copy(update={"parts": kept})
    boundaries, segments = _segments(text, proposal.parts)
    checked = _check_parts(proposal, text, segments)
    conflicts = {_norm(p.label) for p in proposal.parts if _label_conflict(p.label, checked)}
    if boundaries == "labelled" and (not all(checked) or conflicts):
        # A label that is really a value, or a part with nothing stated, means the split
        # itself is unreliable: fall back to the unverified-boundary rules.
        boundaries, segments = "unverified", None
        checked = _check_parts(proposal, text, None)
    if boundaries == "unverified":
        # No established part boundaries: evidence and parsed values are kept for review,
        # but no value is trusted as belonging to a particular part (D36-05).
        checked = [[_downgrade(f, "part_association_unverified") for f in fs] for fs in checked]
    if _CORRECTION.search(text):
        # The customer marked something as wrong or replaced; which value survives is a
        # Day 37 contradiction decision, so no structured value is trusted meanwhile.
        checked = [[f if f.field in _FREE_TEXT else _downgrade(f, "superseded_or_contradicted") for f in fs] for fs in checked]  # fmt: skip
    claimed = [_norm(p.label) for p in proposal.parts if p.label]

    def label_ok(label: str | None) -> bool:
        if not _label_spans(label, text) or claimed.count(_norm(label)) > 1:  # several entries
            return False
        return _norm(label) not in conflicts

    parts = tuple(
        IntakePart(
            index=i + 1,
            label=part.label,
            label_verified=label_ok(part.label),
            fields=tuple(fields),
            missing=tuple(n for n in SCALAR_FIELDS if not _stated(getattr(part, n))),
            scope=segments[i] if segments else None,
        )
        for i, (part, fields) in enumerate(zip(proposal.parts, checked, strict=True))
    )
    rfq_fields = tuple(
        _check(n, item, text, None)
        for n, item in _items(proposal, ("requested_delivery_date", "general_notes"))
    )
    if _CORRECTION.search(text):
        rfq_fields = tuple(f if f.field in _FREE_TEXT else _downgrade(f, "superseded_or_contradicted") for f in rfq_fields)  # fmt: skip
    clean = (
        boundaries != "unverified"
        and all(p.label is None or p.label_verified for p in parts)
        and all(p.fields for p in parts)
        and all(
            f.status == "verified" for f in (*rfq_fields, *(f for p in parts for f in p.fields))
        )
    )
    if not parts:
        status = "no_parts_found"
    else:
        status = "extracted" if clean else "extracted_with_issues"
    return IntakeResult(
        status=status,
        source_id=source_id,
        rfq_id=rfq_id,
        part_boundaries=boundaries,
        injection_suspected=_INJECTION.search(text) is not None,
        parts=parts,
        rfq_fields=rfq_fields,
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        discarded_empty_entries=discarded,
    )


def candidate_specification(
    result: IntakeResult, part: IntakePart | int
) -> PartSpecification | None:
    """Verified numeric fields as the downstream contract, when the part is identifiable.

    Material and finish catalog ids, part type and completeness are decided later (Day 37 and
    the Engineering Agent); only an explicit no-finish request maps to ``finish_id="none"``."""
    if not isinstance(result, IntakeResult):
        return None
    if isinstance(part, int) and not isinstance(part, bool):  # 1-based IntakePart.index
        part = next((p for p in result.parts if p.index == part), None)
    if not any(p is part for p in result.parts):  # identity: a foreign part is never used
        return None
    if result.status not in ("extracted", "extracted_with_issues"):
        return None
    if result.part_boundaries == "unverified" or not part.label_verified:
        return None
    mapping = {
        "quantity": "quantity", "hole_count": "hole_count", "thickness": "thickness_mm",
        "width": "width_mm", "length": "length_mm", "height": "height_mm",
        "tolerance": "tolerance_mm", "finish": "finish_id",
    }  # fmt: skip
    data = {}
    for f in part.fields:
        if f.status != "verified" or f.field not in mapping:
            continue
        if f.field == "finish" and f.normalized != "none":
            continue
        if f.field == "tolerance" and not isinstance(f.normalized, Decimal):
            continue
        data[mapping[f.field]] = f.normalized
    if not data:
        return None
    try:
        return PartSpecification(part_id=part.label, **data)
    except ValidationError:
        return None


# --- bounded graph -----------------------------------------------------------------------


class IntakeState(TypedDict, total=False):
    text: str  # untrusted customer text, credential-checked before the graph runs
    source_id: str  # trusted, caller-assigned
    rfq_id: str | None  # trusted, caller-assigned
    messages: list[BaseMessage]
    proposal: ExtractionProposal | None
    result: IntakeResult | None
    error: ErrorCode | None


def build_intake_graph(model: BaseChatModel) -> CompiledStateGraph:
    """Compile the Intake Agent for an injected chat model. No tools, no checkpointer."""

    async def prepare_input(state: IntakeState) -> IntakeState:
        try:
            return {"messages": llm.build_messages(INSTRUCTIONS, {"customer_rfq": state["text"]})}
        except llm.LlmError as error:
            return {"error": error.code}

    async def extract(state: IntakeState) -> IntakeState:
        try:
            proposal = await llm.invoke_structured(model, state["messages"], ExtractionProposal)
            llm.reject_credentials(proposal.model_dump_json())
        except llm.LlmError as error:
            return {"error": error.code}
        return {"proposal": proposal}

    async def verify(state: IntakeState) -> IntakeState:
        result = verify_extraction(
            state["proposal"], state["text"], state["source_id"], state.get("rfq_id")
        )
        return {"result": result}

    async def safe_error(state: IntakeState) -> IntakeState:
        return {"text": "", "messages": [], "proposal": None, "result": None}

    def on_error(next_node: str):
        return lambda state: "safe_error" if state.get("error") else next_node

    graph = StateGraph(IntakeState)
    graph.add_node("prepare_input", prepare_input)
    graph.add_node("extract", extract)
    graph.add_node("verify", verify)
    graph.add_node("safe_error", safe_error)
    graph.add_edge(START, "prepare_input")
    graph.add_conditional_edges("prepare_input", on_error("extract"), ["extract", "safe_error"])
    graph.add_conditional_edges("extract", on_error("verify"), ["verify", "safe_error"])
    graph.add_edge("verify", END)
    graph.add_edge("safe_error", END)
    return graph.compile()


_IDENTIFIER = TypeAdapter(Identifier)


async def run_intake(
    graph: CompiledStateGraph,
    text: str,
    *,
    source_id: str,
    rfq_id: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> IntakeResult:
    """One bounded extraction of caller-supplied customer text: one model call, no retries.

    ``source_id`` and ``rfq_id`` are trusted values from the authorized caller (a bad one is a
    caller bug and raises ``ValueError``); the customer text is untrusted."""
    try:
        _IDENTIFIER.validate_python(source_id)
        if rfq_id is not None:
            _IDENTIFIER.validate_python(rfq_id)
    except ValidationError:
        raise ValueError("source_id and rfq_id must be identifiers") from None

    def failed(code: ErrorCode) -> IntakeResult:
        return IntakeResult(status="error", source_id=source_id, rfq_id=rfq_id, error=code)

    if (
        not isinstance(text, str)
        or not text.strip()
        or "\x00" in text
        or len(text) > MAX_RAW_REQUEST_CHARS
    ):
        return failed("invalid_input")
    try:
        llm.reject_credentials(text)  # before the text reaches any graph state
    except llm.LlmError as error:
        return failed(error.code)
    try:
        async with asyncio.timeout(timeout_seconds):
            state = await graph.ainvoke(
                {"text": text, "source_id": source_id, "rfq_id": rfq_id, "error": None},
                {"recursion_limit": RECURSION_LIMIT},
            )
    except (GraphRecursionError, TimeoutError):
        return failed("execution_limit")
    except Exception:  # an unexpected fault: its text never surfaces
        return failed("internal_error")
    if state.get("error") or state.get("result") is None:
        return failed(state.get("error") or "internal_error")
    return state["result"]
