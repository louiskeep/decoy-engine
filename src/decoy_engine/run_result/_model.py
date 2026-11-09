"""The ``decoy-run-result/v1`` document: one pydantic model, written once.

Value-free by construction. Every string field is a ``Literal``, an enum, or one of the named
types below, and each named type carries a :class:`ValueFree` marker and a validator, so no cell
value, exception text, absolute path or free-form message can ride in a document. The guard test
walks this tree and fails on any other leaf.

The write model (:class:`RunResult`) is strict: unknown fields and unknown enum values are
refused. The read model (:class:`RunResultRead`) is derived from it by :func:`~decoy_engine._value_free.derive_read`, so an
older reader accepts a document whose enum gained a value or that carries an added optional field.
Under ``/v1`` only optional fields may be added (compatibility contract section 3.1).

Numbers and booleans are strict (no ``False`` as a ``0``, no ``"1"`` as a ``1``, no ``"false"`` as
``False``), on write and on read. The ``schema`` tag is a named type that accepts only major v1, and
the read model requires it: a document of another major is refused, never read as v1.

Pattern source: the reason registry's own wire compatibility (``ReasonRecord.parse_payload``),
reused here instead of a second validator.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from annotated_types import Ge, Le, MaxLen
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
)

from decoy_engine._value_free import (  # noqa: F401  (re-exported: the names tests and guards import here)
    Count,
    Hex64,
    InvocationId,
    RelPath,
    SafeName,
    SafeRef,
    Sha256,
    Token,
    UtcTimestamp,
    ValueFree,
    Version,
    check,
    derive_read,
    has_kind,
)
from decoy_engine.errors import ConfigRuleError
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import REGISTRY_VERSION_RE, ReasonRecord, decoy

RUN_RESULT_SCHEMA: Final = "decoy-run-result/v1"


SchemaTag = Annotated[
    str,
    ValueFree("schema_tag"),
    AfterValidator(check("schema_tag", lambda v: v == RUN_RESULT_SCHEMA)),
]
# The reason registry's own version: strict X.Y.Z, the one shape ``ReasonRecord`` parses.
RegistryVersion = Annotated[
    str,
    ValueFree("registry_version"),
    AfterValidator(
        check("registry_version", lambda v: REGISTRY_VERSION_RE.fullmatch(v) is not None)
    ),
]
ErrorClass = Annotated[
    str,
    ValueFree("error_class"),
    AfterValidator(check("error_class", lambda v: safe_error_class(v) == v)),
]
# Strict primitives: pydantic's lax mode turns ``False`` into 0, ``"1"`` into 1 and ``"false"`` into
# ``False``, which would let a mistyped field pass as a number or flag.
Seconds = Annotated[float, Strict(), Ge(0), AllowInfNan(False)]
ExitCode = Annotated[int, Strict(), Ge(0), Le(255)]
Flag = Annotated[bool, Strict()]

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
        raise ConfigRuleError("reason payload does not match the registry")
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


class _Base(BaseModel):
    """Alias-only (the ``pass`` count key has no second spelling) and frozen, write and read."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    def to_payload(self) -> dict[str, Any]:
        """The JSON dict written to ``--result`` and stored as the record payload."""
        return self.model_dump(mode="json", by_alias=True)


class _Model(_Base):
    model_config = ConfigDict(extra="forbid")


class _ReadBase(_Base):
    model_config = ConfigDict(extra="ignore")

    def to_payload(self) -> dict[str, Any]:
        """The read projection: what this reader understood, as the wire dict.

        Reasons appear in their read form (the canonical local payload plus
        ``written_registry_version``) or ``null``. It is a view of a read document, not a document
        to write back; only :class:`RunResult` validates a payload for writing.
        """
        return super().to_payload()


class ProducerInfo(_Model):
    cli_version: Version
    engine_version: Version
    registry_version: RegistryVersion
    event_schema: Count


class ConfigRef(_Model):
    name: SafeName | None
    file_sha256: Sha256 | None
    semantic_hash: Hex64 | None


class NativeRoute(_Model):
    applicable: Flag
    state: NativeState | None


class Route(_Model):
    execution_mode: ExecutionMode | None
    route_reason: SafeRef | None
    chunked: Flag
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
    blocking: Flag
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
    schema_tag: SchemaTag = Field(default=RUN_RESULT_SCHEMA, alias="schema")
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


# --- read model, derived -----------------------------------------------------------------------


def _substitute(meta: list[Any]) -> Any | None:
    return ReasonPayloadRead if has_kind(meta, "reason_payload") else None


RunResultRead: Any = derive_read(
    RunResult,
    base=_ReadBase,
    substitute=_substitute,
    required_kinds=("schema_tag",),
    module=__name__,
)
