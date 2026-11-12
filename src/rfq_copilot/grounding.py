"""Deterministic answers and argument provenance for the agent (Day 35, Phase 5 final).

Pure functions, no model, no I/O.

Answers. Model text is never shown as an answer and never decides an RFQ fact:
- No tool result this turn: the reply is the fixed ``GUIDANCE`` text (grounding ``guidance``).
  There is no claim detection to bypass, because model prose is not shown at all.
- Tool results this turn: the reply is ``render(results)``, built only from the validated
  current-turn MCP results (grounding ``rendered``). Statuses, findings and nested fields are
  printed exactly as returned; money fields get display rounding only (``MoneyDisplay``).
History is never evidence: earlier replies are not results of this turn.

Argument provenance. Values the model puts into tool arguments are checked against what the
user explicitly wrote (``graph._inspect``):
- ``labelled_dates``: a planning horizon or delivery date counts as user input only in an
  explicit, labelled form ("planning start 2026-11-16", "requested delivery date
  2026-12-22"). Any other ISO date (a meeting, a submission) is incidental and never used.
- ``single_item_finish_none``: ``finish_id`` may be set to ``none`` only for a request that
  names exactly one item and exactly one finish, and that finish is none / unfinished.
"""

import decimal
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

Grounding = Literal["none", "guidance", "rendered"]

GUIDANCE = (
    "I can only state RFQ facts that come from verified system data, and no verified data "
    "was retrieved for this message. Ask about a material, a part's manufacturability, "
    "inventory, capacity, lead time, cost or price, and include the part details (material, "
    "dimensions, thickness, finish, quantity) or a stored RFQ id."
)

_ISO = r"(\d{4}-\d{2}-\d{2})"
_SEP = r"\s*(?:is|:|=|of|on)?\s*"
HORIZON_LABEL = re.compile(
    rf"(?i)\b(?:planning\s+(?:start|horizon)|horizon(?:[\s_]+start)?|plan(?:ning)?\s+from"
    rf"|schedule\s+from)(?:\s+date)?{_SEP}{_ISO}"
)
DELIVERY_LABEL = re.compile(
    rf"(?i)\b(?:(?:requested\s+)?delivery\s+date|deliver(?:ed|y)?\s+by|due\s+date){_SEP}{_ISO}"
)
# A finish followed by an alternative ("none or brushed") is not a stated finish.
_FINISH = re.compile(r"(?i)\bfinish(?:_id)?\b(?:\s*[:=]\s*|\s+)([a-z_]+)\b(?!\s*(?:or|and|/)(?:\b|\s))|\bno finish\b|\bunfinished\b")  # fmt: skip
_ITEM = re.compile(r"(?i)\b(?:item|line|position|pos\.?)\s*#?\s*(\d+)\b")
# Part kinds (contract PartType values and their plain words): two kinds named = two parts.
_PART_KIND = re.compile(r"(?i)\b(?:flat[\s_]panel|panel|mounting[\s_]plate|plate|l[\s_]bracket|u[\s_]bracket|bracket|enclosure|fabricated[\s_]assembly|assembly)s?\b")  # fmt: skip


def labelled_dates(question: str, label: re.Pattern) -> set[date]:
    """Distinct ISO dates the user wrote in an explicit labelled form (invalid dates ignored)."""
    found = set()
    for match in label.finditer(question):
        try:
            found.add(date.fromisoformat(match.group(1)))
        except ValueError:
            continue
    return found


def single_item_finish_none(question: str) -> bool:
    """True only for a single-item request in which every stated finish is none / unfinished.

    Multi-item requests (several item/line numbers, or several part kinds) never qualify, so
    a finish is never carried from one part to another."""
    items = set(_ITEM.findall(question))
    if len(items) > 1:
        return False  # multi-item: never propagate a finish between items
    kinds = {re.sub(r"[\s_]+", " ", k.lower()).rstrip("s") for k in _PART_KIND.findall(question)}
    if not items and len({k.split()[-1] for k in kinds}) > 1:
        return False  # no item numbers and two part kinds ("panels ... and brackets")
    named = [(m.group(1) or "none").lower() for m in _FINISH.finditer(question)]
    return bool(named) and all(n in ("none", "unfinished") for n in named)


@dataclass(frozen=True)
class MoneyDisplay:
    """Display rounding for money fields only; exact results are never changed.

    ``from_pricing_rule`` reuses the pinned pricing rule's own scales and rounding mode
    (``services.costing.rounding``). The default is the documented display-only fallback:
    two decimal places, ROUND_HALF_UP, for every money field."""

    unit_scale: Decimal = Decimal("0.01")
    line_scale: Decimal = Decimal("0.01")
    rounding: str = decimal.ROUND_HALF_UP

    @classmethod
    def from_pricing_rule(cls, pricing_rule) -> "MoneyDisplay":
        from rfq_copilot.services.costing import rounding

        unit, line, mode = rounding(pricing_rule)
        return cls(unit, line, mode)

    def format(self, path: str, value: Any) -> str | None:
        """The display text if ``path`` names a USD money field, else ``None``."""
        key = path.rsplit(".", 1)[-1]
        if key in _UNIT_MONEY or (key == "total_manufacturing_cost" and "lines[" in path):
            scale = self.unit_scale  # per-unit values, as services.costing.display_costs
        elif key in _LINE_MONEY:
            scale = self.line_scale
        else:
            return None
        with decimal.localcontext() as ctx:
            ctx.prec = 50
            return f"{Decimal(str(value)).quantize(scale, rounding=self.rounding)} USD"


# Explicit USD fields of the MCP output contracts (not a name heuristic). gross_margin is a
# fraction and stays exact; quantities, dimensions, density and days are never money.
_UNIT_MONEY = frozenset(
    {
        "material_cost_per_unit",
        "labour_cost_per_unit",
        "finish_cost_per_unit",
        "packaging_cost_per_unit",
        "variable_cost_per_unit",
        "quoted_unit_price_usd",
    }
)
_LINE_MONEY = frozenset(
    {"setup_cost_per_line", "line_total_usd", "quote_total_usd", "total_manufacturing_cost"}
)
_MAX_ITEMS = 10


def _flatten(prefix: str, value: Any, out: list[str], money: MoneyDisplay) -> None:
    if value is None or prefix.endswith("fingerprint"):
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(f"{prefix}.{key}" if prefix else key, inner, out, money)
    elif isinstance(value, list):
        if all(isinstance(v, (str, int, bool)) for v in value):
            out.append(f"{prefix}=" + ", ".join(map(str, value)))
        else:
            for i, inner in enumerate(value[:_MAX_ITEMS]):
                _flatten(f"{prefix}[{i}]", inner, out, money)
            if len(value) > _MAX_ITEMS:
                out.append(f"{prefix}=... {len(value) - _MAX_ITEMS} more")
    elif isinstance(value, bool):
        out.append(f"{prefix}={str(value).lower()}")
    else:
        shown = money.format(prefix, value) if isinstance(value, (str, int)) else None
        out.append(f"{prefix}={value if shown is None else shown}")


def render(
    results: Iterable[tuple[str, Mapping[str, Any]]], money: MoneyDisplay = MoneyDisplay()
) -> str:
    """The customer-visible answer: validated results exactly as returned, money rounded
    for display only."""
    lines = ["Verified tool data:"]
    for name, data in results:
        fields: list[str] = []
        _flatten("", dict(data), fields, money)
        lines.append(f"- {name}: " + "; ".join(fields))
    return "\n".join(lines)
