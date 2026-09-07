"""engine-v2 S12 strategy substrate parity: pandas adapter vs polars adapter.

The migration gate (substrate-decision doc): for every migrated strategy, the
v2 PANDAS execution adapter and the v2 POLARS execution adapter must produce
semantically-equal `outputs` for the same `(plan, sources)`. This is the v2
EXECUTION-path harness, distinct from the V1 graph-engine pandas-vs-duckdb
harness (`test_relational_ops_parity.py` / `test_source_sink_parity.py`).

Accepted differences (see SEMANTIC_DIFFERENCES.md, v2 section): the comparison is
value-level (`to_pydict()`), so Arrow type-width drift introduced by the
pa -> pl -> pa boundary (string vs large_string, etc.) is accepted as long as the
logical values + null positions match. Each migrated strategy adds a case here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter, PolarsExecutionAdapter
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    deterministic: bool = False,
    provider: str = "x_nobackend",
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(table: str, columns: tuple[tuple[str, ColumnSeed], ...]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=((table, TableSeed(per_column=columns, per_group=())),),
        )
    )


def assert_frames_semantically_equal(pandas_table: pa.Table, polars_table: pa.Table) -> None:
    """Value-level equality: same columns, same per-column values + null positions.

    Arrow type-width drift (string vs large_string, etc.) is accepted; only the
    logical values are compared (SEMANTIC_DIFFERENCES.md v2 section).
    """
    assert polars_table.column_names == pandas_table.column_names
    assert polars_table.to_pydict() == pandas_table.to_pydict()


def _both(plan: Any, sources: dict[str, pa.Table]) -> tuple[pa.Table, pa.Table]:
    pandas_res = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    polars_res = PolarsExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    (table,) = sources
    return pandas_res.outputs[table], polars_res.outputs[table]


# Each entry: (id, plan, sources). Cheap band migrated in S12; medium/expensive
# bands append here as they migrate.
_SMALL = ["a@b.com", None, "carol@example.org", "", "dave@x.io"]
_NUMS = ["12345", "67890", None, "00001", "99999"]
_MEDIUM = [f"user{i:04d}@mail.test" for i in range(1000)]

_CASES: list[tuple[str, Any, dict[str, pa.Table]]] = [
    (
        "passthrough-small",
        _plan("t", (("c", _col("passthrough")),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "redact-small-with-nulls",
        _plan("t", (("c", _col("redact")),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "redact-custom-value",
        _plan("t", (("c", _col("redact", provider_config=(("redact_with", "X"),))),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "truncate-from-start",
        _plan("t", (("c", _col("truncate", provider_config=(("length", 3),))),)),
        {"t": pa.table({"c": _NUMS})},
    ),
    (
        "truncate-from-end",
        _plan(
            "t",
            (("c", _col("truncate", provider_config=(("length", 2), ("from_end", True)))),),
        ),
        {"t": pa.table({"c": _NUMS})},
    ),
    (
        # Sprint 13 / coercion-13 S3 (2026-07-03): the valid keep + mask_char
        # path must stay byte-identical across both substrates after the
        # fail-closed fix (truncate_length_invalid / truncate_keep_invalid /
        # truncate_mask_char_invalid only fire on invalid config; this case
        # proves the VALID config path is untouched by that change).
        "truncate-keep-tail-mask-char",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "truncate",
                        provider_config=(("length", 4), ("keep", "tail"), ("mask_char", "*")),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": ["1234567890", None, "abc"]})},
    ),
    (
        "shuffle-deterministic-small",
        _plan("t", (("c", _col("shuffle", namespace="s_ns", deterministic=True)),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "shuffle-deterministic-medium",
        _plan("t", (("c", _col("shuffle", namespace="s_ns", deterministic=True)),)),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        "redact-medium",
        _plan("t", (("c", _col("redact")),)),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        "hash-string-with-nulls",
        _plan("t", (("c", _col("hash", namespace="h_ns")),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "hash-truncated",
        _plan("t", (("c", _col("hash", namespace="h_ns", provider_config=(("truncate", 8),))),)),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        # Null-free int column: to_pandas keeps int64 (an int64+null column would
        # widen to float64 on the pandas side and hard-error in canonicalization,
        # a pandas-oracle limitation, not a substrate divergence).
        "hash-int-column",
        _plan("t", (("c", _col("hash", namespace="h_ns")),)),
        {"t": pa.table({"c": pa.array([1, 22, 333, 4444, 5], type=pa.int64())})},
    ),
    (
        "hash-medium",
        _plan("t", (("c", _col("hash", namespace="h_ns")),)),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        "categorical-deterministic",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "categorical",
                        namespace="cat_ns",
                        deterministic=True,
                        provider_config=(("categories", ["alpha", "beta", "gamma"]),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": _SMALL})},
    ),
    (
        "categorical-medium",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "categorical",
                        namespace="cat_ns",
                        deterministic=True,
                        provider_config=(("categories", ["A", "B", "C", "D"]),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        # Weighted deterministic path: the polars handler ignored `weights`
        # entirely until 2026-06-12 (it predated the MG-1 S5 extension), so
        # weighted categoricals silently diverged across substrates. The CDF
        # is now imported from the pandas module; this case pins the parity.
        "categorical-weighted-deterministic",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "categorical",
                        namespace="cat_ns",
                        deterministic=True,
                        provider_config=(
                            ("categories", ["A", "B", "C", "D"]),
                            ("weights", [0.5, 0.3, 0.15, 0.05]),
                        ),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": _MEDIUM})},
    ),
    (
        "date_shift-iso-dates",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "date_shift",
                        namespace="ds_ns",
                        provider_config=(("min_days", -30), ("max_days", 30)),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": ["2020-01-15", "2019-06-30", None, "2021-12-01", "2018-03-20"]})},
    ),
    (
        # HC-3a: both substrates anchor date_shift on the PRE-MASK group column
        # read from ctx.group_anchor_snapshots (Codex R1 P1 #1). The polars
        # route builds that snapshot in _run_polars_native, so an entity-anchored
        # shift is byte-identical to pandas without the port ever seeing a live
        # (possibly-masked) sibling.
        "date_shift-group-by-entity-anchored",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "date_shift",
                        namespace="ds_group_ns",
                        provider_config=(
                            ("min_days", -30),
                            ("max_days", 30),
                            ("group_by", "patient_id"),
                        ),
                    ),
                ),
                ("patient_id", _col("passthrough")),
            ),
        ),
        {
            "t": pa.table(
                {
                    # Last row: a NULL patient_id with a VALID date exercises the
                    # null-group self-anchor branch across substrates (it reduces
                    # to the date-anchor path, which must stay pandas==polars).
                    "c": [
                        "2020-01-15",
                        "2020-02-20",
                        None,
                        "2019-06-30",
                        "2019-07-15",
                        "2021-03-10",
                    ],
                    "patient_id": ["p1", "p1", "p1", "p2", "p2", None],
                }
            )
        },
    ),
    (
        # HC-3a (Codex R1 P1 #2): an INT+null group column must stay lossless on
        # BOTH substrates -- pandas via to_pandas_fk_safe, polars via the
        # pyarrow-extension snapshot -- so a valid id never widens to float64
        # (which _canonicalize_source rejects). The null row self-anchors.
        "date_shift-group-by-nullable-int",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "date_shift",
                        namespace="ds_int_group_ns",
                        provider_config=(
                            ("min_days", -30),
                            ("max_days", 30),
                            ("group_by", "patient_id"),
                        ),
                    ),
                ),
                ("patient_id", _col("passthrough")),
            ),
        ),
        {
            "t": pa.table(
                {
                    "c": ["2020-01-15", "2020-02-20", "2019-06-30", None, "2019-07-15"],
                    "patient_id": pa.array([1, 1, 2, 2, None], type=pa.int64()),
                }
            )
        },
    ),
    (
        "bucketize-lower-int",
        _plan("t", (("c", _col("bucketize", provider_config=(("width", 10),))),)),
        {"t": pa.table({"c": pa.array([3, 17, 42, None, 99], type=pa.int64())})},
    ),
    (
        "bucketize-range-format",
        _plan(
            "t",
            (("c", _col("bucketize", provider_config=(("width", 10), ("format", "range")))),),
        ),
        {"t": pa.table({"c": pa.array([3, 17, 42, 99], type=pa.int64())})},
    ),
    (
        "fpe-digits",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "fpe",
                        namespace="fpe_ns",
                        deterministic=True,
                        provider="fpe",
                        provider_config=(("charset", "digits"),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": [f"{i:05d}" for i in range(20)]})},
    ),
    (
        "faker-person-email-deterministic",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "faker",
                        namespace="people_ns",
                        deterministic=True,
                        provider="person_email",
                        provider_config=(("pool_size", 256),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": ["a", "b", "a", None, "c"]})},
    ),
    (
        "formula-upper",
        _plan("t", (("c", _col("formula", provider_config=(("formula", "value.upper()"),))),)),
        {"t": pa.table({"c": ["alpha", "beta", None, "delta"]})},
    ),
    (
        "formula-fstring",
        _plan("t", (("c", _col("formula", provider_config=(("formula", "f'USER-{value}'"),))),)),
        {"t": pa.table({"c": ["alpha", "beta", "gamma"]})},
    ),
    # MG-2 (2026-05-31): text_redact rides the PandasStrategyPort on the
    # polars side, so substrate parity is guaranteed by construction. The
    # parity case locks the wiring against a future polars-native impl.
    (
        "text_redact-default-token",
        _plan("t", (("c", _col("text_redact")),)),
        {
            "t": pa.table(
                {
                    "c": [
                        "Contact alice@example.com please.",
                        None,
                        "SSN 123-45-6789 on file.",
                        "Just prose, no PII.",
                    ]
                }
            )
        },
    ),
    (
        "text_redact-label-token",
        _plan(
            "t",
            (("c", _col("text_redact", provider_config=(("label_token", True),))),),
        ),
        {"t": pa.table({"c": ["alice@example.com and 123-45-6789"]})},
    ),
    # MG-3 / M3 (2026-05-31): conditional `when:` gate. The polars
    # adapter converts to pandas just for the predicate eval, then
    # writes back; the pandas adapter evaluates natively. Byte-
    # identical output by construction.
    (
        "when_byte_identical_pandas_vs_polars_port",
        _plan(
            "t",
            (
                (
                    "v",
                    ColumnSeed(
                        namespace=None,
                        strategy="redact",
                        provider="x_nobackend",
                        backend_type="faker",
                        backend_version="v",
                        cardinality_mode="reuse",
                        deterministic=False,
                        provider_config=(),
                        coherent_with=(),
                        when="flag == 1",
                    ),
                ),
            ),
        ),
        {
            "t": pa.table(
                {
                    "v": ["a", "b", "c", "d"],
                    "flag": pa.array([0, 1, 1, 0], type=pa.int64()),
                }
            )
        },
    ),
    # MG-3 / M2 (2026-05-31): nested rides PandasStrategyPort on the
    # polars side. Parity by construction.
    (
        "nested_redact_byte_identical_pandas_vs_polars_port",
        _plan(
            "t",
            (
                (
                    "data",
                    _col(
                        "nested",
                        provider_config=(
                            ("strategy", "redact"),
                            ("target", "$.user.email"),
                        ),
                    ),
                ),
            ),
        ),
        {
            "t": pa.table(
                {
                    "data": [
                        '{"user": {"name": "Alice", "email": "alice@x.com"}}',
                        '{"user": {"name": "Bob", "email": "bob@x.com"}}',
                    ]
                }
            )
        },
    ),
]


@pytest.mark.parametrize("plan,sources", [(c[1], c[2]) for c in _CASES], ids=[c[0] for c in _CASES])
def test_strategy_parity(plan: Any, sources: dict[str, pa.Table]) -> None:
    pandas_out, polars_out = _both(plan, sources)
    assert_frames_semantically_equal(pandas_out, polars_out)


# --------------------------------------------------------------------------
# 2026-09-02 polars-hash-kernel-parity plan: `hash` cross-adapter parity for
# the full common key-dtype matrix. The Polars handler used to feed
# `pl.Series.to_list()` (a us-truncated stdlib `datetime` for sub-us
# timestamps) into the shared canonicalizer while the pandas handler fed
# `pa.array(..., from_pandas=True).to_pylist()` (an ns-preserving
# `pandas.Timestamp`), so the two hashed different bytes for the identical
# logical value. The fix routes the Polars handler through the same
# `source.to_arrow()` -> `hash_array` path pandas already uses. The
# `timestamp[ns, tz=UTC]` case carries NONZERO sub-microsecond digits: it is
# the exact input that diverged before the fix and must match after it.
#
# One null-free row per dtype avoids the unrelated null-bearing-int rejection
# (see `_INT_NULL_REJECTED` below); this matrix is about VALUE parity for
# admitted inputs, not the null-int guard.
# --------------------------------------------------------------------------

_NS_SUBSECOND = pd.Timestamp("2020-01-01T00:00:00.123456789", tz="UTC")

_HASH_COMMON_DTYPE_CASES: list[tuple[str, pa.Array]] = [
    ("string", pa.array(["alice", "bob", None], type=pa.string())),
    ("large_string", pa.array(["alice", "bob", None], type=pa.large_string())),
    ("int8", pa.array([1, -2, 3], type=pa.int8())),
    ("uint8", pa.array([1, 2, 3], type=pa.uint8())),
    ("int16", pa.array([1, -2, 3], type=pa.int16())),
    ("uint16", pa.array([1, 2, 3], type=pa.uint16())),
    ("int32", pa.array([1, -2, 3], type=pa.int32())),
    ("uint32", pa.array([1, 2, 3], type=pa.uint32())),
    ("int64", pa.array([1, -2, 3], type=pa.int64())),
    ("uint64", pa.array([1, 2, 3], type=pa.uint64())),
    ("bool", pa.array([True, False, None], type=pa.bool_())),
    ("date32", pa.array([dt.date(2020, 1, 1), None], type=pa.date32())),
    ("timestamp_s_utc", pa.array([_NS_SUBSECOND, None], type=pa.timestamp("s", tz="UTC"))),
    ("timestamp_ms_utc", pa.array([_NS_SUBSECOND, None], type=pa.timestamp("ms", tz="UTC"))),
    ("timestamp_us_utc", pa.array([_NS_SUBSECOND, None], type=pa.timestamp("us", tz="UTC"))),
    (
        # THE regression: fails pre-fix (us-truncated `to_list()` output),
        # passes post-fix (`to_arrow()` preserves the ns digits).
        "timestamp_ns_utc_subsecond",
        pa.array([_NS_SUBSECOND, None], type=pa.timestamp("ns", tz="UTC")),
    ),
    ("decimal32", pa.array([Decimal("123.45"), None], type=pa.decimal32(9, 2))),
    ("decimal64", pa.array([Decimal("123.45"), None], type=pa.decimal64(15, 2))),
    ("decimal128", pa.array([Decimal("123.45"), None], type=pa.decimal128(20, 2))),
]


@pytest.mark.parametrize(
    "source", [c[1] for c in _HASH_COMMON_DTYPE_CASES], ids=[c[0] for c in _HASH_COMMON_DTYPE_CASES]
)
def test_hash_cross_adapter_common_dtype_matrix(source: pa.Array) -> None:
    pandas_out, polars_out = _both(
        _plan("t", (("c", _col("hash", namespace="h_ns")),)), {"t": pa.table({"c": source})}
    )
    assert_frames_semantically_equal(pandas_out, polars_out)


def test_hash_timezone_robustness_same_instant_utc_and_iana_non_utc() -> None:
    """The same instant expressed as UTC and as an IANA non-UTC zone must hash
    identically on each adapter (the canonicalizer normalizes via
    `.astimezone(utc)`), and the two adapters must agree with each other."""
    instant = pd.Timestamp("2020-06-15T12:00:00", tz="UTC")
    utc_source = pa.array([instant], type=pa.timestamp("us", tz="UTC"))
    ny_source = pa.array(
        [instant.tz_convert("America/New_York")], type=pa.timestamp("us", tz="America/New_York")
    )

    plan = _plan("t", (("c", _col("hash", namespace="h_ns")),))
    pandas_utc, polars_utc = _both(plan, {"t": pa.table({"c": utc_source})})
    pandas_ny, polars_ny = _both(plan, {"t": pa.table({"c": ny_source})})

    assert pandas_utc.to_pydict() == polars_utc.to_pydict()
    assert pandas_ny.to_pydict() == polars_ny.to_pydict()
    assert pandas_utc.to_pydict() == pandas_ny.to_pydict()


def test_hash_nan_and_none_fold_to_null_identically() -> None:
    """`[NaN, None]` hashes to `[None, None]` on both adapters (`hash_array`'s
    `_is_missing` folds NaN AND None, unlike the pre-fix Polars loop's
    `value is None`-only check)."""
    source = pa.array([float("nan"), None], type=pa.float64())
    pandas_out, polars_out = _both(
        _plan("t", (("c", _col("hash", namespace="h_ns")),)), {"t": pa.table({"c": source})}
    )
    assert pandas_out.to_pydict() == {"c": [None, None]}
    assert_frames_semantically_equal(pandas_out, polars_out)


@pytest.mark.parametrize(
    "source,expected_code",
    [
        (pa.array([1.5, 2.5], type=pa.float64()), "float_canonicalization_unsupported"),
        (
            pa.array([dt.datetime(2020, 1, 1)], type=pa.timestamp("us")),
            "timezone_naive_datetime",
        ),
    ],
    ids=["finite-float-key", "tz-naive-datetime-key"],
)
def test_hash_fail_closed_parity_both_substrates(source: pa.Array, expected_code: str) -> None:
    """A finite float and a tz-naive datetime are both unhashable identifiers
    (S5 spec's float/naive-datetime hard errors); neither adapter may silently
    mask them, and both must fail with the SAME typed error."""
    plan = _plan("t", (("c", _col("hash", namespace="h_ns")),))
    sources = {"t": pa.table({"c": source})}
    for adapter in (PandasExecutionAdapter(), PolarsExecutionAdapter()):
        with pytest.raises(GenerationError) as exc:
            adapter.run(
                plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
            )
        assert exc.value.code == expected_code


# --------------------------------------------------------------------------
# DENNIS S12 review (Session 49): null-bearing INTEGER source divergence.
#
# `to_pandas()` widens an int64+null column to float64 on the pandas-oracle side;
# the polars-native path keeps int64. The same job therefore diverges ACROSS
# SUBSTRATES on a null-bearing int column, and the difference is NOT in the
# accepted-differences list (it is a VALUE / BEHAVIOR difference, not an Arrow
# type-width difference):
#
#   - truncate: pandas stringifies the widened float ("100.0" -> "100." at len 4);
#     polars stringifies the int ("100"). DIFFERENT MASKED VALUES.
#   - hash / categorical (deterministic): pandas HARD-ERRORS
#     (float_canonicalization_unsupported, the S5 PO-lock); polars-native SUCCEEDS
#     because the int never widened. So a job that errors under pandas produces
#     output under polars. At S13 (polars default, fallback removed) this becomes
#     the shipped behavior.
#
# B1 RESOLVED (PO-settled 2026-05-28): reject at validation. The divergence is
# scoped out by rejecting this input class on BOTH substrates (the plan-compile
# check `null_bearing_int_unsupported` + the execution-time guard
# `reject_null_bearing_int`). So these cases are no longer xfail-divergence: each
# now asserts BOTH adapters raise the SAME typed ExecutionError, identically. The
# divergence can never silently cross the line because neither substrate produces
# output for it. Consistent with the S5 float-canonicalization hard error.
# --------------------------------------------------------------------------

_INT_NULL_REJECTED: list[tuple[str, Any, dict[str, pa.Table]]] = [
    (
        "truncate-int-null-VALUE-DIVERGENCE",
        _plan("t", (("c", _col("truncate", provider_config=(("length", 4),))),)),
        {"t": pa.table({"c": pa.array([100, 200, None], type=pa.int64())})},
    ),
    (
        "hash-int-null-BEHAVIOR-DIVERGENCE",
        _plan("t", (("c", _col("hash", namespace="h_ns")),)),
        {"t": pa.table({"c": pa.array([1, 2, None], type=pa.int64())})},
    ),
    (
        "categorical-int-null-BEHAVIOR-DIVERGENCE",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "categorical",
                        namespace="cat_ns",
                        deterministic=True,
                        provider_config=(("categories", ["A", "B", "C"]),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": pa.array([1, 2, None], type=pa.int64())})},
    ),
]


@pytest.mark.parametrize(
    "plan,sources",
    [(c[1], c[2]) for c in _INT_NULL_REJECTED],
    ids=[c[0] for c in _INT_NULL_REJECTED],
)
def test_strategy_int_null_rejected_both_substrates(
    plan: Any, sources: dict[str, pa.Table]
) -> None:
    # B1 (S13): both adapters reject this input class identically (same typed
    # error), so neither silently diverges. No xfail: the rejection is the
    # contract, asserted on both substrates.
    for adapter in (PandasExecutionAdapter(), PolarsExecutionAdapter()):
        with pytest.raises(ExecutionError) as exc:
            adapter.run(
                plan,
                sources,
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NS,
            )
        assert exc.value.code == "null_bearing_int_unsupported"


# --------------------------------------------------------------------------
# Sprint 13 / coercion-13 S3 (2026-07-03): fail-closed handler backstop parity.
#
# truncate/bucketize/categorical each had a silent-passthrough or silent-
# corruption exit on a bad config (finding 0.4 + GATE-1 Q4 siblings). Both the
# pandas AND polars handlers now raise the SAME typed StrategyError on the
# same invalid config, mirroring the null_bearing_int pattern above: the
# handler-level backstop is defense-in-depth (check_truncate_config /
# check_bucketize_config / check_categorical_categories already reject these
# shapes at plan-compile), so this proves neither substrate can be reached
# with an unvalidated config that would silently leak or corrupt.
# --------------------------------------------------------------------------

_FAIL_CLOSED_BOTH_SUBSTRATES: list[tuple[str, Any, dict[str, pa.Table], str]] = [
    (
        "truncate-invalid-length-both-substrates",
        _plan("t", (("c", _col("truncate", provider_config=(("length", 0),))),)),
        {"t": pa.table({"c": ["a", "b"]})},
        "truncate_length_invalid",
    ),
    (
        "bucketize-unresolvable-width-both-substrates",
        _plan("t", (("c", _col("bucketize", provider_config=(("width", 0),))),)),
        {"t": pa.table({"c": pa.array([1, 2], type=pa.int64())})},
        "bucketize_width_unresolvable",
    ),
    (
        "categorical-string-categories-both-substrates",
        _plan(
            "t",
            (
                (
                    "c",
                    _col(
                        "categorical",
                        namespace="cat_ns",
                        deterministic=True,
                        provider_config=(("categories", "gold,silver"),),
                    ),
                ),
            ),
        ),
        {"t": pa.table({"c": ["a", "b"]})},
        "categorical_categories_not_list",
    ),
]


@pytest.mark.parametrize(
    "plan,sources,expected_code",
    [(c[1], c[2], c[3]) for c in _FAIL_CLOSED_BOTH_SUBSTRATES],
    ids=[c[0] for c in _FAIL_CLOSED_BOTH_SUBSTRATES],
)
def test_fail_closed_sibling_leaks_both_substrates(
    plan: Any, sources: dict[str, pa.Table], expected_code: str
) -> None:
    for adapter in (PandasExecutionAdapter(), PolarsExecutionAdapter()):
        with pytest.raises(ExecutionError) as exc:
            adapter.run(
                plan,
                sources,
                registry=_REG,
                relationship_graph=_GRAPH,
                namespace_registry=_NS,
            )
        assert exc.value.code == expected_code


# --------------------------------------------------------------------------
# FK / orphan-REMAP parity (S11 review M3). The FK path is the most
# dtype-sensitive in the engine (the int/float null-dtype split that to_pandas
# introduces on a null-bearing child FK column). At S12 the polars adapter runs
# FK jobs via the pandas oracle (the round-trip), so this proves the round-trip
# preserves FK resolution + orphan policy identically to a direct pandas run,
# rather than assuming it (the S11 review's "I expect" -> "the suite proves").
# --------------------------------------------------------------------------


def _hash_col(namespace: str) -> ColumnSeed:
    return _col("hash", namespace=namespace, deterministic=True, provider="hash")


def _fk_plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
                (
                    "orders",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
            ),
        )
    )


def _fk_graph(policy: OrphanPolicy) -> RelationshipGraph:
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _both_multi(
    plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph
) -> tuple[dict[str, pa.Table], dict[str, pa.Table]]:
    pandas_res = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    polars_res = PolarsExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    return pandas_res.outputs, polars_res.outputs


_FK_CASES: list[tuple[str, dict[str, pa.Table], OrphanPolicy]] = [
    (
        "fk-null-bearing-child-preserve",
        {
            "customers": pa.table({"customer_id": ["c1", "c2", "c3"]}),
            "orders": pa.table({"customer_id": ["c1", None, "c2", "c1"]}),
        },
        OrphanPolicy.PRESERVE,
    ),
    (
        "fk-orphan-remap",
        {
            "customers": pa.table({"customer_id": ["c1", "c2", "c3"]}),
            "orders": pa.table({"customer_id": ["c1", "c2", "c9", None]}),  # c9 orphan
        },
        OrphanPolicy.REMAP,
    ),
    (
        "fk-int-null-child",  # to_pandas widens the null child to float64
        {
            "customers": pa.table({"customer_id": pa.array([1, 2, 3], type=pa.int64())}),
            "orders": pa.table({"customer_id": pa.array([1, None, 2], type=pa.int64())}),
        },
        OrphanPolicy.PRESERVE,
    ),
]


@pytest.mark.parametrize(
    "sources,policy", [(c[1], c[2]) for c in _FK_CASES], ids=[c[0] for c in _FK_CASES]
)
def test_fk_parity(sources: dict[str, pa.Table], policy: OrphanPolicy) -> None:
    pandas_out, polars_out = _both_multi(_fk_plan(), sources, _fk_graph(policy))
    assert set(polars_out) == set(pandas_out)
    for table in pandas_out:
        assert_frames_semantically_equal(pandas_out[table], polars_out[table])
