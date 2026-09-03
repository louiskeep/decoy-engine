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
from decoy_engine.plan import PlanCompileError

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
    voided. Now fails closed at the chunk boundary. Rebased to `hash`
    (2026-09-02 cascade-safety plan): `passthrough` is no longer admitted for
    FK self-masking at all (see test_chunked_fk_gate_kills.py's allowlist
    gate-kills); the strategy choice here is incidental to what this test
    covers (the declared-vs-real family mismatch), so `hash` reaches the same
    check."""
    config = _fk_config(strategy="hash", parent_dtype="int64", child_dtype="int64", namespace="ns")
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
    caught, not just the first. Rebased to `hash` (strategy-incidental; see
    test_declared_int_but_real_string_fk_fails_closed)."""
    config = _fk_config(
        strategy="hash", parent_dtype="string", child_dtype="string", namespace="ns"
    )
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
    and streams normally -- the guard only fires on a real mismatch. Rebased
    to `hash` (strategy-incidental; see test_declared_int_but_real_string_fk_
    fails_closed): the output is no longer the raw value (hash, unlike the
    dropped `passthrough`, masks it), so the assertion checks the guard did
    NOT fire (a full row count, non-null) rather than a specific value."""
    config = _fk_config(
        strategy="hash", parent_dtype="string", child_dtype="string", namespace="ns"
    )
    chunk = pa.table({"customer_id": pa.array(["1", "2", "3"], type=pa.string())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert len(vals) == 3
    assert all(v is not None for v in vals)


def test_matching_family_different_width_admitted() -> None:
    """int32 real data under a declared int64 is the SAME family -- the kernel
    canonicalizer encodes any-width integer to the same bytes, so this is not an
    RI risk and must NOT false-positive reject (mirrors the gate's own
    family-granularity tolerance). Rebased to `hash` (strategy-incidental; see
    test_declared_int_but_real_string_fk_fails_closed)."""
    config = _fk_config(strategy="hash", parent_dtype="int64", child_dtype="int64", namespace="ns")
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.int32())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert len(vals) == 3
    assert all(v is not None for v in vals)


def test_dictionary_encoded_string_fk_key_rejected_for_hash() -> None:
    """A low-cardinality string FK key arrives dictionary-encoded from Parquet
    (`dictionary<values=string, ...>`); its logical family is `string`,
    matching a declared `string`, so the coarse family check (which resolves
    the dictionary value type first) does NOT reject it. Predicate 12
    (2026-09-02 cascade-safety plan) is a STRICTER, separate check specific to
    hash-only FK self-masking: it rejects a dictionary wrapper BEFORE
    unwrapping (hash on a dictionary-encoded key is not proven cross-adapter
    byte-identical -- deferred per the plan's non-goals), so this now fails
    closed instead of streaming. Was `test_dictionary_encoded_string_fk_not_
    false_positive` (admitted under the dropped `passthrough` strategy); the
    strategy rebase to `hash` is what brings it into predicate 12's scope."""
    config = _fk_config(
        strategy="hash", parent_dtype="string", child_dtype="string", namespace="ns"
    )
    dict_type = pa.dictionary(pa.int32(), pa.string())
    chunk = pa.table({"customer_id": pa.array(["a", "b", "a"]).dictionary_encode().cast(dict_type)})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


# ---------------------------------------------------------------------------
# date vs timestamp: DISTINCT families (date32 and timestamp canonicalize to
# different bytes for the same instant), and fixed_size_binary -> bytes.
# ---------------------------------------------------------------------------


def test_declared_date32_but_real_timestamp_fk_fails_closed() -> None:
    """A declared `date` FK key backed by real `timestamp` data used to fold
    into one shared "datetime" family and pass silently. A date32 value and a
    timestamp value for the same instant canonicalize to DIFFERENT bytes, so
    the child would self-mask a different byte sequence than a parent masked
    from its own real dtype -- RI voided. Now a cross-family reject.

    Rebased to `hash` + declared `date32` (2026-09-02 cascade-safety plan): a
    BARE `"date"` declaration is itself unprovable under predicate 12 (it
    could mean date32 or date64) and would now be caught EARLIER, at compile
    time, by predicate 12's declared stage -- see test_declared_bare_date_
    gate_kill below for that. `date32` is the exact safe form, which lets
    this test keep exercising what it is actually about: the RUNTIME
    declared-vs-real family check."""
    config = _fk_config(
        strategy="hash", parent_dtype="date32", child_dtype="date32", namespace="ns"
    )
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.timestamp("us"))})

    with pytest.raises(ExecutionError) as exc:
        list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"
    assert "orders.customer_id" in exc.value.message


def test_declared_bare_date_gate_kill() -> None:
    """A bare `"date"` declaration (no explicit width) is unprovable under
    predicate 12 -- it could mean date32 (safe) or date64 (excluded) -- so it
    is now rejected at COMPILE time, before any chunk is read. Contrast with
    test_declared_date32_but_real_timestamp_fk_fails_closed, which uses the
    exact `date32` form to keep exercising the runtime family check."""
    config = _fk_config(strategy="hash", parent_dtype="date", child_dtype="date", namespace="ns")

    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_pipeline_chunked(
                config,
                [pa.table({"customer_id": pa.array([1, 2, 3], type=pa.date32())})],
                table="orders",
                engine_version=_ENGINE,
            )
        )
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


def test_declared_date32_and_real_date32_admitted() -> None:
    """No false positive: a declared `date32` FK key backed by a real
    `date32` column is the SAME family and must stream normally. Rebased to
    `hash` + declared `date32` (see test_declared_date32_but_real_timestamp_
    fk_fails_closed)."""
    config = _fk_config(
        strategy="hash", parent_dtype="date32", child_dtype="date32", namespace="ns"
    )
    chunk = pa.table({"customer_id": pa.array([1, 2, 3], type=pa.date32())})

    out = list(run_mask_pipeline_chunked(config, [chunk], table="orders", engine_version=_ENGINE))
    vals = pa.concat_tables(out).column("customer_id").to_pylist()
    assert len(vals) == 3


def test_declared_binary_gate_kill() -> None:
    """Was `test_declared_binary_and_real_fixed_size_binary_admitted`: binary/
    bytes are NOT in predicate 12's exact cross-adapter-safe set at all (only
    string, large_string, integer widths, bool, date32, IANA-tz timestamp,
    and scale>=0 32/64/128-bit decimal are) -- hash on a binary FK key is
    deferred, not proven cross-adapter byte-identical. A declared `binary`
    FK key is now rejected at compile time (2026-09-02 cascade-safety plan),
    flipping this from an admission proof to a gate-kill."""
    config = _fk_config(
        strategy="hash", parent_dtype="binary", child_dtype="binary", namespace="ns"
    )

    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_pipeline_chunked(
                config,
                [pa.table({"customer_id": pa.array([b"abcd", b"efgh"], type=pa.binary(4))})],
                table="orders",
                engine_version=_ENGINE,
            )
        )
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


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
    under a correct declaration streams normally rather than failing closed.
    Rebased to `hash` (strategy-incidental; see test_declared_int_but_real_
    string_fk_fails_closed) -- the all-null `pa.null()` carveout must stay
    green (2026-09-02 cascade-safety plan predicate 12 preserves it)."""
    config = _fk_config(strategy="hash", parent_dtype="int64", child_dtype="int64", namespace="ns")
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


def test_declared_bare_decimal_gate_kill() -> None:
    """(a) Was `test_declared_bare_decimal_but_real_decimal_2_1_fails_closed`:
    a bare `dtype: "decimal"` declaration (no scale) can never match a real
    scaled Arrow decimal family, so it always failed closed regardless of the
    real scale. Rebased to `hash` (strategy-incidental) + `table="orders"`
    (the CHILD role, where the compile-time gate walks this edge): predicate
    12's declared stage (2026-09-02 cascade-safety plan) now catches a bare
    `"decimal"` at COMPILE time -- a strict improvement over deferring to the
    per-chunk runtime guard, since it fails before any chunk is ever read.
    See test_reproduced_ri_case_bare_decimal_parent_role_still_runtime below
    for the PARENT-role call, which the compile gate never walks and so still
    relies on the runtime guard's own bare-decimal-sentinel check."""
    config = _fk_config(
        strategy="hash", parent_dtype="decimal", child_dtype="decimal", namespace="ns"
    )

    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_pipeline_chunked(
                config,
                [
                    pa.table(
                        {
                            "customer_id": pa.array(
                                [decimal.Decimal("1.0"), decimal.Decimal("2.0")],
                                type=pa.decimal128(2, 1),
                            )
                        }
                    )
                ],
                table="orders",
                engine_version=_ENGINE,
            )
        )
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"
    assert "orders.customer_id" in exc.value.message


def test_declared_scaled_decimal_matching_real_admitted() -> None:
    """(b) A declaration that DOES carry precision+scale (PyArrow's own
    `str()` form, "decimal128(2, 1)") parses to the same `decimal(2,1)`
    family as the matching real data, so it is admitted and streams
    normally -- a correct scale-aware declaration is not penalized. Rebased
    to `hash` (strategy-incidental; see test_declared_int_but_real_string_fk_
    fails_closed); `decimal128(2, 1)` is also in predicate 12's exact safe
    set (scale 1 >= 0, fits in 128-bit precision), so it clears BOTH stages
    of the two-stage check too."""
    config = _fk_config(
        strategy="hash",
        parent_dtype="decimal128(2, 1)",
        child_dtype="decimal128(2, 1)",
        namespace="ns",
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
    one -- still fails closed, on the actual (precision, scale) disagreement.
    Rebased to `hash` (strategy-incidental); the declared/declared family
    match happens BEFORE predicate 12 is reached, so this still exercises the
    EXISTING family-mismatch check (unchanged code), not predicate 12."""
    config = _fk_config(
        strategy="hash",
        parent_dtype="decimal128(2, 1)",
        child_dtype="decimal128(2, 1)",
        namespace="ns",
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


def test_reproduced_ri_case_bare_decimal_parent_role_still_runtime() -> None:
    """(c) Codex's exact repro, PARENT role only (the CHILD-role half moved to
    test_declared_bare_decimal_gate_kill, now a compile-time reject): the
    compile-time gate (`gate_fk_child_edges`) only ever walks an edge when the
    table being chunked is the CHILD of it, so gating table="customers" (the
    PARENT here) never even reaches predicate 12's declared stage -- this
    role still depends entirely on the runtime guard's own bare-decimal-
    sentinel check (existing, unchanged), which fails closed on ANY declared
    bare decimal regardless of the real scale."""
    config = _fk_config(
        strategy="hash", parent_dtype="decimal", child_dtype="decimal", namespace="ns"
    )
    parent_chunk = pa.table({"id": pa.array([decimal.Decimal("1.0")], type=pa.decimal128(2, 1))})

    with pytest.raises(ExecutionError) as parent_exc:
        list(
            run_mask_pipeline_chunked(
                config, [parent_chunk], table="customers", engine_version=_ENGINE
            )
        )
    assert parent_exc.value.code == "chunked_fk_declared_dtype_mismatch"


@pytest.mark.parametrize(
    ("declared", "real_array"),
    [
        ("date32", pa.array([0], type=pa.date64())),
        (
            "decimal128(10,2)",
            pa.array([decimal.Decimal("1.00")], type=pa.decimal256(10, 2)),
        ),
    ],
    ids=["date64", "decimal256"],
)
def test_unsafe_real_dtype_in_parent_role_rejected_by_predicate12(
    declared: str, real_array: pa.Array
) -> None:
    """(dennis LOW-2) The parent-role real-type half of predicate 12. Gating the
    PARENT table never reaches the compile-time declared stage (only child edges
    are walked there), and the coarse family check collapses date32/date64 and
    every decimal width into one family, so a real `date64` under a declared
    `date32` (or a real `decimal256` under a declared `decimal128`) sails past
    the family check. The runtime real stage must still reject it, because the
    hash-column scope set is both-sides symmetric. Complements the bare-decimal
    parent-role case above with the two exact dtypes the family check cannot
    distinguish on its own."""
    config = _fk_config(
        strategy="hash", parent_dtype=declared, child_dtype=declared, namespace="ns"
    )
    parent_chunk = pa.table({"id": real_array})
    with pytest.raises(ExecutionError) as exc:
        list(
            run_mask_pipeline_chunked(
                config, [parent_chunk], table="customers", engine_version=_ENGINE
            )
        )
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


@pytest.mark.parametrize(
    ("real_array", "admitted"),
    [
        (pa.array([0], type=pa.date64()), False),
        (pa.array([decimal.Decimal("1.00")], type=pa.decimal256(10, 2)), False),
        (pa.array([5], type=pa.int64()), True),  # safe: must NOT false-positive
        (pa.array([None], type=pa.null()), True),  # all-null carveout intact
    ],
    ids=["date64", "decimal256", "safe_int", "all_null"],
)
def test_undeclared_hash_fk_key_still_predicate12_checked(
    real_array: pa.Array, admitted: bool
) -> None:
    """(Codex final-gate P1-1) `dtype` is optional in config, so a hash FK key
    with NO declared dtype leaves `declared_fk_dtypes` empty and used to skip
    predicate 12's runtime real stage entirely -- an unsafe real date64 /
    decimal256 reached the hash kernel and diverged cross-adapter (native Polars
    raised timezone_naive / Int256-panicked on the same input). The real stage
    is exact-type and needs no declaration; it now runs for every present hash
    FK key column. Safe dtypes and the all-null carveout must not regress."""
    config = _fk_config(
        strategy="hash", parent_dtype=None, child_dtype=None, namespace="ns"
    )
    parent_chunk = pa.table({"id": real_array})
    if admitted:
        out = list(
            run_mask_pipeline_chunked(
                config, [parent_chunk], table="customers", engine_version=_ENGINE
            )
        )
        assert out[0].num_rows == 1
    else:
        with pytest.raises(ExecutionError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config, [parent_chunk], table="customers", engine_version=_ENGINE
                )
            )
        assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


def test_undeclared_hash_fk_key_unsafe_on_later_chunk_still_rejected() -> None:
    """(Codex final-gate P1-1) The undeclared-hash real check must fire on EVERY
    chunk, not just the first: a stream whose first chunk is a safe int64 and
    whose second drifts to date64 must still fail closed on the drifting chunk,
    same as the declared-dtype guard's per-chunk contract."""
    config = _fk_config(
        strategy="hash", parent_dtype=None, child_dtype=None, namespace="ns"
    )
    safe_chunk = pa.table({"id": pa.array([1], type=pa.int64())})
    drifted_chunk = pa.table({"id": pa.array([0], type=pa.date64())})
    with pytest.raises(ExecutionError) as exc:
        list(
            run_mask_pipeline_chunked(
                config,
                [safe_chunk, drifted_chunk],
                table="customers",
                engine_version=_ENGINE,
            )
        )
    assert exc.value.code == "chunked_fk_key_dtype_not_cross_adapter_safe"


def test_dtype_family_decimal_scale_aware_unit() -> None:
    """Direct unit coverage of the scale-aware family strings themselves."""
    from decoy_engine.execution._chunked_fk import _dtype_family

    # RI keys on SCALE ONLY -- precision and storage width are irrelevant.
    assert _dtype_family("decimal128(2, 1)") == "decimal(scale=1)"
    assert _dtype_family("decimal256(40, 2)") == "decimal(scale=2)"
    assert _dtype_family("decimal(10, 2)") == "decimal(scale=2)"
    assert _dtype_family("numeric(10, 2)") == "decimal(scale=2)"
    assert _dtype_family("decimal") == "decimal:unprovable"
    assert _dtype_family("numeric") == "decimal:unprovable"
    # Different scales are DIFFERENT families -- not folded together.
    assert _dtype_family("decimal128(2, 1)") != _dtype_family("decimal128(3, 2)")
    # Same scale, DIFFERENT precision/width -> SAME family (Codex LOW-2): the
    # canonicalizer keys on (unscaled_int, scale), so these mask equal keys
    # identically and must not be over-rejected.
    assert _dtype_family("decimal128(2, 1)") == _dtype_family("decimal128(3, 1)")
    assert _dtype_family("decimal32(2, 1)") == _dtype_family("decimal128(9, 1)")
    # decimal32/decimal64 (PyArrow 24) parse concretely, not as the sentinel
    # (Codex LOW-1): a healthy decimal32 FK must not be false-positive rejected.
    assert _dtype_family("decimal64(9, 1)") == "decimal(scale=1)"
    assert _dtype_family("decimal32(4, 1)") != "decimal:unprovable"
    # Negative scale (Arrow/Parquet-legal) parses concretely, not as the sentinel.
    assert _dtype_family("decimal128(4, -1)") == "decimal(scale=-1)"
    assert _dtype_family("decimal128(4, -1)") != "decimal:unprovable"


def test_declared_bare_decimal_negative_scale_real_fails_closed() -> None:
    """Regression (dennis BLOCKER): a NEGATIVE-scale real decimal must not slip
    the bare-decimal guard. Pre-fix, str(decimal128(4,-1)) failed the scale
    regex and returned the same `decimal:unprovable` sentinel as a bare
    declaration, so real == declared == sentinel and the guard did NOT fire ->
    RI could break silently. Now a bare declaration fails closed regardless of
    the real family."""
    chunk = pa.table(
        {"customer_id": pa.array([decimal.Decimal("1E+1")], type=pa.decimal128(4, -1))}
    )
    with pytest.raises(ExecutionError) as exc:
        reject_mismatched_chunked_fk_declared_dtype(
            chunk, table="orders", declared_fk_dtypes={"customer_id": "decimal"}
        )
    assert exc.value.code == "chunked_fk_declared_dtype_mismatch"


def test_declared_matched_negative_scale_decimal_admitted() -> None:
    """A correctly-scaled NEGATIVE-scale declaration matches its real data and
    is admitted -- the scale regex parses the leading minus, so a legitimate
    negative-scale FK key is not false-positive rejected."""
    chunk = pa.table(
        {"customer_id": pa.array([decimal.Decimal("1E+1")], type=pa.decimal128(4, -1))}
    )
    # Must not raise: declared decimal128(4,-1) resolves to the real family.
    reject_mismatched_chunked_fk_declared_dtype(
        chunk, table="orders", declared_fk_dtypes={"customer_id": "decimal128(4,-1)"}
    )
