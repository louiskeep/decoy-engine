"""Closed value types of a `decoy-support-bundle/v1` support bundle.

A bundle carries structure only: versions, limits, routes, timings, coded errors, topology and
coded events. No field holds free text. Names become ordinal `Label`s from a `LabelAllocator`,
versions are pattern-checked `Version`s, reasons are `decoy_engine.reasons.ReasonRef`s that the
bundle types check against the registry at construction, and every other field is a closed enum,
a bounded `int`, a `bool`, `None` or a tuple of those.

Pattern: the `_V1Fields` exact-type check of `decoy_engine._progress_events` ("parse, don't
validate": every holder re-checks exact types in `__post_init__`, so a `str` subclass, a `bool`
posing as an `int` or a hand-forged label is refused at construction), and the `sos clean`
sequential-token scheme (Red Hat, "Cleaning an sos report") for labels with no kept map.
"""

from __future__ import annotations

import functools
import re
import types
import typing
from dataclasses import dataclass, fields
from enum import Enum

from decoy_engine.execution.native._route_decline_codes import RouteDeclineCode
from decoy_engine.profile import PIIClass
from decoy_engine.reasons import Outcome, ReasonRef, RetryClass, Severity, find

_LABEL_TOKEN = object()
_LABEL = re.compile(r"(t[0-9]{1,4}(\.c[0-9]{1,4})?|n[0-9]{1,4})")
_LABEL_KIND = {"table": re.compile(r"t[0-9]{1,4}"), "node": re.compile(r"n[0-9]{1,4}")}
_LABEL_KIND["column"] = re.compile(r"t[0-9]{1,4}\.c[0-9]{1,4}")
_VERSION = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){1,2}([.-][0-9A-Za-z]{1,16})?")
# Closed ranges [low, end) by field name; an `int` field with no entry is refused.
_RANGES = {"ms": (0, 2**53), "arity": (1, 9)}
_RANGES |= dict.fromkeys(("rows_log2", "length_log2", "counts_log2"), (0, 64))
_RANGES |= dict.fromkeys(("seed_protocol", "threads", "peak_memory_mib"), (0, 2**31))
_LABEL_FIELDS = {"node": "node", "TableShape.label": "table", "ColumnShape.label": "column"}
_LABEL_FIELDS |= dict.fromkeys(("table", "parent", "child"), "table")
_LABEL_FIELDS |= dict.fromkeys(("parent_cols", "child_cols"), "column")


class Label(str):
    """An ordinal label (`t3`, `t3.c2`, `n4`). Only `LabelAllocator` can mint one."""

    __slots__ = ()

    def __new__(cls, value: object, token: object = None) -> Label:
        if token is not _LABEL_TOKEN:
            raise TypeError("a Label is minted by LabelAllocator only")
        if type(value) is not str or not _LABEL.fullmatch(value):
            raise ValueError("not an ordinal label")
        return super().__new__(cls, value)

    # A str subclass is otherwise rebuilt by pickle, copy and asdict without the token.
    def __copy__(self) -> Label:
        return self

    def __deepcopy__(self, memo: object) -> Label:
        return self

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (_relabel, (str(self),))


def _relabel(value: str) -> Label:
    return Label(value, _LABEL_TOKEN)


class Version(str):
    """A dotted version string: two or three numeric parts and one optional suffix."""

    __slots__ = ()

    def __new__(cls, value: object) -> Version:
        if type(value) is not str or not _VERSION.fullmatch(value):
            raise ValueError("not a version string")
        return super().__new__(cls, value)


class BundleSchema(str, Enum):
    V1 = "decoy-support-bundle/v1"


class RouteKind(str, Enum):
    CHUNKED_DISPATCHER = "chunked_dispatcher"
    LEGACY_CHUNKED = "legacy_chunked"
    LEGACY_FULL_FRAME = "legacy_full_frame"
    UNIFIED_SLICE = "unified_slice"
    UNKNOWN = "unknown"


class Phase(str, Enum):
    READ = "read"
    PROFILE_COMPILE = "profile_compile"
    EXECUTE = "execute"
    BOUNDARY_CONVERSION = "boundary_conversion"
    MATERIALIZE = "materialize"
    HOOKS = "hooks"


class DtypeClass(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    DATE = "date"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


class RatioBucket(str, Enum):
    ZERO = "zero"
    LT_1PCT = "lt_1pct"
    LT_10PCT = "lt_10pct"
    LT_50PCT = "lt_50pct"
    LT_100PCT = "lt_100pct"
    ALL = "all"
    UNKNOWN = "unknown"


class OrphanPolicy(str, Enum):
    PRESERVE = "preserve"
    REMAP = "remap"
    WARN = "warn"
    FAIL = "fail"


class StrategyKind(str, Enum):
    """Mask strategies (`SCALAR_HANDLERS` plus `from_parent`) and generate-column types."""

    BUCKET_PERTURB = "bucket_perturb"
    BUCKETIZE = "bucketize"
    CATEGORICAL = "categorical"
    CODESET = "code_set"
    CONSTANT = "constant"
    DATE_SHIFT = "date_shift"
    DERIVED = "derived"
    DERIVED_AGGREGATE = "derived_aggregate"
    DISTRIBUTION = "distribution"
    FAKER = "faker"
    FORMULA = "formula"
    FPE = "fpe"
    FROM_PARENT = "from_parent"
    GEO_GENERALIZE = "geo_generalize"
    GROUP_KEY = "group_key"
    GROUPED_SERIES = "grouped_series"
    HASH = "hash"
    JOINT_MASK = "joint_mask"
    NESTED = "nested"
    PASSTHROUGH = "passthrough"
    REDACT = "redact"
    REFERENCE = "reference"
    SEQUENCE = "sequence"
    SHUFFLE = "shuffle"
    STATISTICAL = "statistical"
    TEXT_MASK = "text_mask"
    TEXT_REDACT = "text_redact"
    TOKENIZE = "tokenize"
    TOPCODE = "top_code"
    TRUNCATE = "truncate"
    WINDOWED_DATE = "windowed_date"
    OTHER = "other"


class CountKey(str, Enum):
    ATTEMPT = "attempt"
    BATCHES = "batches"
    BYTES = "bytes"
    COLUMNS = "columns"
    ERRORS = "errors"
    FINDINGS = "findings"
    RETRIES = "retries"
    ROWS = "rows"
    TABLES = "tables"


class NativeStatus(str, Enum):
    PRESENT_OK = "present-ok"
    ABSENT = "absent"
    ABI_MISMATCH = "abi-mismatch"
    KAT_CORRUPT = "kat-corrupt"
    LOAD_ERROR = "load-error"


class PlatformFamily(str, Enum):
    LINUX = "linux"
    DARWIN = "darwin"
    WIN32 = "win32"
    OTHER = "other"

    @classmethod
    def of(cls, sys_platform: object) -> PlatformFamily:
        for member in (cls.LINUX, cls.DARWIN, cls.WIN32):
            if isinstance(sys_platform, str) and sys_platform.startswith(member.value):
                return member
        return cls.OTHER


@functools.cache
def _hints(cls: type) -> dict[str, object]:
    return typing.get_type_hints(cls)


def _check(owner: str, name: str, tp: object, value: object) -> None:
    """Exact-type check of `value` against the field annotation `tp`."""
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        if value is None and type(None) in args:
            return
        (tp,) = (a for a in args if a is not type(None))
        _check(owner, name, tp, value)
    elif origin is tuple:
        if type(value) is not tuple:
            raise TypeError(f"{owner}.{name} must be a tuple")
        items = args[:1] * len(value) if args[-1:] == (Ellipsis,) else args
        if len(items) != len(value):
            raise TypeError(f"{owner}.{name} has the wrong length")
        for item_tp, item in zip(items, value, strict=True):
            _check(owner, name, item_tp, item)
    elif not (
        tp in _LEAVES
        or (isinstance(tp, type) and (issubclass(tp, Enum) or issubclass(tp, _Checked)))
    ):
        raise TypeError(f"{owner}.{name} has a type a bundle may not carry")
    elif type(value) is not tp:
        raise TypeError(f"{owner}.{name} must be {getattr(tp, '__name__', tp)}")
    elif tp is ReasonRef:
        ns, code = getattr(value, "namespace", None), getattr(value, "code", None)
        if type(ns) is not str or type(code) is not str or find(ns, code) is None:
            raise ValueError(f"{owner}.{name} is not a registered reason")
    elif tp is int and isinstance(value, int):
        low, end = _RANGES[name]
        if not low <= value < end:
            raise ValueError(f"{owner}.{name} is out of range")
    elif tp is Label and isinstance(value, str):
        kind = _LABEL_FIELDS.get(f"{owner}.{name}") or _LABEL_FIELDS[name]
        if not _LABEL_KIND[kind].fullmatch(value):
            raise ValueError(f"{owner}.{name} must be a {kind} label")


class _Checked:
    def __post_init__(self) -> None:
        cls: type = type(self)
        owner, hints = cls.__name__, _hints(cls)
        for f in fields(self):  # type: ignore[arg-type]
            _check(owner, f.name, hints[f.name], getattr(self, f.name))


@dataclass(frozen=True)
class Versions(_Checked):
    engine: Version
    sdk: Version
    registry: Version
    product: Version
    python: Version
    seed_protocol: int
    platform: PlatformFamily
    native: NativeStatus


@dataclass(frozen=True)
class Limits(_Checked):
    threads: int | None
    peak_memory_mib: int | None


@dataclass(frozen=True)
class RouteFact(_Checked):
    node: Label
    table: Label
    route: RouteKind
    native_admitted: bool | None
    declines: tuple[RouteDeclineCode, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if list(self.declines) != sorted(set(self.declines), key=lambda c: c.value):
            raise ValueError("declines must be unique and sorted")


@dataclass(frozen=True)
class PhaseTiming(_Checked):
    phase: Phase
    ms: int


@dataclass(frozen=True)
class NodeTiming(_Checked):
    node: Label
    ms: int


@dataclass(frozen=True)
class Timings(_Checked):
    phases: tuple[PhaseTiming, ...]
    nodes: tuple[NodeTiming, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if len({p.phase for p in self.phases}) != len(self.phases):
            raise ValueError("a phase repeats")


@dataclass(frozen=True)
class ErrorFact(_Checked):
    node: Label
    table: Label | None
    reason: ReasonRef
    outcome: Outcome
    retry_class: RetryClass


@dataclass(frozen=True)
class ColumnShape(_Checked):
    label: Label
    dtype: DtypeClass
    strategy: StrategyKind
    nullable: RatioBucket
    distinct: RatioBucket
    length_log2: int | None
    pk: bool
    fk: bool
    pii: PIIClass | None


def _owned(table: str, columns: tuple[Label, ...]) -> bool:
    return all(c.split(".", 1)[0] == table for c in columns)


@dataclass(frozen=True)
class TableShape(_Checked):
    label: Label
    rows_log2: int | None
    columns: tuple[ColumnShape, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        labels = tuple(c.label for c in self.columns)
        if not _owned(self.label, labels) or len(set(labels)) != len(labels):
            raise ValueError("a column label belongs to another table or repeats")


@dataclass(frozen=True)
class EdgeShape(_Checked):
    parent: Label
    parent_cols: tuple[Label, ...]
    child: Label
    child_cols: tuple[Label, ...]
    arity: int
    orphan_policy: OrphanPolicy | None

    def __post_init__(self) -> None:
        super().__post_init__()
        for table, cols in ((self.parent, self.parent_cols), (self.child, self.child_cols)):
            if len(cols) != self.arity or not _owned(table, cols):
                raise ValueError("edge columns must be arity columns of their table")


@dataclass(frozen=True)
class Topology(_Checked):
    tables: tuple[TableShape, ...]
    edges: tuple[EdgeShape, ...]


@dataclass(frozen=True)
class EventFact(_Checked):
    severity: Severity
    reason: ReasonRef
    node: Label | None
    counts_log2: tuple[tuple[CountKey, int], ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        keys = [k.value for k, _ in self.counts_log2]
        if keys != sorted(set(keys)):
            raise ValueError("counts_log2 keys must be unique and sorted")


@dataclass(frozen=True, kw_only=True)
class SupportBundle(_Checked):
    schema: BundleSchema = BundleSchema.V1
    versions: Versions
    limits: Limits
    routes: tuple[RouteFact, ...] | None
    timings: Timings | None
    errors: tuple[ErrorFact, ...] | None
    topology: Topology | None
    events: tuple[EventFact, ...] | None
    notes: tuple[ReasonRef, ...] = ()


_LEAVES = (int, bool, Label, Version, ReasonRef)
