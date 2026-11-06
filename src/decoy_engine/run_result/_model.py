"""The ``decoy-run-result/v1`` document: one pydantic model, written once.

Value-free by construction. Every string field is a ``Literal``, an enum, or one of the named
types below, and each named type carries a :class:`ValueFree` marker and a validator, so no cell
value, exception text, absolute path or free-form message can ride in a document. The guard test
walks this tree and fails on any other leaf.

The write model (:class:`RunResult`) is strict: unknown fields and unknown enum values are
refused. The read model (:class:`RunResultRead`) is derived from it by :func:`_derive_read`, so an
older reader accepts a document whose enum gained a value or that carries an added optional field.
Under ``/v1`` only optional fields may be added (compatibility contract section 3.1).

Pattern source: the reason registry's own wire compatibility (``ReasonRecord.parse_payload``),
reused here instead of a second validator.
"""

from __future__ import annotations

import re
import types
from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any, Final, Literal, Union, get_args, get_origin

from annotated_types import Ge, Le, MaxLen
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    create_model,
)

from decoy_engine._progress_events import safe_error_class
from decoy_engine.reasons import ReasonRecord, approve_name, approve_ref, decoy

RUN_RESULT_SCHEMA: Final = "decoy-run-result/v1"


class ValueFree:
    """Marks a leaf type whose validator keeps the value free of data. ``kind`` names it."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind


def _check(kind: str, test: Callable[[str], bool]) -> Callable[[str], str]:
    """A validator that passes ``value`` through when ``test`` holds; the error text is fixed."""

    def validate(value: str) -> str:
        if not test(value):
            raise ValueError(f"not a valid {kind}")
        return value

    return validate


def _matches(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern)
    return lambda value: compiled.fullmatch(value) is not None


def _relative_path(value: str) -> bool:
    if not value or len(value) > 256 or value.startswith("/"):
        return False
    return all(
        part not in (".", "..")
        and approve_name(part) is not None
        and re.fullmatch(r"[A-Za-z]:", part) is None
        for part in value.split("/")
    )


SafeName = Annotated[
    str,
    ValueFree("safe_name"),
    AfterValidator(_check("safe_name", lambda v: approve_name(v) is not None)),
]
SafeRef = Annotated[
    str,
    ValueFree("safe_ref"),
    AfterValidator(_check("safe_ref", lambda v: approve_ref(v) is not None)),
]
ErrorClass = Annotated[
    str,
    ValueFree("error_class"),
    AfterValidator(_check("error_class", lambda v: safe_error_class(v) == v)),
]
Sha256 = Annotated[
    str, ValueFree("sha256"), AfterValidator(_check("sha256", _matches(r"sha256:[0-9a-f]{64}")))
]
Hex64 = Annotated[
    str, ValueFree("hex64"), AfterValidator(_check("hex64", _matches(r"[0-9a-f]{64}")))
]
Version = Annotated[
    str,
    ValueFree("version"),
    AfterValidator(_check("version", _matches(r"\d{1,6}(\.\d{1,6}){1,3}[0-9A-Za-z.+\-]{0,32}"))),
]
UtcTimestamp = Annotated[
    str,
    ValueFree("utc_timestamp"),
    AfterValidator(
        _check("utc_timestamp", _matches(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"))
    ),
]
InvocationId = Annotated[
    str,
    ValueFree("invocation_id"),
    AfterValidator(
        _check(
            "invocation_id",
            _matches(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"),
        )
    ),
]
RelPath = Annotated[str, ValueFree("rel_path"), AfterValidator(_check("rel_path", _relative_path))]

Count = Annotated[int, Ge(0)]
Seconds = Annotated[float, Ge(0), AllowInfNan(False)]
ExitCode = Annotated[int, Ge(0), Le(255)]

RunStatus = Literal["succeeded", "findings", "partially_delivered", "failed", "cancelled"]
FailureCategory = Literal["usage", "parameter", "runtime", "capacity", "cancelled"]
DeliveryMode = Literal["in_place", "strict", "best_effort"]
DeliveryOutcome = Literal["delivered", "partial", "nothing", "not_applicable"]
VaultState = Literal["not_used", "committed", "failed"]
PipelineKind = Literal["mask", "generate", "mixed"]
ExecutionMode = Literal["full_frame", "sequential", "out_of_core", "chunked", "other"]
NativeState = Literal["pandas", "native", "unified_slice"]
TableKind = Literal["mask", "generate"]
TableOutcome = Literal["succeeded", "failed", "not_run"]
RowsSource = Literal["outputs", "phase", "rate"]
TableDeliveryState = Literal["delivered", "failed", "withheld", "dropped", "none"]
OutputFormat = Literal["csv", "parquet", "fixed_width"]
CheckId = Literal["post_validation", "post_scan_integrity", "fidelity", "native_post_run"]
CheckStatus = Literal["passed", "warned", "failed", "error", "not_run"]

# --- reason payloads ----------------------------------------------------------------------------


def _check_reason(value: dict[str, Any]) -> dict[str, Any]:
    """Write side: the payload must be exactly what the local registry would emit."""
    if ReasonRecord.from_payload(value).to_payload() != value:
        raise ValueError("reason payload does not match the registry")
    return value


ReasonPayload = Annotated[
    dict[str, Any], ValueFree("reason_payload"), AfterValidator(_check_reason)
]

# A record that parses to one of these never carried a usable reason of its own.
_DIAGNOSTIC_CODES: Final = frozenset(
    {str(decoy.REASON_RECORD_INVALID), str(decoy.UNSUPPORTED_SCHEMA_VERSION)}
)


def read_reason(value: object) -> dict[str, Any] | None:
    """Read side: the canonical local payload plus the version the writer stamped, else ``None``.

    Submitted semantics and context are never re-emitted; the compatibility decision belongs to
    ``ReasonRecord.parse_payload``.
    """
    record, written = ReasonRecord.parse_payload(value)
    if written is None or record.code in _DIAGNOSTIC_CODES:
        return None
    return {**record.to_payload(), "written_registry_version": written}


ReasonPayloadRead = Annotated[
    dict[str, Any] | None, ValueFree("reason_payload_read"), BeforeValidator(read_reason)
]

# --- the document -------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, populate_by_name=True
    )


class ProducerInfo(_Model):
    cli_version: Version
    engine_version: Version
    registry_version: Version
    event_schema: Count


class ConfigRef(_Model):
    name: SafeName | None
    file_sha256: Sha256 | None
    semantic_hash: Hex64 | None


class NativeRoute(_Model):
    applicable: bool
    state: NativeState | None


class Route(_Model):
    execution_mode: ExecutionMode | None
    route_reason: SafeRef | None
    chunked: bool
    native: NativeRoute | None


class Delivery(_Model):
    mode: DeliveryMode
    outcome: DeliveryOutcome
    vault: VaultState


class TableDelivery(_Model):
    state: TableDeliveryState
    format: OutputFormat | None
    output: RelPath | None
    reason: ReasonPayload | None
    dropped_because: SafeName | None


class TableResult(_Model):
    index: Count
    name: SafeName | None
    kind: TableKind
    outcome: TableOutcome
    rows_out: Count | None
    rows_source: RowsSource | None
    row_errors: Count | None
    elapsed_s: Seconds | None
    reason: ReasonPayload | None
    delivery: TableDelivery


class CheckCounts(_Model):
    passed: Count = Field(alias="pass")
    warning: Count
    fail: Count
    error: Count


class Check(_Model):
    id: CheckId
    status: CheckStatus
    blocking: bool
    counts: CheckCounts
    codes: Annotated[tuple[SafeRef, ...], MaxLen(64)]


class WarningEntry(_Model):
    code: SafeRef
    table: SafeName | None
    column: SafeName | None
    count: Count


class Failure(_Model):
    category: FailureCategory
    error_class: ErrorClass | None
    reason: ReasonPayload | None


class PhaseTiming(_Model):
    name: SafeRef
    elapsed_s: Seconds


class Timings(_Model):
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    elapsed_s: Seconds
    phases: tuple[PhaseTiming, ...]


class LogLink(_Model):
    path: RelPath
    sha256: Sha256


class EvidenceLink(_Model):
    manifest_hash: Sha256


class BaselineLink(_Model):
    record_id: SafeRef


class Links(_Model):
    log: LogLink | None
    evidence: EvidenceLink | None
    baseline: BaselineLink | None


class RunResult(_Model):
    # ``schema`` would shadow ``BaseModel.schema``, so the field has another name and the wire key.
    schema_tag: Literal["decoy-run-result/v1"] = Field(
        default="decoy-run-result/v1", alias="schema"
    )
    invocation_id: InvocationId
    command: Literal["run"] = "run"
    status: RunStatus
    exit_code: ExitCode
    pipeline_kind: PipelineKind | None
    producer: ProducerInfo
    config: ConfigRef | None
    route: Route
    delivery: Delivery
    tables: tuple[TableResult, ...]
    checks: tuple[Check, ...]
    warnings: Annotated[tuple[WarningEntry, ...], MaxLen(256)]
    failure: Failure | None
    timings: Timings
    links: Links

    def to_payload(self) -> dict[str, Any]:
        """The JSON dict written to ``--result`` and stored as the record payload."""
        return self.model_dump(mode="json", by_alias=True)


# --- read model, derived -----------------------------------------------------------------------


def _is_union(origin: object) -> bool:
    return origin is Union or origin is types.UnionType


def _read_annotation(ann: Any, cache: dict[type, type]) -> Any:
    """The read-side annotation: the closed substitution map, applied recursively."""
    origin = get_origin(ann)
    if origin is Annotated:
        base, *meta = get_args(ann)
        if any(isinstance(m, ValueFree) and m.kind == "reason_payload" for m in meta):
            return ReasonPayloadRead
        return Annotated[(_read_annotation(base, cache), *meta)]
    if _is_union(origin):
        return Union[tuple(_read_annotation(a, cache) for a in get_args(ann))]  # noqa: UP007
    if origin in (list, tuple, set, frozenset, dict):
        args = tuple(a if a is Ellipsis else _read_annotation(a, cache) for a in get_args(ann))
        return origin[args]
    if origin is Literal or (isinstance(ann, type) and issubclass(ann, Enum)):
        return str
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return _derive_read(ann, cache)
    return ann


def _derive_read(model: type[BaseModel], cache: dict[type, type] | None = None) -> type[BaseModel]:
    """``model`` with every enum a ``str``, reasons parsed tolerantly and unknown fields ignored."""
    cache = {} if cache is None else cache
    if model in cache:
        return cache[model]
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        ann: Any = info.annotation
        if info.metadata:
            ann = Annotated[(ann, *info.metadata)]
        default: Any = ... if info.is_required() else info.default
        fields[name] = (_read_annotation(ann, cache), Field(default, alias=info.alias))
    read = create_model(
        f"{model.__name__}Read",
        __config__=ConfigDict(
            extra="ignore", frozen=True, hide_input_in_errors=True, populate_by_name=True
        ),
        __module__=__name__,
        **fields,
    )
    cache[model] = read
    return read


RunResultRead: Any = _derive_read(RunResult)
