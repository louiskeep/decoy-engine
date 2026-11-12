"""`scan_sources`: one scan over many declared tables, and the `scan-set/v1` document.

Tables are scanned one at a time and each frame is dropped before the next read, so memory is
bounded by the largest sample, not the sum. Every source in the mapping is validated up front,
selected or not. Column names come from the producer's header text, read as the descriptor
declares it (`has_header`, `column_names`), never inferred. When the header guard flags a
header row that looks like data, the persisted core writes positional names (`c0..cn`) in place
of the header text. A header that is misdeclared and that the guard cannot recognise (a person
name, an address) is the stated limit: its text is then a column name like any other.

The persisted form is `ScanSetCore`, the one definition of the value-free core. Its write model
refuses unknown fields; `ScanSetCoreRead` is derived from it with unknown fields ignored, so an
older reader accepts a newer record (compatibility contract section 3.1). Nothing in the core
can carry a cell value: it holds counts, rates, type labels, detector ids and column names
(positional when the header is suspect).
`scan_set_to_dict(detail=True)` adds the nested `ColumnProfile` dict per table, which is
value-bearing and for protected scratch only.

Errors name the table (a config key) and the exception class, never the exception text, which
can carry a cell value or a path. Errors that already carry an engine reason are raised as they
are, so their identity reaches the caller.
"""

from __future__ import annotations

import logging
import math
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Union, cast, get_args, get_origin

import pandas as pd
from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model, model_validator
from pydantic import ValidationError as _PydanticValidationError

from decoy_engine import reason_adapter, reasons
from decoy_engine._field_names import repeated_names
from decoy_engine.config import PipelineConfigError, SourceDescriptor
from decoy_engine.errors import DecoyError, PipelineValidationError
from decoy_engine.fileaccess import (
    FileAccess,
    FileAccessDenied,
    FileAccessReadError,
    FileAccessUnavailable,
    entry_point,
)
from decoy_engine.profiling import _scan_integrity
from decoy_engine.profiling._scan_integrity import RelationshipFacts, Status
from decoy_engine.profiling._scan_text_checks import count_leading_zeros, header_looks_like_data
from decoy_engine.profiling.profiler import profile_columns
from decoy_engine.profiling.types import ColumnProfile, FieldStats
from decoy_engine.progress import safe_error_class

SCAN_SET_SCHEMA: Final = "scan-set/v1"
Strategy = Literal["head", "random", "full"]

# Matches the CLI's long-standing cut-off for "this field is a PII candidate".
PII_CANDIDATE_THRESHOLD: Final = 0.6
_LEADING_ZERO_CHUNK_ROWS: Final = 50_000
_RANDOM_SEED: Final = 42
_LOG = logging.getLogger(__name__)
_DESCRIPTOR: TypeAdapter[Any] = TypeAdapter(SourceDescriptor)

# Errors the file-access and CSV layers already keep value-free; wrapping them would hide
# their codes from the caller. Any other registered engine family passes through too
# (`_is_coded`), so only an unclassified failure is wrapped.
_PASS_THROUGH = (
    FileAccessDenied,
    FileAccessReadError,
    FileAccessUnavailable,
    PipelineValidationError,
)


class ScanSourceError(DecoyError):
    """A table could not be scanned. Carries the table name and the cause's class only."""

    def __init__(self, table: str, cause_class: str) -> None:
        self.table = table
        self.cause_class = cause_class
        super().__init__(f"scan of table {table!r} failed: {cause_class}")


# ---------------------------------------------------------------------------
# The persisted core
# ---------------------------------------------------------------------------


class _Core(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WarningRefCore(_Core):
    namespace: str
    code: str

    @model_validator(mode="after")
    def _known(self) -> WarningRefCore:
        # Write side only: the read variant drops validators, so an older reader still
        # accepts a code a newer registry added.
        reasons.require_known(reasons.ReasonRef(self.namespace, self.code))
        return self


class FieldCore(_Core):
    name: str
    dtype: str
    null_count: int
    null_rate: float | None
    distinct_count: int
    pii_score: float | None
    pii_candidate: bool
    detector_hits: list[str]
    leading_zero_count: int | None


class TableCore(_Core):
    name: str
    descriptor: dict[str, Any]
    row_count: int
    row_count_exact: bool
    sample_strategy: Strategy
    sample_row_cap: int | None
    fields: list[FieldCore]
    key_candidates: list[str]
    warnings: list[WarningRefCore]
    reid_risk_score: float | None
    quasi_identifier_groups: list[list[str]]


class ChildFactsCore(_Core):
    table: str
    columns: list[str]
    rows: int | None
    null_key_rows: int | None
    orphan_rows: int | None
    orphan_distinct_keys: int | None


class RelationshipFactsCore(_Core):
    parent: str
    parent_columns: list[str]
    children: list[ChildFactsCore]
    status: Status
    parent_rows: int | None
    parent_null_key_rows: int | None
    parent_duplicate_excess: int | None
    exact: bool
    bounded: bool
    warnings: list[WarningRefCore]


class ScanSetCore(_Core):
    scan_schema: Literal["scan-set/v1"]
    engine_version: str
    scanned_at: str
    tables: list[TableCore]
    relationships: list[RelationshipFactsCore]


def _relaxed(model: type[BaseModel]) -> type[BaseModel]:
    """`model` with unknown fields ignored, nested models included."""

    def swap(annotation: Any) -> Any:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return _relaxed(annotation)
        origin = get_origin(annotation)
        if origin is None:
            return annotation
        args = tuple(swap(a) for a in get_args(annotation))
        if origin in (Union, types.UnionType):
            return Union[args]  # noqa: UP007 - built from a runtime tuple
        return origin[args]

    fields: dict[str, Any] = {
        name: (swap(info.annotation), info) for name, info in model.model_fields.items()
    }
    config = ConfigDict(extra="ignore", frozen=True, strict=True)
    return create_model(f"{model.__name__}Read", __config__=config, **fields)


ScanSetCoreRead: Final = _relaxed(ScanSetCore)


# ---------------------------------------------------------------------------
# In-memory result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableScan:
    """What one table's scan found. `row_count` is the table total (an estimate unless
    `row_count_exact`); `profile.row_count` is the rows actually scanned."""

    name: str
    descriptor: dict[str, Any]
    profile: ColumnProfile
    warnings: tuple[reasons.ReasonRef, ...]
    row_count: int
    row_count_exact: bool
    key_candidates: tuple[str, ...]
    null_counts: dict[str, int]
    leading_zero_counts: dict[str, int | None]


@dataclass(frozen=True)
class ScanSet:
    tables: tuple[TableScan, ...]
    relationships: tuple[RelationshipFacts, ...]
    scan_schema: str
    engine_version: str
    scanned_at: str


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


@entry_point
def scan_sources(
    sources: Mapping[str, dict[str, Any]],
    *,
    tables: Sequence[str] | None = None,
    sample_rows: int | None = 10_000,
    strategy: Strategy = "head",
    relationships: Sequence[Mapping[str, Any]] = (),
    integrity: bool = True,
    spill_dir: Path | None = None,
    file_access: FileAccess | None = None,
) -> ScanSet:
    """Scan the selected tables of `sources` and return one `ScanSet`.

    `sources` maps table name to a source descriptor dict. Every descriptor is validated,
    selected or not; an invalid one is a `PipelineConfigError` naming the table. `tables`
    selects and orders (default: every source in mapping order). `sample_rows=None` or `strategy="full"` reads
    each table whole. `head` reads a bounded prefix; `random` reads the table and keeps a fixed
    random sample, so it is not bounded in memory.

    `relationships` are declared `RelationshipConfig` dicts. With `integrity` true they get
    key, null and orphan counts (`RelationshipFacts`), read from key columns only and spilled to
    a protected directory under `spill_dir` (default: the system temp directory). The tables
    they name must be in `sources` but need not be selected. Nothing is inferred.
    """
    from decoy_engine.release import __version__

    if strategy not in ("head", "random", "full"):
        raise PipelineConfigError("strategy must be 'head', 'random' or 'full'")
    if sample_rows is not None and (
        isinstance(sample_rows, bool) or not isinstance(sample_rows, int) or sample_rows < 1
    ):
        raise PipelineConfigError("sample_rows must be a positive integer or None")
    descriptors = {name: _validated(name, raw) for name, raw in sources.items()}
    selected = list(descriptors) if tables is None else list(tables)
    if repeated_names(selected):
        raise PipelineConfigError("tables selects the same table more than once")
    unknown = [t for t in selected if t not in descriptors]
    if unknown:
        raise PipelineConfigError("tables names a table that is not in sources")

    declared = _scan_integrity.declare(descriptors, relationships) if integrity else ()

    scans = []
    for name in selected:
        started = time.monotonic()
        scan = _scan_one(name, descriptors[name], sample_rows, strategy, file_access)
        _LOG.info(
            "scanned table=%s rows=%d seconds=%.2f",
            name,
            scan.profile.row_count,
            time.monotonic() - started,
        )
        scans.append(scan)
    return ScanSet(
        tables=tuple(scans),
        relationships=_integrity_facts(descriptors, declared, spill_dir),
        scan_schema=SCAN_SET_SCHEMA,
        engine_version=__version__,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )


def _integrity_facts(
    descriptors: Mapping[str, dict[str, Any]],
    declared: Sequence[_scan_integrity.DeclaredRelationship],
    spill_dir: Path | None,
) -> tuple[RelationshipFacts, ...]:
    if not declared:
        return ()
    cause: str | None = None
    try:
        return _scan_integrity.relationship_facts(descriptors, declared, spill_dir=spill_dir)
    except (*_PASS_THROUGH, PipelineConfigError):
        raise
    except Exception as exc:
        if _is_coded(exc):
            raise
        cause = safe_error_class(type(exc).__name__)
    raise ScanSourceError("relationships", cause or "Exception")


def _validated(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    failed = False
    try:
        model = _DESCRIPTOR.validate_python(dict(raw))
    except _PydanticValidationError:
        failed = True
    if failed:
        # Raised outside the handler so no validation error, and the input it holds, is chained.
        raise PipelineConfigError(f"source descriptor for table {name!r} is invalid")
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def _scan_one(
    name: str,
    descriptor: dict[str, Any],
    sample_rows: int | None,
    strategy: Strategy,
    file_access: FileAccess | None,
) -> TableScan:
    cause: str | None = None
    try:
        return _read_and_profile(name, descriptor, sample_rows, strategy, file_access)
    except _PASS_THROUGH:
        raise
    except Exception as exc:
        if _is_coded(exc):
            raise
        cause = safe_error_class(type(exc).__name__)
    raise ScanSourceError(name, cause or "Exception")


def passes_through(exc: BaseException) -> bool:
    return False


def _is_coded(exc: BaseException) -> bool:
    """True for a scan error or any engine error family the reason adapter classifies."""
    return isinstance(exc, (ScanSourceError, *reason_adapter.registered_families()))


def _read_and_profile(
    name: str,
    descriptor: dict[str, Any],
    sample_rows: int | None,
    strategy: Strategy,
    file_access: FileAccess | None,
) -> TableScan:
    # Imported here because decoy_engine.profile imports this package.
    from decoy_engine import profile as src
    from decoy_engine.execution._guards import refuse_duplicate_field_names

    reader = src.build_declared_source(name, descriptor)
    whole = strategy == "full" or sample_rows is None
    if sample_rows is None or whole or strategy == "random":
        frame = reader.to_frame()
        total, exact = len(frame), True
        if not whole and sample_rows is not None and len(frame) > sample_rows:
            frame = frame.sample(n=sample_rows, random_state=_RANDOM_SEED).reset_index(drop=True)
    else:
        count = reader.row_count()
        frame = reader.sample_frame(sample_rows)
        total, exact = max(count.value, len(frame)), count.exact
    refuse_duplicate_field_names(name, [str(c) for c in frame.columns])

    is_csv = descriptor["type"] == "file" and descriptor["format"] == "csv"
    warned: list[reasons.ReasonRef] = []
    zero_counts: dict[str, int] | None = None
    if is_csv:
        dialect = src.resolve_csv_dialect(descriptor)
        key = f"sources.{name}.path"
        if dialect.has_header:
            header = src.read_csv_source(
                descriptor["path"], dialect, table=name, dtype=str, nrows=0, field_key=key
            )
            if header_looks_like_data([str(c) for c in header.columns]):
                warned.append(reasons.warning.SCAN_HEADER_SUSPECT.spec.ref)
            del header
        if whole or strategy == "head":
            zero_counts = count_leading_zeros(
                _text_frames(src, descriptor, dialect, name, key, None if whole else sample_rows)
            )

    row_cap = None if whole else sample_rows
    profile = profile_columns(
        frame,
        name,
        sample_strategy="full" if whole else strategy,
        sample_row_cap=row_cap,
        file_access=file_access,
    )
    del frame
    return _table_scan(name, descriptor, profile, tuple(warned), total, exact, zero_counts)


def _text_frames(
    src: Any,
    descriptor: dict[str, Any],
    dialect: Any,
    name: str,
    key: str,
    head_rows: int | None,
) -> Iterator[pd.DataFrame]:
    """The sampled rows again, as text. A head sample is one bounded read; a whole table is
    read in chunks so only one is alive at a time."""
    if head_rows is not None:
        yield src.read_csv_source(
            descriptor["path"], dialect, table=name, dtype=str, nrows=head_rows, field_key=key
        )
        return
    yield from src.read_csv_source(
        descriptor["path"],
        dialect,
        table=name,
        dtype=str,
        chunksize=_LEADING_ZERO_CHUNK_ROWS,
        field_key=key,
    )


def _table_scan(
    name: str,
    descriptor: dict[str, Any],
    profile: ColumnProfile,
    warned: tuple[reasons.ReasonRef, ...],
    total: int,
    exact: bool,
    zero_counts: dict[str, int] | None,
) -> TableScan:
    scanned = profile.row_count
    keys = tuple(
        f.name
        for f in profile.fields
        if scanned > 0 and f.null_count == 0 and f.distinct_count == scanned
    )
    return TableScan(
        name=name,
        descriptor=descriptor,
        profile=profile,
        warnings=warned,
        row_count=total,
        row_count_exact=exact,
        key_candidates=keys,
        null_counts={f.name: f.null_count for f in profile.fields},
        leading_zero_counts={
            f.name: None if zero_counts is None else zero_counts.get(f.name) for f in profile.fields
        },
    )


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def _finite(value: float | None) -> float | None:
    """JSON has no NaN or infinity; an undefined rate is None."""
    if value is None or not math.isfinite(value):
        return None
    return float(value)


def _field_core(f: FieldStats, name: str, scanned: int, zeros: int | None) -> FieldCore:
    # Only these attributes of FieldStats are read: top_values, mode, min and max can hold
    # cell text and must not reach the core.
    return FieldCore(
        name=name,
        dtype=f.inferred_type,
        null_count=f.null_count,
        null_rate=_finite(round(f.null_rate, 4)) if scanned > 0 else None,
        distinct_count=f.distinct_count,
        pii_score=_finite(round(f.pii_score, 3)) if scanned > 0 else None,
        pii_candidate=f.pii_score >= PII_CANDIDATE_THRESHOLD,
        detector_hits=[m.detector_id for m in (f.detector_matches or [])],
        leading_zero_count=zeros,
    )


def _core_names(t: TableScan) -> dict[str, str]:
    """Real field name -> the name the persisted core writes.

    A header the guard flagged as data is producer text that can hold a value, so the core
    names those columns by position. The in-memory scan and the detail keep the real names."""
    names = [f.name for f in t.profile.fields]
    if reasons.warning.SCAN_HEADER_SUSPECT.spec.ref not in t.warnings:
        return {n: n for n in names}
    return {n: f"c{i}" for i, n in enumerate(names)}


def _table_core(t: TableScan) -> TableCore:
    p = t.profile
    core_name = _core_names(t)
    fields = [
        _field_core(f, core_name[f.name], p.row_count, t.leading_zero_counts.get(f.name))
        for f in p.fields
    ]
    return TableCore(
        name=t.name,
        descriptor=t.descriptor,
        row_count=t.row_count,
        row_count_exact=t.row_count_exact,
        sample_strategy=cast(Strategy, p.sample_strategy),
        sample_row_cap=p.sample_row_cap,
        fields=fields,
        key_candidates=[core_name[k] for k in t.key_candidates],
        warnings=[WarningRefCore.model_validate(asdict(w)) for w in t.warnings],
        reid_risk_score=_finite(p.reid_risk_score) if p.row_count > 0 else None,
        quasi_identifier_groups=[[core_name[n] for n in g] for g in p.quasi_identifier_groups],
    )


def scan_set_to_dict(s: ScanSet, *, detail: bool) -> dict[str, Any]:
    """The `scan-set/v1` dict. `detail=False` is `ScanSetCore` and value-free; `detail=True`
    adds `profile.to_dict()` as a per-table `detail` member, which can hold cell values."""
    core = ScanSetCore(
        scan_schema="scan-set/v1",
        engine_version=s.engine_version,
        scanned_at=s.scanned_at,
        tables=[_table_core(t) for t in s.tables],
        relationships=[RelationshipFactsCore.model_validate(r.to_dict()) for r in s.relationships],
    )
    doc: dict[str, Any] = core.model_dump()
    if detail:
        for table_doc, t in zip(doc["tables"], s.tables, strict=True):
            table_doc["detail"] = t.profile.to_dict()
    return doc
