# RFQ Intake Agent (A1): extraction (Day 36) and validation (Day 37)

`src/rfq_copilot/intake.py` turns customer-authored RFQ text into a **provisional,
evidence-checked** per-part extraction. It does not validate for manufacturing, check
feasibility, map catalog ids, price or approve anything. Every result has
`readiness="pending_validation"`. Day 37 adds `src/rfq_copilot/intake_validation.py`
(completeness, contradictions, catalog recognition, clarification, downstream readiness);
see "Day 37: validation and clarification" below.

## Interface (for the future Supervisor)

```python
graph = intake.build_intake_graph(model)            # injected BaseChatModel, no tools
result = await intake.run_intake(graph, text, source_id="email-1", rfq_id="RFQ-0001")
spec = intake.candidate_specification(result, result.parts[0])  # PartSpecification | None
```

- `text`: customer text, supplied by an authorized caller. It is untrusted, 1 to 20,000
  characters (`MAX_RAW_REQUEST_CHARS`).
- `source_id`, `rfq_id`: trusted values from the caller. Never taken from the model. A bad
  value raises `ValueError`, because it is a caller bug.
- `IntakeResult` is a frozen Pydantic model, so it round-trips through JSON. Its fields:
  - `status`: `extracted`, `extracted_with_issues`, `no_parts_found` or `error`
  - `error`: a fixed code
  - `part_boundaries`: `single_part`, `labelled`, `unverified` or `none`
  - `injection_suspected`
  - `parts`: tuple of `IntakePart`
  - `rfq_fields`: delivery date and general notes
- `IntakePart` has:
  - `index`: 1-based, ours, never a customer part number
  - `label`: the customer's name for the part
  - `label_verified`
  - `fields`: tuple of `FieldEvidence`
  - `missing`: scalar fields not stated
- `FieldEvidence` has:
  - `field`
  - `value`: as the model proposed it
  - `evidence`: the source text at `span`
  - `span`: `[start, end)` offsets into `text`
  - `status`: `verified`, `ambiguous`, `unverified` or `invalid`
  - `reason`: a fixed code
  - `normalized`: set only when the status is `verified`
  - `transformation`: the deterministic rule applied

The Supervisor can call `run_intake` directly, or add the compiled graph as a subgraph. No
state is shared between runs.

## Graph

```
START -> prepare_input -> extract -> verify -> END
   (prepare_input | extract error) -> safe_error -> END
```

- `prepare_input`: `llm.build_messages(INSTRUCTIONS, {"customer_rfq": text})`.
  Instructions are written in code. Customer text goes in an `<untrusted_data>` block.
  Credential-looking text is refused.
- `extract`: makes exactly one `llm.invoke_structured(model, messages,
  ExtractionProposal)` call, then screens the proposal for credentials.
- `verify`: `verify_extraction`, pure deterministic code.
- `safe_error`: clears the text, messages and proposal.

The graph is acyclic, with no tools and no checkpointer. `RECURSION_LIMIT = 6`, and
`run_intake` applies a timeout (120 s by default). There are no retries. Cancellation
propagates.

## Proposal schema (what the model fills)

`ExtractionProposal`:
- `parts[]` (at most `MAX_PARTS` = 50)
- `requested_delivery_date`
- `general_notes[]`

`PartProposal`:
- `label`
- `description`, `quantity`, `material`, `material_grade`
- `thickness`, `width`, `length`, `height`
- `hole_count`, `finish`, `tolerance`
- `operations[]`, `notes[]`

Every value is an `Evidenced`: `{value, quote}`. `value` is a string or an integer, never
a float. All models use `extra="forbid"`, so a proposal fails as `malformed_output` if it:
- carries identity, customer, price, feasibility or source/page fields, or
- uses floats or has the wrong shape.

A `"null"`/`"null"` pair counts as not stated.

`invoke_structured` (shared, `llm.py`, D36-08) checks the raw response. With
`include_raw=True`, every LangChain chat model returns its `AIMessage` as `raw`. The
following are accepted:
- zero tool calls (JSON or json_schema modes, such as ChatOllama)
- exactly one call named after the schema (`title` or class name, as LangChain's
  `PydanticToolsParser`)

The following are refused:
- a missing or non-`AIMessage` raw: `malformed_output`
- invalid tool calls, non-list call fields, several calls, or any other tool name:
  `invalid_tool_call`

## Verification rules (`verify_extraction`)

1. **Evidence in source.** The quote must occur in the text. Matching is case-insensitive,
   tolerates whitespace and needs whole words at both ends, so "2 mm" never matches inside
   "12 mm". The recorded `span` and `evidence` come from the original text. Failing this
   gives `unverified/evidence_not_in_source`.
2. **Evidence in the part's own segment.** With established boundaries, the quote must
   occur within the part's segment. Otherwise: `unverified/evidence_outside_part`. This
   blocks cross-part leaks, for example Part A's finish proposed for Part B.
3. **Value in evidence.** The value must occur inside the quoted span:
   - numbers as whole numbers, never one preceded by a minus or dash sign
   - lengths with the same unit factor
   - text as whole words

   Otherwise: `unverified/value_not_in_evidence`.
3a. **Clause context (remediation D36-01/02/03/07).** For each occurrence of the value, the
   clause around it is checked. Clauses split at `, ; ( ) [ ] ! ?`, a full stop, "and",
   "with", "plus", "but" and "while". At least one occurrence must pass all three checks
   below. Otherwise the field is `ambiguous`, with the first failing reason.
   - **Not negated or conditional** (`negated_or_conditional`). Examples: not, never,
     without, avoid, exclude, except, instead, unless, if, optional, maybe, possibly,
     prefer, n't, and "no" (but not "No." before a number). "No later than" counts as a
     deadline, not a negation. Free-text `notes` and `general_notes` are exempt, because a
     prohibition is their content.
   - **No alternatives or ranges in the source** (`alternatives`). Examples: "or", "either",
     "between", "alternatively", "word/word", and `N-N` or `N to N`. ISO dates, `p/n` and
     `+/-` are excluded first.
   - **Attributable to the field** (`field_not_attributable`):
     - **Dimensions.** The clause names this dimension (thick/thickness/t=, wide/width/w=,
       long/length/l=, high/height/tall/h=). It names no other dimension, tolerance or
       hole. It has exactly one measurement and no `N x N` product. No ordering convention
       is assumed: "120 mm x 80 mm" is ambiguous.
     - **Tolerance.** The clause names a tolerance (tolerance/tol, ±, +/-, +-).
     - **Hole count.** The clause says "hole(s)".
     - **Delivery date.** The clause has delivery wording (deliver, due, ship, by, before,
       arrive, later than).
     - **Quantity.** The number is not followed by a unit, the clause has no "hole", and
       either the clause has a quantity word (qty, quantity, pcs, pieces, units, off, ea,
       each, sets) or a part noun (the Phase 5 `grounding.PART_KIND` vocabulary) follows
       within three words.
4. **Exact parsing.** Counts must be whole integers. Lengths must be one exact decimal with
   an explicit unit. Unit conversion uses only the fixed table:
   - mm ×1
   - cm ×10
   - in or `"` ×25.4

   Every minus or dash variant is read as a minus sign: U+2212, the figure, en and em
   dashes, and the small and full-width hyphen-minus. A negative value is
   `invalid/out_of_range`. Non-ASCII digits and an explicit `+5` are
   `invalid/unsupported_format`. A tolerance prefix (`±`, `+/-`, `+-`) is removed before
   parsing.

   These are never interpreted:
   - no unit (`ambiguous/unit_not_stated`)
   - gauge, m, ft, mils, comma decimals (`invalid/unsupported_format`)
   - several numbers (`ambiguous/multiple_values`)
   - zero or negative (`invalid/out_of_range`)
   - more than 3 decimal places in mm (`invalid/precision_exceeds_contract`; never rounded)

   A tolerance designation such as "ISO 2768-m" is kept verbatim. Delivery dates parse
   only from ISO or month-name forms; "03/04/2026" is `invalid`.
5. **Undecided choices.** "or", "either" or "/" in a material, grade or finish gives
   `ambiguous/alternatives`.
6. **One value, two fields.** The same stated value under two fields of one part (for
   example "3 mm thick" as both thickness and height) gives `ambiguous/value_reused` for
   both.
7. **Shared evidence.** Without established boundaries, a source span cited for the same
   field by several parts gives `ambiguous/shared_evidence` for all of them.
8. **Explicit no-finish.** "unfinished", "no finish" and similar normalize to `"none"`,
   only when the customer stated it.

`normalized` is set only for `verified` fields.

### Part boundaries

| Value | When |
|---|---|
| `single_part` | one part, and the source does not itself name several parts (several item numbers, several "Part X" markers or several part kinds). The whole text is its segment |
| `labelled` | every part has an identifier-shaped label that occurs exactly once; labels differ; no label overlaps any field's evidence; every part states at least one field. Segments run from each label to the next |
| `unverified` | anything else: unnamed parts, duplicate or repeated labels, description labels, empty parts, or one entry for a multi-part source |
| `none` | no parts |

**Unverified boundaries (D36-05).** When boundaries are `unverified`, every field that would
have been verified becomes `ambiguous/part_association_unverified`. Its evidence, span and
value are kept for review; `normalized` is cleared. No value is trusted as belonging to a
particular part.

**Labels (D36-06).** Single-part and multi-part requests use the same checks. A label is
verified only if all of these hold:
- it is identifier-shaped: a keyword form ("Part A", "Item 2", "P/N 7731", "drawing …") or
  one code token with a digit ("BRK-200") or of at most two letters ("A")
- it occurs exactly once in the source
- it does not overlap any field's evidence (this catches a grade "6061-T6" proposed as a
  label)
- no other entry claims it

Descriptions such as "200 aluminum brackets" are never identifiers, so
`candidate_specification` cannot use them as `part_id`.

Text before the first label (RFQ-wide wording) is outside every segment. It is therefore
never applied to a part automatically.

`status` is `extracted` only when boundaries are not `unverified`, all labels are verified,
every part states a field and every field is `verified`. Otherwise it is
`extracted_with_issues`. Neither status means validated.

### Downstream contract

`candidate_specification` builds the unchanged `PartSpecification` only when all of these
hold:
- the part's label is verified
- boundaries are not `unverified`
- at least one verified field maps

Only verified quantity, hole count, dimensions, a numeric tolerance and an explicit
no-finish (`finish_id="none"`) are mapped. `material_id`, `finish_id` (other than
`"none"`) and `part_type` need catalog mapping, which comes later.

## Security boundaries

- Customer text is data. It cannot close its data block, and the instructions are rebuilt
  from code on every run.
- No MCP tool is offered, bound or called. No database access. No writes. Attachment
  retrieval waits for an authorized intake tool (none exists in `mcp/contracts.py`).
- Identity and scope come only from the caller (`source_id`, `rfq_id`). The proposal schema
  cannot carry them.
- Credentials:
  - refused in the input before any graph state (`unsafe_content`)
  - refused in the model output
  - never in state

  `IntakeState` holds the text, trusted ids, messages, proposal, result and error only.
- Failures return fixed codes:
  - `invalid_input`, `unsafe_content`, `unsupported`, `model_error`
  - `malformed_output`, `invalid_tool_call`, `execution_limit`, `internal_error`

  Provider exception text is never carried.
- `injection_suspected` flags instruction-like customer text for review. It does not
  change extraction. Injected values are still checked as data; a value the injection
  invents has no source evidence.

**What "verified" means:** the value is stated in the source, in that part's scope,
and parsed deterministically. It does not mean the customer's request is correct,
consistent, complete or manufacturable.

## Real-model observations (qwen3:4b-instruct-2507-q4_K_M, local Ollama)

- With optional fields, the model fills mainly quantity and thickness. It often adds a
  spurious second, empty part.
- With all fields required, it fills more but assigns values to the wrong fields (for
  example "3 mm thick" as height and as finish). This led to rule 6, and to keeping
  optional fields.
- Without a clear label rule, it used specification phrases as labels. This led to the
  overlap and empty-part boundary rules.
- In the remediation run, the model split one labelled part into two "Part A" entries.
  This led to the rule that a label claimed by several entries is not verified.
- Because of these rules, every observed bad output came back as `extracted_with_issues`
  or `unverified`, never as a trusted wrong value. Extraction recall with this 4B model is
  low; a stronger model is expected to improve recall without changing the verifier.

## Final remediation (D36-09 .. D36-13)

- **Signs outside the quote (D36-09).** The sign is read from the source just before the
  digits, even when it lies outside the quoted span. A positive proposal never verifies
  against a source that has "-3", "−3", "–3", "+3" or "±3". `±`, `+/-` and
  `+-` are accepted only for tolerance.
- **Material, grade and finish attribution (D36-10).** Recognition is narrow and aligned
  with the catalog's material categories and finishes (`master_data`). It is not catalog
  mapping:
  - A **material** needs a material term (aluminum/aluminium, steel, stainless, copper,
    brass, …) and no finish term. "galvanized steel" is a catalog category and is allowed.
  - A **finish** needs a finish term (powder coat, anodized, passivated, galvanized,
    brushed, painted, …, or an explicit no-finish) and no material term.
  - A **grade** must be code-shaped (304, 6061-T6, A36, C110, CRS) and not a measurement.

  Anything else is `field_not_attributable`.
- **Part ownership (D36-11).** `candidate_specification(result, part)` accepts an
  `IntakePart` only by object identity with one of `result.parts`, or a 1-based index. It
  refuses:
  - a foreign part (even an equal copy), a missing part, or `bool`/`None`
  - an `error` or `no_parts_found` result, or a non-`IntakeResult`
  - unverified boundaries or an unverified label
- **Superseded statements (D36-12).** A source containing correction wording (wrong,
  incorrect, mistake, typo, correction, revised, superseded, amended, disregard, cancel,
  obsolete, "scratch that") trusts no structured value:
  `ambiguous/superseded_or_contradicted`. Notes are exempt. Deciding which value survives
  is Day 37.
- **Coded parts (D36-13).** Two or more distinct code labels followed by a colon
  ("BRK-200:", "BRK-201:") mark a multi-part source. One entry for such a source gets
  `unverified` boundaries.
- **Grounding names.** Grounding now exports `ITEM_NUMBER` and `PART_KIND` as public
  aliases of the same objects; its behaviour is unchanged. Intake no longer imports private
  names.

## Known limitations (after the independent audit remediation)

- **Attribution and negation are lexical clause rules, not language understanding.** They
  fail conservatively: a legitimate value whose clause lacks a cue word becomes
  `field_not_attributable`. Examples: "3 mm sheet", "120 x 80 mm", "30 covers" ("cover"
  is not in the part-kind vocabulary). A negation anywhere in the clause makes the value
  `negated_or_conditional`.
- **Negation and conditions spread only within a clause.** A prohibition stated in one
  clause or sentence and referring to a value in another is not detected. Example:
  "Coating: none of the following. Black powder coating." Day 37's contradiction checks
  should cover this.
- **Unverified boundaries hide useful values.** With `unverified` boundaries (including the
  real 4B model's habit of adding an empty second part), no field is verified. Recall is
  sacrificed for ownership safety.
- **Shared clause cues can still verify a wrong value.** Within one clause carrying the
  same cue, the rules cannot tell which of two equal-shaped values the customer meant.
  The one-measurement rule blocks the common dimension case. Quantities rely on part
  nouns or quantity words.

## Day 37: validation and clarification

```python
graph = intake.build_intake_graph(model)
result = await intake.run_intake(graph, text, source_id="email-1", rfq_id="RFQ-0001")
rules = await mcp.runtime.pinned_rules(session, MANUFACTURING_RULE_VERSION)  # trusted caller
catalog = graph_module.mcp_executor(mcp_integration, principal, rest_credential)  # optional
validation = await intake_validation.validate_intake(result, text, rules=rules, catalog=catalog)
```

### Flow

```
customer text --run_intake--> IntakeResult (Day 36, provisional, evidence-checked)
             --validate_intake(text, pinned rules, catalog executor)--> IntakeValidation
```

`validate_intake` is deterministic and makes no model call. It refuses:
- a text whose sha256 differs from `IntakeResult.source_sha256`
- a non-manufacturing rule version

An `error` result gives `not_evaluated`.

### Output contract: `IntakeValidation`

These are additional models. `IntakeResult` and `PartSpecification` are unchanged, apart
from three backward-compatible fields with defaults: `IntakeResult.source_sha256`,
`IntakeResult.discarded_empty_entries` and `IntakePart.scope`.

- **`IntakeValidation`:**
  - `source_id`, `rfq_id`: trusted, copied from the result
  - `readiness`
  - `manufacturing_rule_version`, `manufacturing_rule_fingerprint`
  - `parts`
  - `findings`: RFQ level
  - `clarifications`
  - `feasibility_evaluated`: always `False`
- **`PartValidation`:**
  - `readiness`
  - `part_type`
  - `verified`: intake fields verified and not contradicted
  - `derived`: values from the source scan; see below
  - `findings`
  - `material`, `finish`: each a `CatalogCheck` (`status`, `tool`, `query`, `material_id` or
    `finish_id`, catalog `options`)
  - `operations`
  - `specification`: a `PartSpecification`, built **only when ready**
- **`Finding`:**
  - `part_index`, `field`, `kind`
  - `detail`: a fixed code
  - `evidence`, `spans`: exact source text and offsets
  - `action`: `clarify` (customer) or `review` (internal)
- **`Clarification`:**
  - `id`: stable, `"<part>:<field>:<kind>"`
  - `part_index`, `part_label`, `field`, `kind`
  - `question`

| Readiness | Meaning |
|---|---|
| `needs_clarification` | at least one `clarify` finding: missing, ambiguous, contradiction, ownership, invalid, unknown operation, no parts |
| `requires_review` | no customer question, but an internal finding: catalog not checked, unavailable or no match; rules not supplied; an unsupported requirement from the pinned rules; a tolerance standard; suspected injection |
| `ready_for_manufacturing_evaluation` | every pinned mandatory field is verified, the material id and finish id are catalog-recognized, and there are no findings. **Not** a feasibility, stock, schedule or price result |
| `not_evaluated` | the Day 36 result was an error |

The RFQ readiness is the worst of the RFQ-level findings and all parts.

### Rules and their sources

- **Required fields.** Required fields are the pinned rule document's `mandatory_fields`
  (`mfg-v1.0`: part_type, material_id, length_mm, width_mm, thickness_mm, quantity,
  tolerance_mm, finish_id). `height_required` part types add height, and
  `weld_length_required` part types add weld length. Without rules, the result is
  `rules_not_checked` and nothing is ready. A `part_id` (a verified customer identifier) is
  required to build a `PartSpecification`. The agent never invents one.
- **Part type.** Mapped only from the contract `PartType` words in the verified description
  or in the part's own segment ("L bracket" → `l_bracket`, "panel" → `flat_panel`, and so
  on). A plain "bracket" is asked about ("L- or U-bracket?"). Several kinds in one segment
  are asked about. A type outside `allowed_part_types` needs review.
- **Catalog.** Uses the read-only `search_materials` tool only, through the trusted
  executor (`graph.mcp_executor`: delegated credential, authenticated gateway, RBAC v2).
  Lookups are sequential, at most `MAX_CATALOG_CALLS = 20` per validation, with a timeout
  and no retries. Every failure, including off-contract output, is `unavailable`; text is
  never surfaced.
  - **Pagination (D37-05).** Every page of a query is read (`limit = MAX_PAGE_SIZE`, `offset`
    advanced) within the same call budget. An incomplete result is `unavailable`, never a
    match: an empty page with `has_more`, offsets past `MAX_OFFSET`, or an exhausted budget
    (`incomplete_results` or `call_budget`).
  - **Row consistency (D37-06).** Every returned row must have the requested category (and
    grade, case-insensitive), and the page must echo the requested offset. Otherwise the
    response is `unavailable` / `inconsistent_response`, and the catalog is not trusted for
    the rest of the validation. This also applies to finish queries. The material text maps to a catalog category:
  - aluminum, stainless steel, galvanized steel, mild steel, copper, brass
  - plain "steel" is asked about

  `material_id` is set only for exactly one catalog row with that category and grade. With
  no grade, the customer is asked, with the catalog's grades as options. `finish_id` is set
  only when `search_materials(category, grade, finish_id)` lists the matched material.
  Otherwise the finish is `no_match` and needs review: it may be incompatible or unknown,
  and deciding that is manufacturing's job. The query and tool are recorded in
  `CatalogCheck`.
- **Operations.**
  - Words naming the pinned rules' `unsupported_operations` (casting, forging, additive) are
    `unsupported_requirement` (review), as is anodized steel (`anodize_steel`).
  - **D37-01:** the source itself is scanned too, independent of the model, so an omitted
    "Also cast the panels." is still found.
    - Negated or prohibited mentions ("Do not cast", "No forging") are not requests.
    - Inside a part's segment, the finding belongs to that part.
    - Outside every segment (RFQ-wide text, or the whole text when boundaries are
      unverified), it is RFQ-level and clears every part's specification.
  - Operation words matching the catalog's operation ids are `recognized_not_checked`; no
    MCP operation catalog exists.
  - Other words are `unknown_operation`, and the customer is asked to describe them.
  - **D37-08: requests the model omitted.** A narrow source detector finds two shapes:
    - an imperative after "also", "please" or "then" followed by an object word ("Also
      knurl the edges", "Please emboss the logo");
    - a gerund after "requires", "with", "including" and similar, ending a phrase
      ("requires knurling").

    These are skipped: recognized operation words, finish or material words, a short
    stop-list of non-manufacturing verbs (send, quote, note, …), negated requests, and words
    already extracted. Unsupported words go to the D37-01 check. Anything else is
    `unknown_operation` (`source_request`), owned by its part. Outside every segment it is
    RFQ-level and blocks every part (`needs_clarification`, no spec). Several unknown
    operations of one part share one question that quotes each word.

    **Limitation:** operation requests phrased in other shapes ("knurled edges", "edges to
    be knurled") are not detected unless the model extracts them. This is deliberately not
    open natural-language understanding.
- **RFQ-wide requirements (D37-07).** Text outside every part's segment (for example
  "All parts must be stainless steel." before the labelled parts) is scanned for material
  category, grade and finish, both requested and prohibited. Each value is compared with
  each part's own verified or source-scanned value; it is never copied into a part.
  - A sentence with an explicit quantifier (all, every, each, both, any) applies to every
    part. A conflict or prohibition gives a `contradiction` (`rfq_wide_conflict` or
    `rfq_wide_prohibited`) with both spans.
  - Without a quantifier the scope is unclear. A conflict gives `ownership_unverified`
    (`rfq_wide_scope_unclear`). Agreeing introductory text is ignored.
  - A contested value is not catalog-checked, and the affected parts get no
    specification.
  - A statement limited to one part names its label. After that part's section it belongs
    to that segment; before it, the repeated label interleaves and boundaries fail safe as
    `unverified`.
- **Contradictions.** Each scanned field is scanned across clauses and sentences within the
  part's own segment, with the Day 36 attribution, negation and alternative rules. The
  scanned fields are quantity, hole count, thickness, width, length, height, tolerance,
  finish, **material category (D37-02)** and **grade (D37-03)**.
  - A grade counts only next to a material word, before or after it ("aluminum 6061-T6",
    "6061-T6 aluminum", D37-09), or after "grade", "alloy" or "type", or in a clause naming
    a grade. It must not be a size, a count, a date or a part label.
  - A bare number before a material word that reads as a quantity ("25 aluminum panels") is
    a count, not a grade. A verified part label ("BRK-200 aluminum plates") is never a
    grade.
  - Limitation: a bare-number grade written before the material and followed by a part noun
    ("304 stainless brackets") reads as a quantity, so it is not taken as a grade there.
  - A contested material or grade is never looked up in the catalog.
  - **Prohibitions (D37-04).** The same value both requested and prohibited, in any clause
    of the segment ("powder coat finish … Do not powder coat these parts"), is a
    `contradiction` with detail `requested_and_prohibited`. Two or more firm
  values give a `contradiction`, with every value's evidence. When correction wording is
  nearby, the detail is `correction_stated`. **Corrections are never resolved
  automatically**: the customer confirms. A value stated only as alternatives gives an
  `ambiguous` finding quoting the options, even if the model dropped them.
- **Source scan (`derived`).** A field gets the part segment's single firm value, with
  provenance `source_scan`, only when all of these hold:
  - the model gave no usable value
  - the field has no finding
  - the segment states exactly one firm value, once, with no alternatives

  This needs established boundaries, and it reuses the Day 36 rules. Values are never
  moved between parts.

### Clarification behaviour

- Questions are rendered from findings only, by fixed templates. No model is used, so
  nothing can be dropped or invented.
- There is one question per (part, field). The most specific finding wins: contradiction,
  then ownership, invalid, ambiguous, unknown operation, missing.
- Verified fields are never asked about. Free-text description and notes are never asked
  about. A rejected model value with no source support (`unverified`) is never quoted back;
  if the field is required, the question simply asks for it.
- Questions name the customer's label ("Part 'BRK-201': …"), quote only the customer's own
  text (at most 80 characters) and, for grades, offer the catalog options.
- The stable `id` lets a future conversation map an answer to the finding. An answer is
  appended as new customer text, and `run_intake` + `validate_intake` run again. No
  persistence or Supervisor exists yet.

### Extraction changes (Day 37)

- **Prompt.** One entry per part, null for unstated fields, short values, an identifier-only
  label, and a complete example.
- **Provider schema.** Every key is required in the JSON schema shown to the provider
  (`json_schema_extra`); Pydantic validation still accepts omitted keys. This made the 4B
  model consider each field.
- **Safe proposal cleanup:**
  - Entries with no label and nothing stated are dropped and counted
    (`discarded_empty_entries`). They carry no information.
  - Split digits ("30 0 mm", "BRK- 201") are rejoined only when the original is not in the
    source and the rejoined text is. The result is the customer's own wording, and it is
    still verified.
- **Verifier fixes found during Day 37:**
  - A count token is never part of a code ("5052-H32") or the year of an ISO date.
  - A number after a material word, "grade", "alloy" or "type" is not a quantity
    ("steel 1018").
  - A numeric tolerance stated with words ("tolerance ±0.2 mm") is parsed as a number;
    only named standards (ISO, DIN, ASME, …) stay verbatim.
  - A label may repeat inside its own section ("Correction: Part B must be …"); another
    label in between makes the split unsafe.
  - The label-overlap check compares the label with verified structured values, not with
    whole quotes.

### Real-model evaluation (qwen3:4b-instruct-2507-q4_K_M, local, temperature 0, seed 7)

The fixed set is `tests/intake_eval_set.py`: eight authored RFQs, not frozen evaluation data
(single, multi, complete, coded, incomplete, ambiguous, malicious, correction).

| Metric | Day 36 final | Day 37 |
|---|---|---|
| Correct part count | 4/8 | 7/8 |
| Extra entries | 51 | 0 |
| Verified correct / gold | 9/33 (recall 0.27) | 29/33 (recall 0.88) |
| Incorrect trusted values | 0 | 0 (precision 1.00) |
| Complete RFQ readiness | n/a | `ready_for_manufacturing_evaluation` |

The remaining misses are model-quality issues:
- The model returns no part for the vague "incomplete" request, so the customer is asked to
  list the parts.
- Some fields are missed or garbled (for example, Part A's finish in the multi-part case).

The opt-in test (`tests/test_intake_ollama.py`) asserts the following:
- zero incorrect trusted values
- recall ≥ 0.6
- the complete RFQ is ready
- the incomplete, ambiguous, correction and malicious cases are not ready

### Outside the Intake scope

- **Attachment retrieval and cross-document checks.** No authorized intake or attachment
  MCP tool exists. Only caller-supplied text with a trusted `source_id` is validated.
- **Manufacturing feasibility, inventory, capacity, lead time, pricing, approval and
  persistence.** These belong to the later agents and services.
- **Applying clarification answers in a conversation.** This needs the Supervisor and
  conversation state.

## Interview notes (how it works end to end)

- **Two layers.** The LLM reads free text and proposes structured values with quotes (Day
  36). Deterministic code then decides what is true (verifier) and what is enough (Day 37
  validation). The model never decides readiness and never writes a question.
- **Evidence first.** Every trusted value points at exact source text in that part's own
  segment, attributed to the right field, not negated, with no alternatives. Contradictions
  are found by scanning the source itself, not by trusting the model.
- **Rules from the source of truth.** Required fields come from the pinned manufacturing
  rule version, and catalog ids come from the authorized read-only catalog tool. When
  either is unavailable, the answer is "not checked", never a guess.
- **Readiness is not feasibility.** `ready_for_manufacturing_evaluation` hands a complete,
  catalog-recognized `PartSpecification` to the Manufacturing Agent, which may still
  decline it.
- **Supervisor integration.** The Supervisor will:
  1. call `run_intake`, then `validate_intake` with the pinned rules and a principal-bound
     catalog executor;
  2. route on `readiness`: ask the customer the `clarifications`, send `requires_review`
     to a human, or pass `specification`s to the Manufacturing Agent;
  3. rerun both steps when answers arrive.

  Both outputs are immutable and serializable, and they carry the trusted ids.
- **Not a chatbot.** One bounded model call. Typed output. No tools in the extraction step.
  No free-text answers. Every question is traceable to a finding.
