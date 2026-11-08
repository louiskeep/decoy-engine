"""Public errors on the discovery and subset-seed failure paths carry no source value.

A canary corpus (an SSN-shaped string, an email, a float, a naive datetime, a 2 KB string) is
pushed through each failure path; no canary may reach `str(exc)`, `repr(exc)`, `exc.args`, the
rendered traceback, the cause/context chain or a logged record, and the message is the fixed
text for its failure class.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess
from decoy_engine.data_discovery import DiscoverySqlError, run_discovery_sql
from decoy_engine.fileaccess.adapters import duckdb as duckdb_adapter
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.subset._errors import SubsetConfigError
from decoy_engine.subset._keys import RI
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import Predicate, SeedSpec
from tests.fileaccess.support import RootRelative
from tests.fileaccess.x_support import rendered

SSN = "SSN-123-45-6789"
EMAIL = "canary.person@example.com"
FLOAT = 31337.4242
NAIVE = dt.datetime(1987, 6, 5, 4, 3, 2)
LONG = "CANARY2K" + "q" * 2040

NEEDLES = [
    SSN,
    "123-45-6789",
    EMAIL,
    "31337",
    "1987-06-05",
    "1987, 6, 5",
    "04:03:02",
    "CANARY2K",
    "qqqqqqqqqqqqqqqq",
]

JOB_SEED = bytes(range(32))

MSG_SQL_CONVERSION = (
    "SQL execution failed: a value could not be converted to the requested type, "
    "parsed, or was out of range."
)
MSG_SQL_NOT_EXECUTABLE = (
    "SQL execution failed: the statement could not be planned; check table names, "
    "column names, types and syntax."
)
MSG_SQL_FAILED = "SQL execution failed."


def assert_value_free(exc: BaseException, *, chained: bool = False) -> None:
    # `rendered` walks the cause/context chain, so a chained error is still checked for text.
    forms = rendered(exc) + [repr(a) for a in exc.args]
    for form in forms:
        for needle in NEEDLES:
            assert needle not in form, f"{needle!r} leaked in: {form[:300]!r}"
    if not chained:
        assert exc.__cause__ is None and exc.__context__ is None


# -- discovery -------------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    pq.write_table(
        pa.table(
            {
                "ssn": [SSN],
                "email": [EMAIL],
                "f": pa.array([FLOAT], pa.float64()),
                "ts": pa.array([NAIVE], pa.timestamp("us")),
                "big": [LONG],
            }
        ),
        root / "t.parquet",
    )
    return root


@contextmanager
def _mode(mode: str, root: Path) -> Iterator[None]:
    if mode == "ambient":
        yield
        return
    policy = RootRelative(root)
    try:
        with fileaccess.scope(policy):
            yield
    finally:
        policy.close()


CONVERSION_SQL = [
    "SELECT CAST(ssn AS INTEGER) FROM t",
    "SELECT CAST(email AS DATE) FROM t",
    "SELECT strptime(big, '%Y') FROM t",
    "SELECT CAST(f AS TINYINT) FROM t",
    "SELECT CAST(CAST(ts AS VARCHAR) AS INTEGER) FROM t",
    "SELECT * FROM t WHERE ssn = 1",
    "SELECT error(ssn) FROM t",
]


@pytest.mark.parametrize("mode", ["ambient", "strict"])
@pytest.mark.parametrize("sql", CONVERSION_SQL)
def test_discovery_conversion_failure_is_value_free(corpus: Path, mode: str, sql: str) -> None:
    with _mode(mode, corpus), pytest.raises(DiscoverySqlError) as info:
        run_discovery_sql(sql, {"t": str(corpus / "t.parquet")})
    assert str(info.value) == MSG_SQL_CONVERSION
    assert_value_free(info.value)


@pytest.mark.parametrize("mode", ["ambient", "strict"])
@pytest.mark.parametrize("sql", ["SELECT nope FROM t", "SELECT * FROM missing", "SELECT 1 +"])
def test_discovery_unplannable_statement_has_fixed_text(corpus: Path, mode: str, sql: str) -> None:
    with _mode(mode, corpus), pytest.raises(DiscoverySqlError) as info:
        run_discovery_sql(sql, {"t": str(corpus / "t.parquet")})
    assert str(info.value) == MSG_SQL_NOT_EXECUTABLE
    assert_value_free(info.value)


class _Relation:
    def __init__(self, error: Exception | None, at_description: Exception | None = None) -> None:
        self._error = error
        self._at_description = at_description

    @property
    def description(self) -> list[tuple[str]]:
        if self._at_description is not None:
            raise self._at_description
        return [("a",)]

    def fetchmany(self, n: int) -> list[tuple[int]]:
        if self._error is not None:
            raise self._error
        return [(1,)]


class _Connection:
    def __init__(
        self,
        at_execute: Exception | None,
        at_fetch: Exception | None,
        at_description: Exception | None = None,
    ) -> None:
        self._at_execute = at_execute
        self._at_fetch = at_fetch
        self._at_description = at_description

    def execute(self, sql: str) -> _Relation:
        if self._at_execute is not None:
            raise self._at_execute
        return _Relation(self._at_fetch, self._at_description)


@pytest.mark.parametrize("mode", ["ambient", "strict"])
@pytest.mark.parametrize("stage", ["execute", "fetch"])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (duckdb.ConversionException, MSG_SQL_CONVERSION),
        (duckdb.OutOfRangeException, MSG_SQL_CONVERSION),
        (duckdb.InvalidInputException, MSG_SQL_CONVERSION),
        (duckdb.BinderException, MSG_SQL_NOT_EXECUTABLE),
        (duckdb.InternalException, MSG_SQL_FAILED),
    ],
)
def test_discovery_library_text_never_forwarded(
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    stage: str,
    error: type[duckdb.Error],
    expected: str,
) -> None:
    # Errors are often deferred to the fetch on a large scan; both stages, both policies.
    text = f"Could not convert string '{SSN}' / {EMAIL} / {FLOAT!r} / {NAIVE!r} / {LONG}"
    exc = error(text)
    errors = (exc, None) if stage == "execute" else (None, exc)

    @contextmanager
    def session(handles: Any) -> Iterator[_Connection]:
        yield _Connection(*errors)

    monkeypatch.setattr(duckdb_adapter, "untrusted_session", session)
    with _mode(mode, corpus), pytest.raises(DiscoverySqlError) as info:
        run_discovery_sql("select 1", {"t": str(corpus / "t.parquet")})
    assert str(info.value) == expected
    assert_value_free(info.value)


def test_discovery_strict_memory_pressure_keeps_its_class(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def session(handles: Any) -> Iterator[_Connection]:
        yield _Connection(duckdb.OutOfMemoryException("oom"), None)

    monkeypatch.setattr(duckdb_adapter, "untrusted_session", session)
    with _mode("strict", corpus), pytest.raises(duckdb.OutOfMemoryException):
        run_discovery_sql("select 1", {"t": str(corpus / "t.parquet")})


# Failures a strict caller classifies by class: memory by type, memory by the text predicate,
# and the policy's own decisions. Ambient handlers keep the fixed-message rebuild.
_KEPT_CLASS = {
    "oom": lambda: duckdb.OutOfMemoryException(f"oom near {SSN}"),
    "memory_text": lambda: duckdb.Error("Unknown error: Wrapping 0xdead failed"),
    "denied": lambda: fileaccess.FileAccessDenied("discovery.tables.t"),
    "read_error": lambda: fileaccess.FileAccessReadError("discovery.tables.t"),
    "unavailable": lambda: fileaccess.FileAccessUnavailable("no policy"),
}
_STAGE_FIXED_TEXT = {
    "statement_kinds": MSG_SQL_NOT_EXECUTABLE,
    "execute": MSG_SQL_FAILED,
    "description": MSG_SQL_FAILED,
    "fetch": MSG_SQL_FAILED,
}


def _inject(monkeypatch: pytest.MonkeyPatch, stage: str, exc: Exception) -> None:
    if stage == "statement_kinds":

        def kinds(sql: str) -> list[str]:
            raise exc

        monkeypatch.setattr(duckdb_adapter, "statement_kinds", kinds)
        return
    at = {
        "execute": (exc, None, None),
        "fetch": (None, exc, None),
        "description": (None, None, exc),
    }

    @contextmanager
    def session(handles: Any) -> Iterator[_Connection]:
        yield _Connection(*at[stage])

    monkeypatch.setattr(duckdb_adapter, "untrusted_session", session)


@pytest.mark.parametrize("stage", list(_STAGE_FIXED_TEXT))
@pytest.mark.parametrize("kind", list(_KEPT_CLASS))
def test_discovery_strict_keeps_memory_and_policy_class_at_every_stage(
    corpus: Path, monkeypatch: pytest.MonkeyPatch, stage: str, kind: str
) -> None:
    exc = _KEPT_CLASS[kind]()
    _inject(monkeypatch, stage, exc)
    with _mode("strict", corpus), pytest.raises(type(exc)) as info:
        run_discovery_sql("select 1", {"t": str(corpus / "t.parquet")})
    assert info.value is exc


@pytest.mark.parametrize("stage", list(_STAGE_FIXED_TEXT))
@pytest.mark.parametrize("kind", list(_KEPT_CLASS))
def test_discovery_ambient_rebuilds_library_failures_at_every_stage(
    corpus: Path, monkeypatch: pytest.MonkeyPatch, stage: str, kind: str
) -> None:
    exc = _KEPT_CLASS[kind]()
    _inject(monkeypatch, stage, exc)
    if not isinstance(exc, duckdb.Error):
        # A policy decision is never a library failure: no handler catches it.
        with _mode("ambient", corpus), pytest.raises(type(exc)) as kept:
            run_discovery_sql("select 1", {"t": str(corpus / "t.parquet")})
        assert kept.value is exc
        return
    with _mode("ambient", corpus), pytest.raises(DiscoverySqlError) as info:
        run_discovery_sql("select 1", {"t": str(corpus / "t.parquet")})
    assert str(info.value) == _STAGE_FIXED_TEXT[stage]
    assert_value_free(info.value)


# -- subset seed -----------------------------------------------------------------------------


def _kf(series: pl.Series) -> pl.DataFrame:
    return pl.DataFrame([series]).with_row_index(RI)


def _keys_error(series: pl.Series, keys: tuple[tuple[Any, ...], ...]) -> SubsetConfigError:
    spec = SeedSpec(table="t", mode="keys", key_columns=("id",), keys=keys)
    with pytest.raises(SubsetConfigError) as info:
        select_seed_rows(seeds=(spec,), key_frames={"t": _kf(series)}, job_seed=JOB_SEED)
    return info.value


INT_KEY = pl.Series("id", [1, 2], dtype=pl.Int64)
BOOL_KEY = pl.Series("id", [True, False])


@pytest.mark.parametrize(
    ("series", "keys"),
    [
        (INT_KEY, ((SSN,),)),
        (INT_KEY, ((EMAIL,),)),
        (INT_KEY, ((LONG,),)),
        (INT_KEY, ((1,), (SSN,))),
        (INT_KEY, ((FLOAT,), ("x",))),
        (INT_KEY, ((NAIVE,), ("x",))),
        (BOOL_KEY, ((NAIVE,),)),
        (BOOL_KEY, ((SSN,),)),
    ],
    ids=["ssn", "email", "long", "mixed-ssn", "float", "datetime", "datetime-bool", "ssn-bool"],
)
def test_subset_explicit_key_mismatch_is_value_free(
    series: pl.Series, keys: tuple[tuple[Any, ...], ...]
) -> None:
    exc = _keys_error(series, keys)
    assert exc.code == "subset_seed_key_type"
    label = "boolean" if series.dtype == pl.Boolean else "integer"
    assert exc.message == (
        "table 't': explicit seed key(s) for key column 'id' "
        f"do not convert exactly to the column's {label} type"
    )
    assert_value_free(exc)


@pytest.mark.parametrize(
    ("op", "value"),
    [
        ("eq", SSN),
        ("eq", EMAIL),
        ("gt", LONG),
        ("eq", NAIVE),
        ("in", (1, SSN)),
        ("in", (NAIVE,)),
    ],
    ids=["eq-ssn", "eq-email", "gt-long", "eq-datetime", "in-ssn", "in-datetime"],
)
def test_subset_filter_literal_mismatch_is_value_free(op: str, value: Any) -> None:
    # The literal check catches the conversion error; the refusal must not chain it, since
    # polars and TypeError text quote the literal.
    spec = SeedSpec(
        table="t", mode="filter", predicates=(Predicate(column="id", op=op, value=value),)
    )
    with pytest.raises(SubsetConfigError) as info:
        select_seed_rows(seeds=(spec,), key_frames={"t": _kf(INT_KEY)}, job_seed=JOB_SEED)
    assert info.value.code == "subset_seed_key_type"
    assert_value_free(info.value)


@pytest.mark.parametrize(
    ("series", "reason"),
    [
        (pl.Series("id", [FLOAT, 2.5]), "float values are not supported"),
        (pl.Series("id", [NAIVE, NAIVE]), "timezone-naive datetimes are not supported"),
    ],
    ids=["float", "datetime"],
)
def test_subset_sample_uncanonicalizable_key_is_value_free(series: pl.Series, reason: str) -> None:
    kf = pl.DataFrame([pl.Series("ok", ["a", "b"]), series]).with_row_index(RI)
    spec = SeedSpec(table="t", mode="sample", key_columns=("ok", "id"), count=1)
    with pytest.raises(SubsetConfigError) as info:
        select_seed_rows(seeds=(spec,), key_frames={"t": kf}, job_seed=JOB_SEED)
    exc = info.value
    assert exc.code == "subset_seed_key_uncanonicalizable"
    assert exc.message == (
        f"table 't': seed key column 'id' holds a value that cannot be canonicalized "
        f"for sampling: {reason}"
    )
    assert_value_free(exc)


@pytest.mark.parametrize("value", [FLOAT, NAIVE], ids=["float", "datetime"])
def test_canonicalize_source_error_is_value_free(value: Any) -> None:
    with pytest.raises(GenerationError) as info:
        _canonicalize_source(value)
    assert_value_free(info.value)


def test_canary_corpus_is_the_stated_size() -> None:
    assert len(LONG.encode()) == 2048


# -- derived expressions (class sweep) -------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "row", "reason"),
    [
        ('days_between(dob, "2024-01-01")', {"dob": SSN}, "an operand value is invalid"),
        ('days_between(dob, "2024-01-01")', {"dob": LONG}, "an operand value is invalid"),
        ("slice(ssn, off)", {"ssn": "abc", "off": EMAIL}, "an argument is not supported"),
        ("slice(ssn, off)", {"ssn": "abc", "off": FLOAT}, "an argument is not supported"),
        ("a / b", {"a": 1, "b": 0}, "division by zero"),
    ],
    ids=["date-ssn", "date-long", "slice-email", "slice-float", "div-zero"],
)
def test_derived_evaluation_failure_is_value_free(
    expression: str, row: dict[str, Any], reason: str
) -> None:
    from decoy_engine.transforms.derived import DerivedConfig, apply_derived

    cfg = DerivedConfig.from_dict({"expression": expression, "null_propagation": "default"})
    with pytest.raises(ValueError) as info:
        apply_derived(cfg, row, column="computed_col", row_index=7)
    assert str(info.value) == (
        f"derived expression evaluation failed at column 'computed_col', row 7: {reason}"
    )
    assert_value_free(info.value)


@pytest.mark.parametrize("value", [EMAIL, FLOAT, NAIVE, LONG], ids=["email", "float", "dt", "long"])
def test_slice_argument_error_names_type_not_value(value: Any) -> None:
    from decoy_engine.errors import ValidationError
    from decoy_engine.expressions import compile_expr, evaluate

    with pytest.raises(ValidationError) as info:
        evaluate(compile_expr("slice(ssn, off)"), {"ssn": "abc", "off": value})
    assert str(info.value) == (
        "Validation error: unsupported expression: slice() 'start' argument must be an int, "
        f"got {type(value).__name__}. slice(s, start[, end]) requires integer indices."
    )
    # The evaluator re-raises lark's unwrapped error with its chain; the text is what is fixed.
    assert_value_free(info.value, chained=True)
