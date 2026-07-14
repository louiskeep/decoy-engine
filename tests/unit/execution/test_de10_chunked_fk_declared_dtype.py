"""DE-10 residual (LOW): the chunked FK gate's declared dtype is a TRUSTED
assertion -- validate it against the real data and fail closed on a mismatch.

`gate_fk_child_edges` condition (f) (`execution/_chunked_fk.py`) admits a
value-sensitive FK edge onto the chunked self-masking route purely on the
operator-DECLARED `dtype` strings (it requires both sides declared and of the
same family). The chunked route masks the child's OWN value with no parent-map
lookup and never sees the parent's data, so at compile time the declaration is
believed, never checked. A MISdeclaration -- `dtype: "int64"` on FK key columns
whose real data is `string`/`float` -- passes that compile-time gate yet makes
the child self-mask a different byte sequence than the parent (masked elsewhere
from its own real dtype) for the same logical key, silently voiding referential
integrity.

Every OTHER route validates the FK key dtype off the REAL Arrow data
(`_fk_keys.to_pandas_fk_safe`, `out_of_core/_join.cast_fk_chunk`) and fails
closed. This closes the same gap on the chunked route: a per-chunk guard
(`reject_mismatched_chunked_fk_declared_dtype`) compares each declared FK key
dtype family against the chunk's real Arrow dtype family and raises
`ExecutionError(code="chunked_fk_declared_dtype_mismatch")` on a cross-family
disagreement, while width-only (int32 vs int64) and dictionary-encoded columns
are admitted without false positives.
"""

from __future__ import annotations

import decimal

import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked_fk_dtype import (
    fk_declared_dtypes_for_table,
    reject_mismatched_chunked_fk_declared_dtype,
)

_ENGINE = "test-de10-declared-dtype"


def _fk_config(
    *,
    strategy: str = "passthrough",
    parent_dtype: str | None = "int64",
    child_dtype: str | None = "int64",
    namespace: str | None = None,
) -> dict:
    """customers(id) -> orders(customer_id) FK config, both columns the same
    value-sensitive `strategy`, REMAP orphan policy -- the shape the chunked
    gate admits. `namespace` is required only for the namespace-requiring
    strategies (hash/fpe/date_shift); passthrough/truncate are namespace-
    agnostic. `parent_dtype`/`child_dtype` set the DECLARED dtype the gate
    trusts (condition (f))."""
    parent_col: dict = {"name": "id", "strategy": strategy}
    child_col: dict = {"name": "customer_id", "strategy": strategy}
    if parent_dtype is not None:
        parent_col["dtype"] = parent_dtype
    if child_dtype is not None:
        child_col["dtype"] = child_dtype
    if namespace is not None:
        parent_col["namespace"] = namespace
        child_col["namespace"] = namespace
    return {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [parent_col]},
            {"name": "orders", "columns": [child_col]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "remap",
            }
        ],
    }


# ---------------------------------------------------------------------------
# End-to-end: a real chunked run must fail closed on a misdeclared FK dtype.
# ---------------------------------------------------------------------------


def test_declared_int_but_real_string_fk_fails_closed() -> None:
    """THE gap: the config declares `dtype: "int64"` on the FK key columns (so
    the compile-time gate ADMITS the edge -- both sides int family), but the
    real child chunk is a STRING column. Left unchecked the child would
    self-mask string keys while the parent (masked elsewhere from real int64)
    masks int keys -- different bytes for the same logical key, RI silently
    voided. Now fails closed at the chunk boundary."""
    config = _fk_config(strategy="passthrough", parent_dtype="int64", child_dtype="int64")
    chunk = pa.table({"customer_id": pa.array(["1", "2", "3"], type=pa.string())})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"
    assert "orders.customer_id" in exc.value.message


def test_declared_int_but_real_float_fk_fails_closed() -> None:
    """Same gap under a different value-sensitive strategy (hash) and a
    different real family (float): declared int64, real float64. Cross-family,
    so RI cannot be guaranteed -- fail closed."""
    config = _fk_config(
        strategy="hash", parent_dtype="int64", child_dtype="int64", namespace="cust_ns"
    )
    chunk = pa.table({"customer_id": pa.array([1.0, 2.0, 3.0], type=pa.float64())})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_declared_dtype_mismatch_raises_on_later_chunk_too() -> None:
    """The guard runs per chunk (like the passthrough magnitude guard), so a
    mid-stream chunk whose schema disagrees with the declaration is still
    caught, not just the first."""
    config = _fk_config(strategy="passthrough", parent_dtype="string", child_dtype="string")
    chunks = [
        pa.table({"customer_id": pa.array(["a", "b"], type=pa.string())}),
        pa.table({"customer_id": pa.array([1, 2], type=pa.int64())}),  # schema drift
    ]
    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, chunks, table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_parent_side_declared_dtype_also_validated() -> None:
    """Both-sides symmetry: when the PARENT table is the one being chunked, its
    own declared FK key dtype is validated against the real data too (the
    chunked route processes a parent table through the same one-table-at-a-time
    ingestion and self-masks its key column)."""
    config = _fk_config(strategy="passthrough", parent_dtype="int64", child_dtype="int64")
    chunk = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})  # declared int, real string

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="customers", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"
    assert "customers.id" in exc.value.message


# ---------------------------------------------------------------------------
# No false positives: correct declarations, width-only differences, and
# dictionary-encoded columns still run.
# ---------------------------------------------------------------------------


def test_correct_declared_dtype_still_runs() -> None:
    """A correctly-declared FK dtype (declared string, real string) still masks
    and streams normally -- the guard only fires on a real mismatch."""
    config = _fk_config(strategy="passthrough", parent_dtype="string", child_dtype="string")
    chunk = pa.table({"customer_id": pa.array(["1", "2", "3"], type=pa.string())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == ["1", "2", "3"]  # passthrough identity, unchanged


def test_matching_family_different_width_admitted() -> None:
    """int32 real data under a declared int64 is the SAME family -- the kernel
    canonicalizer encodes any-width integer to the same bytes, so this is not an
    RI risk and must NOT false-positive reject (mirrors the gate's own
    family-granularity tolerance)."""
    config = _fk_config(strategy="passthrough", parent_dtype="int64", child_dtype="int64")
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.int32())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [1, 2, 3]


def test_dictionary_encoded_string_fk_not_false_positive() -> None:
    """A low-cardinality string FK key arrives dictionary-encoded from Parquet
    (`dictionary<values=string, ...>`); its logical family is `string`, matching
    a declared `string`. The guard resolves the dictionary value type first, so
    this is admitted, not spuriously rejected on the literal `dictionary<...>`
    type string."""
    config = _fk_config(strategy="passthrough", parent_dtype="string", child_dtype="string")
    dict_type = pa.dictionary(pa.int32(), pa.string())
    chunk = pa.table({"customer_id": pa.array(["a", "b", "a"]).dictionary_encode().cast(dict_type)})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == ["a", "b", "a"]


# ---------------------------------------------------------------------------
# date vs timestamp: DISTINCT families (date32 and timestamp canonicalize to
# different bytes for the same instant), and fixed_size_binary -> bytes.
# ---------------------------------------------------------------------------


def test_declared_date_but_real_timestamp_fk_fails_closed() -> None:
    """A declared `date` FK key backed by real `timestamp` data used to fold
    into one shared "datetime" family and pass silently. A date32 value and a
    timestamp value for the same instant canonicalize to DIFFERENT bytes, so
    the child would self-mask a different byte sequence than a parent masked
    from its own real dtype -- RI voided. Now a cross-family reject."""
    config = _fk_config(strategy="passthrough", parent_dtype="date", child_dtype="date")
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.timestamp("us"))})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"
    assert "orders.customer_id" in exc.value.message


def test_declared_date_and_real_date32_admitted() -> None:
    """No false positive: a declared `date` FK key backed by a real `date32`
    column is the SAME family and must stream normally."""
    config = _fk_config(strategy="passthrough", parent_dtype="date", child_dtype="date")
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.date32())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert len(vals) == 3


def test_declared_binary_and_real_fixed_size_binary_admitted() -> None:
    """Regression for the false-positive reject: a real `fixed_size_binary[N]`
    Arrow column used to fall through `_dtype_family` to a raw string family
    (bug), so a healthy FK declared `binary` but stored as
    `fixed_size_binary` was falsely rejected. `fixed_size_binary` must map to
    the `bytes` family, same as `binary`."""
    config = _fk_config(strategy="passthrough", parent_dtype="binary", child_dtype="binary")
    chunk = pa.table({"customer_id": pa.array([b"abcd", b"efgh", b"ijkl"], type=pa.binary(4))})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [b"abcd", b"efgh", b"ijkl"]


# ---------------------------------------------------------------------------
# Unit coverage: the declaration collector.
# ---------------------------------------------------------------------------


def test_fk_declared_dtypes_for_table_collects_both_sides() -> None:
    """Collects the declared FK key dtype for BOTH the child role and the parent
    role (both self-mask on this route)."""
    config = _fk_config(strategy="passthrough", parent_dtype="int64", child_dtype="int64")
    assert fk_declared_dtypes_for_table(config, "orders") == {"customer_id": "int64"}
    assert fk_declared_dtypes_for_table(config, "customers") == {"id": "int64"}


def test_fk_declared_dtypes_for_table_excludes_redact() -> None:
    """redact is dtype-invariant (condition (f) skips it): its masked output is
    a constant regardless of the key's dtype, so a declared/real disagreement is
    not an RI assertion and the collector excludes it (no needless reject)."""
    config = _fk_config(strategy="redact", parent_dtype="int64", child_dtype="int64")
    assert fk_declared_dtypes_for_table(config, "orders") == {}
    assert fk_declared_dtypes_for_table(config, "customers") == {}


def test_fk_declared_dtypes_for_table_skips_undeclared() -> None:
    """A column with no declared dtype contributes nothing to the map -- the
    compile-time gate already rejects an undeclared value-sensitive FK dtype
    (chunked_fk_child_key_dtype_unprovable), so this guard only ever validates
    dtypes that WERE declared and trusted."""
    config = _fk_config(strategy="passthrough", parent_dtype=None, child_dtype=None)
    assert fk_declared_dtypes_for_table(config, "orders") == {}


def test_reject_helper_is_a_noop_on_matching_family() -> None:
    """Direct unit: matching families (declared int64, real int16) is a no-op."""
    chunk = pa.table({"customer_id": pa.array([1, 2], type=pa.int16())})
    # Must not raise.
    reject_mismatched_chunked_fk_declared_dtype(
        chunk, table="orders", declared_fk_dtypes={"customer_id": "int64"}
    )


def test_reject_helper_skips_absent_column() -> None:
    """A declared FK column not present in the chunk is skipped (no KeyError)."""
    chunk = pa.table({"other": pa.array([1, 2], type=pa.int64())})
    reject_mismatched_chunked_fk_declared_dtype(
        chunk, table="orders", declared_fk_dtypes={"customer_id": "int64"}
    )


def test_null_typed_all_null_fk_chunk_not_false_positive() -> None:
    """An all-null FK column can arrive as Arrow `null` type (no surviving real
    dtype, e.g. an in-memory all-None array). Null keys mask to null on both
    sides regardless of declared dtype, so RI is trivially preserved -- a
    CORRECT declaration (here int64) must NOT be rejected. Regression for the
    false-positive reject dennis flagged (declared-correct, real `pa.null()`)."""
    chunk = pa.table({"customer_id": pa.array([None, None, None], type=pa.null())})
    # Must not raise despite declared int64 vs real `null`.
    reject_mismatched_chunked_fk_declared_dtype(
        chunk, table="orders", declared_fk_dtypes={"customer_id": "int64"}
    )


def test_typed_all_null_column_still_validated() -> None:
    """The null-type skip is scoped to Arrow `null` ONLY: a TYPED all-null
    column keeps its real family, so a genuine misdeclaration (declared int64,
    real all-null STRING) is still caught -- the skip does not weaken the guard
    for typed columns that merely happen to be all-null."""
    chunk = pa.table({"customer_id": pa.array([None, None], type=pa.string())})
    with pytest.raises(ExecutionError) as exc:
        reject_mismatched_chunked_fk_declared_dtype(
            chunk, table="orders", declared_fk_dtypes={"customer_id": "int64"}
        )
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_null_typed_fk_chunk_streams_end_to_end() -> None:
    """End-to-end: a real chunked run whose FK key chunk is all-null `null`-typed
    under a correct declaration streams normally rather than failing closed."""
    config = _fk_config(strategy="passthrough", parent_dtype="int64", child_dtype="int64")
    chunk = pa.table({"customer_id": pa.array([None, None, None], type=pa.null())})
    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert vals == [None, None, None]


# ---------------------------------------------------------------------------
# Decimal scale-awareness (Codex gpt-5.6-sol HIGH-1, 2026-07-14). A bare
# "decimal"/"numeric" declaration carries no scale, so it is UNPROVABLE for
# RI on this route -- `_dtype_family` maps it to the distinct family
# "decimal:unprovable", which never matches a real scaled-decimal family
# ("decimal(P,S)"). Two bare declarations still compare EQUAL to each other
# at the compile gate (same literal string), so the reject happens here, at
# the runtime declared-vs-real guard, one layer down.
# ---------------------------------------------------------------------------


def test_declared_bare_decimal_but_real_decimal_2_1_fails_closed() -> None:
    """(a) A bare `dtype: "decimal"` declaration (no scale) can never match a
    real scaled Arrow decimal family, so it fails closed regardless of what
    the real scale turns out to be -- here `decimal128(2, 1)`."""
    config = _fk_config(strategy="passthrough", parent_dtype="decimal", child_dtype="decimal")
    chunk = pa.table(
        {
            "customer_id": pa.array(
                [decimal.Decimal("1.0"), decimal.Decimal("2.0")], type=pa.decimal128(2, 1)
            )
        }
    )

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"
    assert "orders.customer_id" in exc.value.message


def test_declared_scaled_decimal_matching_real_admitted() -> None:
    """(b) A declaration that DOES carry precision+scale (PyArrow's own
    `str()` form, "decimal128(2, 1)") parses to the same `decimal(2,1)`
    family as the matching real data, so it is admitted and streams
    normally -- a correct scale-aware declaration is not penalized."""
    config = _fk_config(
        strategy="passthrough",
        parent_dtype="decimal128(2, 1)",
        child_dtype="decimal128(2, 1)",
    )
    chunk = pa.table(
        {
            "customer_id": pa.array(
                [decimal.Decimal("1.0"), decimal.Decimal("2.0")], type=pa.decimal128(2, 1)
            )
        }
    )

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert len(vals) == 2


def test_declared_scaled_decimal_mismatched_real_scale_fails_closed() -> None:
    """A declaration that carries the WRONG scale (declared `decimal128(2, 1)`,
    real `decimal128(3, 2)`) is a genuine misdeclaration, not a bare/unprovable
    one -- still fails closed, on the actual (precision, scale) disagreement."""
    config = _fk_config(
        strategy="passthrough",
        parent_dtype="decimal128(2, 1)",
        child_dtype="decimal128(2, 1)",
    )
    chunk = pa.table(
        {
            "customer_id": pa.array(
                [decimal.Decimal("1.00"), decimal.Decimal("2.00")], type=pa.decimal128(3, 2)
            )
        }
    )

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_reproduced_ri_case_bare_decimal_both_sides_fails_closed_each_role() -> None:
    """(c) Codex's exact repro: parent `decimal128(2, 1)` value 1.0 and child
    `decimal128(3, 2)` value 1.00 both declared bare `"decimal"`. Under the
    OLD single-`"decimal"`-family model this passed the compile gate (both
    declare the same bare string) AND the runtime guard (both real families
    coarsened to the same bare "decimal" too), silently voiding FK RI:
    canonicalize(1.0) != canonicalize(1.00) despite being the same logical
    key. Scale-awareness closes this at the runtime guard on EACH side
    independently -- neither side's bare declaration can match its own
    chunk's real scaled family, regardless of what that real scale is.
    """
    config = _fk_config(strategy="passthrough", parent_dtype="decimal", child_dtype="decimal")

    # Compile gate: bare-vs-bare compares EQUAL (same literal family string),
    # so it is admitted here -- must NOT raise PlanCompileError.
    parent_chunk = pa.table({"id": pa.array([decimal.Decimal("1.0")], type=pa.decimal128(2, 1))})
    child_chunk = pa.table(
        {"customer_id": pa.array([decimal.Decimal("1.00")], type=pa.decimal128(3, 2))}
    )

    # Parent role: real decimal(2,1) vs declared bare "decimal" -- rejected.
    with pytest.raises(ExecutionError) as parent_exc:
        list(
            run_mask_pipeline_chunked(
                config, [parent_chunk], table="customers", engine_version=_ENGINE
            )
        )
    assert parent_exc.value.code == "chunked_fk_declared_dtype_mismatch"

    # Child role: real decimal(3,2) vs declared bare "decimal" -- rejected too.
    with pytest.raises(ExecutionError) as child_exc:
        list(
            run_mask_pipeline_chunked(config, [child_chunk], table="orders", engine_version=_ENGINE)
        )
    assert child_exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_dtype_family_decimal_scale_aware_unit() -> None:
    """Direct unit coverage of the scale-aware family strings themselves."""
    from decoy_engine.execution._chunked_fk import _dtype_family

    assert _dtype_family("decimal128(2, 1)") == "decimal(2,1)"
    assert _dtype_family("decimal256(40, 2)") == "decimal(40,2)"
    assert _dtype_family("decimal(10, 2)") == "decimal(10,2)"
    assert _dtype_family("numeric(10, 2)") == "decimal(10,2)"
    assert _dtype_family("decimal") == "decimal:unprovable"
    assert _dtype_family("numeric") == "decimal:unprovable"
    # Different scales are DIFFERENT families -- not folded together.
    assert _dtype_family("decimal128(2, 1)") != _dtype_family("decimal128(3, 2)")
