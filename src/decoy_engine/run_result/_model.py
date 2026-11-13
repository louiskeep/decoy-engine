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

import re
from collections.abc import Callable
from typing import Annotated, Any, Final, Literal

from annotated_types import Ge, Le, MaxLen
from packaging.version import InvalidVersion
from packaging.version import Version as PackagingVersion
from pydantic import (
    AfterValidator,
    AllowInfNan,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
)

from decoy_engine._value_free import (
    Count,
    ValueFree,
    derive_read,
    has_kind,
)
from decoy_engine.errors import ConfigRuleError
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import REGISTRY_VERSION_RE, ReasonRecord, approve_name, approve_ref, decoy

RUN_RESULT_SCHEMA: Final = "decoy-run-result/v1"


def _check(kind: str, test: Callable[[str], bool]) -> Callable[[str], str]:
    """A validator that passes ``value`` through when ``test`` holds; the error text is fixed."""

    def validate(value: str) -> str:
        if not test(value):
            raise ConfigRuleError(f"not a valid {kind}")
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


# The read-side stand-in for every enum: any approved identifier, so a value added in a later
# minor reads, but free text does not.
Token = Annotated[
    str,
    ValueFree("token"),
    AfterValidator(_check("token", lambda v: approve_ref(v) is not None)),
]
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
Sha256 = Annotated[
    str, ValueFree("sha256"), AfterValidator(_check("sha256", _matches(r"sha256:[0-9a-f]{64}")))
]
Hex64 = Annotated[
    str, ValueFree("hex64"), AfterValidator(_check("hex64", _matches(r"[0-9a-f]{64}")))
]
_MAX_VERSION_LEN = 64


def _canonical_release(value: str) -> bool:
    """A PEP 440 version in its own canonical spelling, release segment of two or more parts.

    ``packaging`` is the parser; ``str(parsed) == value`` refuses the spellings it would normalize
    (a ``v`` prefix, uppercase, padding, surrounding whitespace, a trailing newline). An epoch is
    refused: Decoy never releases one, so ``1!2.0`` is not a producer version.
    """
    if len(value) > _MAX_VERSION_LEN:
        return False
    try:
        parsed = PackagingVersion(value)
    except InvalidVersion:
        return False
    return str(parsed) == value and parsed.epoch == 0 and len(parsed.release) >= 2


Version = Annotated[
    str, ValueFree("version"), AfterValidator(_check("version", _canonical_release))
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


SchemaTag = Annotated[
    str,
    ValueFree("schema_tag"),
    AfterValidator(_check("schema_tag", lambda v: v == RUN_RESULT_SCHEMA)),
]
# The reason registry's own version: strict X.Y.Z, the one shape ``ReasonRecord`` parses.
RegistryVersion = Annotated[
    str,
    ValueFree("registry_version"),
    AfterValidator(
        _check("registry_version", lambda v: REGISTRY_VERSION_RE.fullmatch(v) is not None)
    ),
]
ErrorClass = Annotated[
    str,
    ValueFree("error_class"),
    AfterValidator(_check("error_class", lambda v: safe_error_class(v) == v)),
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


class SampleMarker(_Model):
    """A run made with `--sample`: the head rows asked for, the seed tables, the rows read from
    each edgeless table and the scratch folder (relative to the project) the outputs went to."""

    rows: Count
    roots: Annotated[tuple[SafeName, ...], MaxLen(256)]
    head_tables: dict[SafeName, Count]
    scratch: RelPath


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
    # Optional under /v1 (compatibility contract section 3.1): a record that never set it keeps
    # its old wire form, so the key is left out of every dump until a sample sets it.
    sample: SampleMarker | None = Field(default=None, exclude_if=lambda v: v is None)


# --- read model, derived -----------------------------------------------------------------------


def _substitute(meta: list[Any]) -> Any | None:
    return ReasonPayloadRead if has_kind(meta, "reason_payload") else None


RunResultRead: Any = derive_read(
    RunResult,
    base=_ReadBase,
    token=Token,
    substitute=_substitute,
    required_kinds=("schema_tag",),
    module=__name__,
)
