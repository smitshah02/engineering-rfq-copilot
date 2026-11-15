"""Opt-in, privacy-safe Supervisor telemetry (Day 50, L07).

    emit(sink, "stage_finished", run=..., stage="manufacturing", status="feasible", ...)

- Contract: ``Event`` is the only thing a sink ever receives. It is a closed allow-list of
  fields, each a fixed code (``^[a-z0-9_]{1,64}$``), a random 32-hex id, a bounded integer or a
  bool, and each event name permits only its own fields. There is no field for free text, an
  amount, a customer/user id, a prompt, a response, evidence, a credential or exception text,
  so none can be expressed. A code-shaped value is still only a code by origin: callers pass
  values derived from agent ``Literal`` fields and fixed Supervisor codes, never customer text.
- Default: no sink, no work. ``SupervisorContext.telemetry`` / ``SupervisorRecovery(telemetry=)``
  default to ``None``; ``emit`` then returns at once. Nothing imports ``langfuse`` unless
  ``from_env`` is called with ``RFQ_LANGFUSE_ENABLED=true``; credentials alone enable nothing.
- Isolation: ``emit`` never raises. An invalid event is dropped; a sink failure is swallowed.
  Telemetry is written after decisions are made and is never read back, so it cannot route,
  approve, price, retry, resume or change custody.
- Langfuse: ``LangfuseSink`` maps events to explicit observations (one root per workflow run,
  one child per stage, short children for routes, tools, A2 fan-out). It sets only
  ``metadata`` (never input/output), exports only Langfuse SDK spans (``should_export_span``),
  and masks anything that is not allow-listed metadata (defence in depth, not the boundary).
  The LangChain ``CallbackHandler`` is deliberately not used: it would capture raw node state.
"""

import math
import os
import re
import threading
from collections import deque
from collections.abc import Mapping
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rfq_copilot.agent_integration import Stage

EventName = Literal[
    "workflow_started",
    "workflow_finished",
    "stage_started",
    "stage_finished",
    "route_decided",
    "tool_call",
    "a2_fanout",
    "recovery",
]
# graph.READ_ONLY_TOOLS, restated as a telemetry allow-list (equality is tested): any other
# tool name is reported as "unlisted", never echoed.
TOOL_NAMES = frozenset(
    {
        "search_materials",
        "get_material",
        "check_material_suitability",
        "check_manufacturing_feasibility",
        "check_inventory",
        "check_capacity",
        "estimate_lead_time",
        "estimate_costs",
        "calculate_price",
        "evaluate_quotation",
    }
)
_Code = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
_Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
_Ms = Annotated[int, Field(ge=0, le=86_400_000)]
_Count = Annotated[int, Field(ge=0, le=10_000)]
_COMMON = frozenset({"name", "run", "attempt"})
_FIELDS: dict[str, frozenset[str]] = {
    name: frozenset(fields.split())
    for name, fields in {
        "workflow_started": "previous_attempt run_kind concurrency",
        "workflow_finished": "outcome error action stage reason duration_ms",
        "stage_started": "stage agent",
        "stage_finished": "stage agent status decision reason duration_ms carried",
        "route_decided": "action next_stage stage outcome reason",
        "tool_call": "stage tool status duration_ms",
        "a2_fanout": "part_count concurrency completed failed fan_in",
        "recovery": "operation outcome code generation",
    }.items()
}


class Event(BaseModel):
    """One telemetry event: allow-listed, code-only metadata (see the module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    name: EventName
    run: _Hex | None = None  # random per workflow run (server-generated), never customer data
    attempt: _Hex | None = None  # the Supervisor attempt id (secrets.token_hex)
    previous_attempt: _Hex | None = None  # restart: the superseded attempt
    run_kind: Literal["start", "resume", "restart"] | None = None
    stage: Stage | None = None
    agent: Literal["A1", "A2", "A3", "A4", "A5"] | None = None
    status: _Code | None = None
    decision: _Code | None = None
    reason: _Code | None = None
    action: _Code | None = None
    next_stage: Stage | None = None
    outcome: _Code | None = None
    error: _Code | None = None
    code: _Code | None = None
    operation: _Code | None = None
    tool: _Code | None = None
    duration_ms: _Ms | None = None
    generation: Annotated[int, Field(ge=0, le=1_000_000)] | None = None
    part_count: _Count | None = None
    concurrency: Annotated[int, Field(ge=1, le=64)] | None = None
    completed: _Count | None = None
    failed: _Count | None = None
    fan_in: Literal["joined", "sequential", "refused", "not_run"] | None = None
    carried: bool | None = None

    @model_validator(mode="after")
    def _allowed(self):
        given = {k for k, v in self.__dict__.items() if v is not None}
        if not given <= _COMMON | _FIELDS[self.name]:
            raise ValueError("field not allowed for this event")
        if (self.run is None) != (self.name == "recovery"):  # workflow events need their run
            raise ValueError("run required")
        if self.tool is not None and self.tool not in TOOL_NAMES | {"unlisted"}:
            raise ValueError("tool not allow-listed")
        return self

    def metadata(self) -> dict:
        """What an exporter may send: the set fields except the event name."""
        return self.model_dump(mode="json", exclude_none=True, exclude={"name"})


METADATA_KEYS = frozenset(Event.model_fields) - {"name"}


def emit(sink, name: str, **fields) -> None:
    """Report one event to ``sink`` (or nothing when it is None). Never raises."""
    if sink is None:
        return
    try:
        event = Event(name=name, **fields)
    except (ValidationError, TypeError, ValueError):
        return  # not expressible under the contract: dropped, never coerced
    try:
        sink.record(event)
    except Exception:  # an exporter fault never reaches business execution
        return


class MemorySink:
    """Bounded in-process sink (tests, local inspection). No I/O."""

    def __init__(self, maxlen: int = 10_000) -> None:
        self.events: deque[Event] = deque(maxlen=maxlen)

    def record(self, event: Event) -> None:
        self.events.append(event)


# --- Langfuse (opt-in) --------------------------------------------------------------------

ENABLE_FLAG = "RFQ_LANGFUSE_ENABLED"
EXPORT_TIMEOUT_SECONDS = 3  # SDK HTTP timeout; export runs on the SDK's background thread
MAX_OPEN_RUNS = 256  # open workflow roots held by one sink; more are not traced
_SAFE_SCALAR = r"^(?:[a-z0-9_]{1,64}|A[1-5])$"  # codes, hex ids, stage names, agent ids


def _safe_metadata(data) -> bool:
    if not isinstance(data, Mapping) or not set(data) <= METADATA_KEYS:
        return False
    for value in data.values():
        if isinstance(value, bool | int):
            continue
        if not isinstance(value, str) or not re.fullmatch(_SAFE_SCALAR, value):
            return False
    return True


def mask(*, data, **kwargs):
    """Langfuse ``mask`` hook: allow-listed metadata or scalars pass, anything else redacted.
    A second line of defence; the ``Event`` contract is the privacy boundary."""
    if data is None or isinstance(data, bool | int) or _safe_metadata(data):
        return data
    if isinstance(data, str):
        return data if re.fullmatch(_SAFE_SCALAR, data) else "[redacted]"
    return "[redacted]"


class LangfuseSink:
    """Events -> explicit Langfuse observations. Metadata only; bounded; thread-safe."""

    def __init__(self, client, max_open: int = MAX_OPEN_RUNS) -> None:
        self._client, self._max = client, max_open
        self._roots: dict[str, object] = {}
        self._stages: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _end(observation) -> None:
        """Best-effort ``end()``: an SDK failure here is contained (the observation may then
        never be delivered; nothing claims it was)."""
        try:
            observation.end()
        except Exception:
            pass

    def _child(self, parent, name: str, meta: dict, end: bool = True):
        """Open, label and (unless ``end`` is False) end one observation. If labelling fails
        the observation is ended at once and the failure propagates (``emit`` contains it),
        so no unlabelled handle is kept or left open."""
        obs = parent.start_observation(name=name, as_type="span")  # failure: nothing opened
        try:
            obs.update(metadata=meta)
        except Exception:
            self._end(obs)
            raise
        if end:
            self._end(obs)
        return obs

    def _finish(self, observation, meta: dict) -> None:
        """Label then end an observation already released from the sink's maps; ``end`` is
        attempted even when labelling fails."""
        try:
            observation.update(metadata=meta)
        finally:
            self._end(observation)

    def record(self, event: Event) -> None:
        meta, run = event.metadata(), event.run
        with self._lock:
            if event.name == "workflow_started":
                if run not in self._roots and len(self._roots) < self._max:
                    self._roots[run] = self._child(self._client, "rfq.supervisor.workflow", meta, end=False)  # fmt: skip
                return  # a failed root is never stored: the run's later events are dropped
            if run is None:  # recovery events: one standalone observation each
                self._child(self._client, f"rfq.supervisor.{event.name}", meta)
                return
            root = self._roots.get(run)
            if root is None:  # a run beyond the bound (or already finished): not traced
                return
            if event.name == "stage_started":
                key = (run, event.stage)
                self._stages[key] = self._child(root, f"rfq.stage.{event.stage}", meta, end=False)  # fmt: skip
            elif event.name == "stage_finished":
                span = self._stages.pop((run, event.stage), None)  # released before any SDK call
                if span is None:  # no start (e.g. a re-recorded held result)
                    span = root.start_observation(name=f"rfq.stage.{event.stage}", as_type="span")
                self._finish(span, meta)
            elif event.name == "workflow_finished":
                # Release every reference first, so an SDK failure below leaks nothing.
                stages = [self._stages.pop(k) for k in [k for k in self._stages if k[0] == run]]
                del self._roots[run]
                for span in stages:  # interrupted stages (cancel, error)
                    self._end(span)
                self._finish(root, meta)
            else:
                parent = self._stages.get((run, event.stage), root) if event.stage else root
                self._child(parent, f"rfq.{event.name}", meta)

    def flush(self) -> None:
        try:
            self._client.flush()
        except Exception:
            pass

    def shutdown(self) -> None:
        """Application shutdown: flush and stop the SDK's exporter. Never raises."""
        try:
            self._client.shutdown()
        except Exception:
            pass


def from_env(env: Mapping[str, str] | None = None):
    """``(sink, reason)``: a ``LangfuseSink`` only when ``RFQ_LANGFUSE_ENABLED=true`` and the
    configuration is valid, else ``(None, reason)`` with a fixed reason. Values are never
    logged or returned. No region is assumed: ``LANGFUSE_BASE_URL`` is required."""
    env = os.environ if env is None else env
    if env.get(ENABLE_FLAG, "").strip().lower() != "true":
        return None, "disabled"
    public = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = env.get("LANGFUSE_SECRET_KEY", "").strip()
    base = env.get("LANGFUSE_BASE_URL", "").strip()
    if not public or not secret:
        return None, "missing_credentials"
    try:
        url = urlsplit(base)
        local = url.hostname in ("localhost", "127.0.0.1")
        ok = (url.scheme == "https" or (url.scheme == "http" and local)) and bool(url.hostname)
        ok = ok and not (url.username or url.password or url.query or url.fragment)
    except ValueError:
        ok = False
    if not ok:
        return None, "invalid_base_url"
    try:
        sample = float(env.get("LANGFUSE_SAMPLE_RATE", "1.0"))
    except ValueError:
        return None, "invalid_sample_rate"
    if not math.isfinite(sample) or not 0.0 <= sample <= 1.0:
        return None, "invalid_sample_rate"
    environment = env.get("LANGFUSE_TRACING_ENVIRONMENT", "development").strip()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", environment) or environment.startswith("langfuse"):
        return None, "invalid_environment"
    try:
        from langfuse import Langfuse
        from langfuse.span_filter import is_langfuse_span
    except ImportError:
        return None, "sdk_unavailable"
    try:
        client = Langfuse(
            public_key=public,
            secret_key=secret,
            base_url=base,
            sample_rate=sample,
            environment=environment,
            tracing_enabled=True,
            timeout=EXPORT_TIMEOUT_SECONDS,
            mask=mask,
            should_export_span=is_langfuse_span,  # no third-party OTel spans (prompts, state)
        )
    except Exception:
        return None, "sdk_init_failed"
    return LangfuseSink(client), "enabled"
