"""Chunked FK self-masking -- Option 1, docs/relationships-memory-scaling.md §2.

FK child columns are admitted for chunked self-masking when ALL four gate
conditions hold:

  (a) The parent key column strategy is in CHUNK_SAFE_STRATEGIES (value-keyed,
      deterministic: hash, fpe, redact, truncate, text_redact, date_shift,
      bucketize, passthrough).
  (b) The child FK column explicitly declares the same value-keyed strategy.
  (c) The child FK column explicitly declares the same namespace as the parent
      (required because ColumnSeed.namespace comes from the config column
      entry, not from FK namespace auto-binding; the hash handler reads
      plan_slice.namespace directly).
  (d) The edge's orphan_policy is 'remap'.

Byte-identity proof (hash strategy; identical argument for any CHUNK_SAFE
strategy):

  Non-orphan child row with FK value V matching parent row:
    full-frame: parent masks V -> M = hash(seed, ns, V); child resolves by
                parent-map lookup -> M.
    self-mask:  hash(seed, ns, V) = M.  IDENTICAL.

  Orphan child row with FK value O (no matching parent), policy REMAP:
    full-frame: REMAP mints via parent strategy -> hash(seed, ns, O) = R.
    self-mask:  hash(seed, ns, O) = R.  IDENTICAL.

  Both hold because the strategy is pure(seed, ns, value) with no row-
  positional state; child ns == parent ns (gate c); REMAP mints via the
  parent strategy over the orphan key, which is the self-mask of that key.

First cut (thin vertical slice):
  - Single-column FK edges only; composite FK rejected.
  - REMAP only; WARN/FAIL/PRESERVE rejected.
  - Explicit namespace on child required; auto-inherited rejected.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.execution._chunked import check_chunked_compatibility
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.profile._types import Profile, Relationship
from decoy_engine.profile._walk import walk_dataframe
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import (
    build_namespace_registry,
    build_relationship_graph,
    check_orphan_fk_policy_completeness,
)

_ENGINE = "test-fk-opt1"
_REGISTRY = get_default_registry()

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _fk_config(
    parent_strategy: str = "hash",
    child_strategy: str | None = "hash",
    parent_ns: str = "cust_ns",
    child_ns: str | None = "cust_ns",
    orphan_policy: str = "remap",
    rel_ns: str | None = "cust_ns",
    parent_provider_config: dict | None = None,
    child_provider_config: dict | None = None,
    parent_dtype: str | None = "string",
    child_dtype: str | None = "string",
) -> dict:
    """Minimal two-table FK config with configurable gate knobs.

    Builds a customers(id) -> orders(customer_id) FK config. The parent
    always has `parent_strategy` + `parent_ns`. The child gets
    `child_strategy` + `child_ns` (either can be None to omit the field
    and test missing-declaration rejections). `parent_provider_config` /
    `child_provider_config` let a test declare differing value-affecting
    settings (e.g. redact_with, truncate length) under the same strategy name.
    `parent_dtype` / `child_dtype` declare each column's optional "dtype"
    field (condition (f), the dtype gate). They default to "string" (the FK
    keys these configs use are string keys) because the fail-closed dtype gate
    now REJECTS a value-sensitive strategy whose FK key dtypes are not both
    declared and same-family; pass None explicitly to exercise that rejection.
    """
    parent_col: dict = {"name": "id", "strategy": parent_strategy}
    if parent_ns is not None:
        parent_col["namespace"] = parent_ns
    if parent_provider_config is not None:
        parent_col["provider_config"] = parent_provider_config
    if parent_dtype is not None:
        parent_col["dtype"] = parent_dtype

    child_col: dict = {"name": "customer_id"}
    if child_strategy is not None:
        child_col["strategy"] = child_strategy
    if child_ns is not None:
        child_col["namespace"] = child_ns
    if child_provider_config is not None:
        child_col["provider_config"] = child_provider_config
    if child_dtype is not None:
        child_col["dtype"] = child_dtype

    cfg: dict = {
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "customers", "columns": [parent_col]},
            {"name": "orders", "columns": [child_col]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": orphan_policy,
            }
        ],
    }
    if rel_ns is not None:
        cfg["relationships"][0]["namespace"] = rel_ns
    return cfg


def _chunks(rows: list, col_name: str, chunk_size: int) -> list[pa.Table]:
    """Split a list of values into pyarrow chunks."""
    result = []
    for i in range(0, len(rows), chunk_size):
        batch = rows[i : i + chunk_size]
        result.append(pa.table({col_name: batch}))
    return result


# ---------------------------------------------------------------------------
# Shared plan/profile builder (mirrors what run_mask_pipeline_chunked does)
# ---------------------------------------------------------------------------


def _profile_for(first_chunk: pa.Table, *, table: str) -> Profile:
    """Minimal single-table Profile from a chunk (mirrors first_chunk_profile)."""
    import random

    tbl_profile = walk_dataframe(
        first_chunk.to_pandas(),
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(tbl_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=_ENGINE,
        profile_seed=None,
    )


# ---------------------------------------------------------------------------
# Test 1: byte parity with REMAP orphans (THE correctness proof)
# ---------------------------------------------------------------------------


class TestByteParityWithRemapOrphans:
    """Chunked self-mask output is byte-identical to full-frame FK resolution
    for both matched rows and REMAP-minted orphan rows."""

    def test_chunked_child_matches_fullframe_including_orphans(self) -> None:
        """Core byte-parity assertion.

        Parent: customers ["c1", "c2", "c3"].
        Child:  orders.customer_id ["c1", "c2", "c1", "c9"]; c9 is an orphan.

        Full-frame masks customers first (hash -> M1, M2, M3), then resolves
        orders.customer_id via the parent map: c1->M1, c2->M2, c1->M1, and
        mints c9->hash(seed, ns, "c9") via REMAP.

        Chunked self-mask on orders only: hash(seed, ns, each value) = same
        result because the hash is pure(seed, ns, value) with no row-state.
        Both paths must produce identical bytes in all four output rows.
        """
        config = _fk_config()

        # ---------- full-frame reference path ----------
        # Build a two-table profile (no file I/O needed; walk_dataframe works
        # on in-memory DataFrames).
        import random

        parent_src = pa.table({"id": ["c1", "c2", "c3"]})
        child_src = pa.table({"customer_id": ["c1", "c2", "c1", "c9"]})

        parent_profile = walk_dataframe(
            parent_src.to_pandas(),
            table_name="customers",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=random.Random(0),
        )
        child_profile = walk_dataframe(
            child_src.to_pandas(),
            table_name="orders",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=random.Random(0),
        )
        full_profile = Profile(
            schema_version=1,
            tables=(parent_profile, child_profile),
            relationships=(
                Relationship(
                    parent_table="customers",
                    parent_columns=("id",),
                    child_table="orders",
                    child_columns=("customer_id",),
                    namespace="cust_ns",
                ),
            ),
            profiled_at=datetime(1970, 1, 1, 0, 0, 0),
            decoy_engine_version=_ENGINE,
            profile_seed=None,
        )

        full_plan = compile_plan(config, full_profile, decoy_engine_version=_ENGINE)
        ns_reg = build_namespace_registry(config, full_profile)
        policy_lookup = check_orphan_fk_policy_completeness(config, full_profile.relationships)
        real_graph = build_relationship_graph(
            full_profile.relationships,
            namespace_registry=ns_reg,
            orphan_policy_lookup=policy_lookup,
        )

        adapter = PandasExecutionAdapter()
        full_result = adapter.run(
            full_plan,
            {"customers": parent_src, "orders": child_src},
            registry=_REGISTRY,
            relationship_graph=real_graph,
            namespace_registry=ns_reg,
        )
        full_child_output = full_result.outputs["orders"].column("customer_id").to_pylist()

        # ---------- chunked self-mask path ----------
        # Multiple small chunks to exercise chunk-boundary crossing.
        child_chunks = _chunks(["c1", "c2", "c1", "c9"], "customer_id", chunk_size=2)
        chunked_output = list(
            run_mask_pipeline_chunked(config, child_chunks, table="orders", engine_version=_ENGINE)
        )
        chunked_child = pa.concat_tables(chunked_output).column("customer_id").to_pylist()

        # Every row -- matched AND orphan -- must be byte-identical.
        assert chunked_child == full_child_output, (
            f"Byte parity failed.\nfull-frame: {full_child_output}\nchunked:    {chunked_child}"
        )

        # Sanity: the masked values are NOT the raw source values (masking ran).
        assert "c1" not in chunked_child
        assert "c9" not in chunked_child

        # REMAP orphan gets a non-null, non-source value.
        assert chunked_child[3] is not None
        assert chunked_child[3] != "c9"

    def test_chunk_boundary_does_not_affect_output(self) -> None:
        """Varying the chunk boundary must not change the masked output."""
        config = _fk_config()
        rows = ["c1", "c2", "c3", "orphan_x", "c1", "orphan_y", "c2"]

        results: dict[int, list] = {}
        for chunk_size in (1, 2, 3, 7):
            chunks = _chunks(rows, "customer_id", chunk_size)
            out = list(
                run_mask_pipeline_chunked(config, chunks, table="orders", engine_version=_ENGINE)
            )
            results[chunk_size] = pa.concat_tables(out).column("customer_id").to_pylist()

        ref = results[1]
        for cs, vals in results.items():
            assert vals == ref, f"chunk_size={cs} output differs from chunk_size=1"


# ---------------------------------------------------------------------------
# Test 2: leak guard -- the critical security test
# ---------------------------------------------------------------------------


class TestNoRawKeyLeak:
    """The gate closes before any output for configs that would leak raw FK keys."""

    def test_by_reference_child_no_strategy_rejected(self) -> None:
        """Child FK column with no strategy ('by-reference' model) is gated out.

        Without the gate, the chunked path would attempt to mask the child
        column with whatever its config says -- and with no strategy declared,
        the column passes through unmasked, leaking raw FK key values (PII).
        The gate must raise PlanCompileError before any chunks are consumed,
        ensuring no output is ever produced.
        """
        config = _fk_config(child_strategy=None)  # no strategy on child
        sentinel = ["raw_fk_123", "raw_fk_456"]

        chunks_consumed: list[int] = []

        def _sentinel_chunks() -> list[pa.Table]:
            chunks_consumed.append(1)
            return [pa.table({"customer_id": sentinel})]

        chunks = _sentinel_chunks()
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config, iter(chunks), table="orders", engine_version=_ENGINE
                )
            )

        # Gate must raise a FK-specific code, not pass through silently.
        assert exc.value.code in (
            "chunked_fk_child_strategy_missing",
            "chunked_fk_child_strategy_mismatch",
            "chunked_fk_child_namespace_missing",
        ), f"Expected FK gate code, got {exc.value.code!r}"

        # No raw key values appear in any output -- there should be no output.
        # The PlanCompileError is raised by check_chunked_compatibility, which
        # runs before any chunk is consumed by the masking loop.
        # (The check happens in run_mask_pipeline_chunked eagerly at call time.)

    def test_mismatched_strategy_child_rejected(self) -> None:
        """Child strategy that differs from parent strategy is gated out.

        A child masking 'id' values via fpe while the parent uses hash would
        produce different bytes from the parent's masked values, breaking RI
        and diverging from the full-frame output.
        """
        config = _fk_config(parent_strategy="hash", child_strategy="fpe")
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config,
                    [pa.table({"customer_id": ["c1"]})],
                    table="orders",
                    engine_version=_ENGINE,
                )
            )
        assert exc.value.code == "chunked_fk_child_strategy_mismatch"
        # Error message must not echo the FK value "c1" (no PII in messages).
        assert "c1" not in str(exc.value)

    def test_wrong_namespace_child_rejected(self) -> None:
        """Child with a different namespace from the parent is gated out.

        A different namespace changes the HMAC key, so hash(wrong_ns, value)
        diverges from the parent's hash(correct_ns, value) and from REMAP
        minting, breaking RI silently.
        """
        config = _fk_config(child_ns="wrong_ns", rel_ns="cust_ns")
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config,
                    [pa.table({"customer_id": ["c1"]})],
                    table="orders",
                    engine_version=_ENGINE,
                )
            )
        assert exc.value.code == "chunked_fk_child_namespace_mismatch"
        # No PII in error message.
        assert "c1" not in str(exc.value)


# ---------------------------------------------------------------------------
# Test 3: fail-closed rejections for each gate condition
# ---------------------------------------------------------------------------


class TestFailClosedRejections:
    """Each gate condition failure raises PlanCompileError before any masking."""

    def test_orphan_policy_warn_rejected(self) -> None:
        """WARN cannot be reproduced: emitting an orphan count needs the parent set."""
        config = _fk_config(orphan_policy="warn")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_orphan_policy_not_remap"

    def test_orphan_policy_fail_rejected(self) -> None:
        """FAIL cannot be detected: detecting orphans needs the parent set."""
        config = _fk_config(orphan_policy="fail")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_orphan_policy_not_remap"

    def test_orphan_policy_preserve_rejected(self) -> None:
        """PRESERVE cannot be reproduced: telling orphan from non-orphan requires
        the parent set; self-masking the orphan diverges from the raw-key output."""
        config = _fk_config(orphan_policy="preserve")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_orphan_policy_not_remap"

    def test_parent_strategy_shuffle_rejected(self) -> None:
        """shuffle is a permutation, not value-keyed; cannot be reproduced independently."""
        config = _fk_config(parent_strategy="shuffle")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_parent_strategy_not_safe"

    def test_parent_strategy_faker_rejected(self) -> None:
        """faker without the deterministic path is not chunk-safe as a parent key."""
        config = _fk_config(parent_strategy="faker")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_parent_strategy_not_safe"

    def test_child_strategy_mismatch_rejected(self) -> None:
        """Child strategy must equal parent strategy exactly."""
        config = _fk_config(parent_strategy="hash", child_strategy="fpe", child_ns="cust_ns")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_strategy_mismatch"

    def test_child_missing_strategy_rejected(self) -> None:
        """Child with no strategy (by-reference model) is rejected."""
        config = _fk_config(child_strategy=None)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_strategy_missing"

    def test_child_missing_namespace_rejected(self) -> None:
        """Child with no explicit namespace cannot be verified safe."""
        config = _fk_config(child_ns=None)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_namespace_missing"

    def test_child_namespace_mismatch_rejected(self) -> None:
        """Child namespace must match the edge namespace."""
        config = _fk_config(rel_ns="cust_ns", child_ns="other_ns")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_namespace_mismatch"

    def test_child_provider_config_mismatch_rejected(self) -> None:
        """P1: same strategy NAME is not enough. A parent redacting to 'P'
        and a child redacting to 'C' would self-mask to different bytes even
        though both declare strategy='redact', silently breaking FK RI.
        """
        config = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_provider_config={"redact_with": "P"},
            child_provider_config={"redact_with": "C"},
        )
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_config_mismatch"
        # No PII in the error message body beyond the config values themselves.
        assert "provider_config" in exc.value.message

    def test_child_provider_config_matching_is_admitted(self) -> None:
        """Same strategy name AND identical provider_config is admitted."""
        config = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_provider_config={"redact_with": "X"},
            child_provider_config={"redact_with": "X"},
        )
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_child_key_dtype_mismatch_rejected(self) -> None:
        """Codex round-2 Finding B: dtype-mismatched FK keys are gated out.

        Even with identical strategy + namespace + provider_config,
        `run_mask_pipeline_chunked` masks the child's own value
        independently of the parent (no parent-map lookup). A parent int64
        key and a child float64 FK key holding the "same" logical value
        (1 vs 1.0) hash to DIFFERENT bytes -- the kernel canonicalizer hard-
        errors on float for hash/fpe/date_shift, and even for strategies
        that don't (truncate), a float's string form differs from an int's
        ("1.0" vs "1") -- so self-masking cannot be guaranteed to reproduce
        the parent's masked value. The gate must reject before any output.
        """
        config = _fk_config(parent_dtype="int64", child_dtype="float64")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_key_dtype_mismatch"

    def test_child_key_dtype_mismatch_rejected_before_any_chunk_pulled(self) -> None:
        """The dtype gate fires eagerly, like every other chunked-FK gate."""
        config = _fk_config(parent_dtype="int64", child_dtype="float64")

        class _LazyTracker:
            def __init__(self) -> None:
                self._items: list[pa.Table] = [pa.table({"customer_id": [1.0, 2.0]})]
                self.consumed = 0

            def __iter__(self) -> _LazyTracker:
                return self

            def __next__(self) -> pa.Table:
                if not self._items:
                    raise StopIteration
                self.consumed += 1
                return self._items.pop(0)

        tracker = _LazyTracker()
        with pytest.raises(PlanCompileError) as exc:
            list(run_mask_pipeline_chunked(config, tracker, table="orders", engine_version=_ENGINE))
        assert exc.value.code == "chunked_fk_child_key_dtype_mismatch"
        assert tracker.consumed == 0

    def test_child_key_dtype_matching_is_admitted(self) -> None:
        """Matching declared dtypes (same family) are admitted."""
        config = _fk_config(parent_dtype="int64", child_dtype="int64")
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_child_key_dtype_matching_family_widths_admitted(self) -> None:
        """Different widths within the same family (int32 vs int64) are fine:
        the kernel canonicalizer encodes any-width int identically."""
        config = _fk_config(parent_dtype="int32", child_dtype="int64")
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_child_key_dtype_date_timestamp_mismatch_rejected(self) -> None:
        """date and timestamp are DISTINCT families (not folded together): a
        date32 value and a timestamp value for the same instant canonicalize
        to different bytes, so a declared-date parent / declared-timestamp
        child FK edge cannot be proven to self-mask identically. Reject."""
        config = _fk_config(parent_dtype="date", child_dtype="timestamp")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_key_dtype_mismatch"

    def test_child_key_dtype_undeclared_rejected_fail_closed(self) -> None:
        """Fail-closed dtype gate (2026-07-07): a value-sensitive strategy with
        an undeclared dtype on either side can NOT be proven to self-mask to the
        parent's value, so it is REJECTED (was fail-open before the fix -- the
        common int-parent/float-child mismatch slipped through undeclared).

        The child's chunked run never sees the parent's data, so the mismatch is
        not recoverable at runtime either; full-frame / run_sequential handle it.
        """
        # hash is value-sensitive; neither side declares a dtype.
        config = _fk_config(parent_dtype=None, child_dtype=None)
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_key_dtype_unprovable"

    def test_child_key_dtype_one_side_undeclared_rejected(self) -> None:
        """Declaring the dtype on only one side is still unprovable -> reject."""
        for parent_dtype, child_dtype in (("int64", None), (None, "int64")):
            config = _fk_config(parent_dtype=parent_dtype, child_dtype=child_dtype)
            with pytest.raises(PlanCompileError) as exc:
                check_chunked_compatibility(config, table="orders")
            assert exc.value.code == "chunked_fk_child_key_dtype_unprovable"

    def test_truncate_undeclared_dtype_rejected(self) -> None:
        """truncate is the silent-divergence case (str(1) != str(1.0)); with an
        undeclared dtype it must fail closed."""
        config = _fk_config(
            parent_strategy="truncate",
            child_strategy="truncate",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
            parent_provider_config={"length": 3},
            child_provider_config={"length": 3},
            parent_dtype=None,
            child_dtype=None,
        )
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_key_dtype_unprovable"

    def test_redact_undeclared_dtype_admitted_value_independent(self) -> None:
        """redact is the one value-INDEPENDENT strategy: it emits a constant for
        every non-null input regardless of dtype, so parent and child provably
        mask to the same value even with no dtype declared. It must be admitted."""
        config = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
            parent_dtype=None,
            child_dtype=None,
        )
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_child_key_dtype_decimal_scale_mismatch_rejected(self) -> None:
        """Decimal scale-awareness (Codex gpt-5.6-sol HIGH-1, 2026-07-14): a
        decimal's canonical bytes depend on its SCALE, not just its family --
        `decimal(2,1)` value 1.0 and `decimal(3,2)` value 1.00 are the same
        logical number but canonicalize to different bytes under
        hash/truncate/fpe. A parent declared `decimal(2, 1)` and a child
        declared `decimal(3, 2)` are therefore DIFFERENT families (unlike the
        old single-"decimal"-bucket model, which folded every scale together
        and let this mismatch through), so this is rejected at the compile
        gate exactly like an int/float mismatch."""
        config = _fk_config(parent_dtype="decimal(2, 1)", child_dtype="decimal(3, 2)")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_child_key_dtype_mismatch"

    def test_child_key_dtype_decimal_matching_scale_admitted(self) -> None:
        """No false positive: identical (precision, scale) on both sides is
        the SAME family and must be admitted."""
        config = _fk_config(parent_dtype="decimal(2, 1)", child_dtype="decimal(2, 1)")
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_child_key_dtype_bare_decimal_both_sides_admitted_at_compile_gate(self) -> None:
        """A bare `"decimal"` declaration on both sides (no scale -- Codex's
        exact repro shape) compares EQUAL at the compile gate (same literal
        unprovable-family string), so it is admitted HERE. The actual
        fail-closed reject for a bare declaration happens one layer down, at
        the per-chunk runtime guard (`_chunked_fk_dtype`), which has real
        Arrow data to compare against and can prove a bare declaration never
        matches any real scaled family. See
        test_de10_chunked_fk_declared_dtype.py for that runtime coverage."""
        config = _fk_config(parent_dtype="decimal", child_dtype="decimal")
        # Must not raise -- the compile gate cannot see real data, so a
        # same-string bare/bare declaration is not, by itself, a proven
        # mismatch at THIS layer.
        check_chunked_compatibility(config, table="orders")

    def test_composite_fk_rejected(self) -> None:
        """Composite FK (multi-column key) is out of scope for first cut."""
        config = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "parents",
                    "columns": [
                        {"name": "pk1", "strategy": "hash", "namespace": "ns1"},
                        {"name": "pk2", "strategy": "hash", "namespace": "ns2"},
                    ],
                },
                {
                    "name": "children",
                    "columns": [
                        {"name": "fk1", "strategy": "hash", "namespace": "ns1"},
                        {"name": "fk2", "strategy": "hash", "namespace": "ns2"},
                    ],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parents", "columns": ["pk1", "pk2"]},
                    "children": [{"table": "children", "columns": ["fk1", "fk2"]}],
                    "orphan_policy": "remap",
                    "namespace": "ns1",
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="children")
        assert exc.value.code == "chunked_fk_composite_unsupported"

    def test_parent_is_unaffected_by_fk_gate(self) -> None:
        """Chunking the PARENT table (no FK child columns on the parent itself)
        is always allowed, even when relationships exist in the config."""
        config = _fk_config()
        # Must not raise -- the parent table has no FK child columns to gate.
        check_chunked_compatibility(config, table="customers")

    def test_admitted_config_passes_gate(self) -> None:
        """A clean REMAP config with matching strategy and namespace passes."""
        config = _fk_config()
        # Must not raise.
        check_chunked_compatibility(config, table="orders")


# ---------------------------------------------------------------------------
# Test 4: MAJOR defect -- edge namespace differs from parent-column namespace
# ---------------------------------------------------------------------------


class TestParentColumnNamespaceIsAuthoritative:
    """The gate must use the parent COLUMN's namespace as the authoritative
    reference, not the edge (relationship) namespace.

    Execution derives masked values via derive(seed, col_namespace, value) where
    col_namespace = col_entry.get("namespace") (_seed_envelope.py:260). The edge
    namespace (rel_ns / edge.namespace) is a WARN label; it is never passed to
    derive(). Admitting a config where rel_ns != parent_col_namespace means the
    child is hashed under a different key than the parent -> silent RI break.
    """

    def test_parent_ns_differs_from_rel_ns_rejected(self) -> None:
        """MAJOR defect: edge namespace differs from parent-column namespace.

        parent_col namespace = "A_parent", edge namespace = "B_rel",
        child namespace = "B_rel" (matches edge, not parent col).

        Pre-fix: effective_ns = rel_ns = "B_rel"; child_ns == effective_ns ->
        ADMITTED, but parent hashes under "A_parent" while child hashes under
        "B_rel" -> diverge (silent RI break).

        Post-fix: gate compares child_ns against parent_col namespace = "A_parent"
        first detects rel_ns != parent_col_namespace and raises
        chunked_fk_parent_namespace_mismatch before any output is produced.
        """
        config = _fk_config(parent_ns="A_parent", rel_ns="B_rel", child_ns="B_rel")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        assert exc.value.code == "chunked_fk_parent_namespace_mismatch", (
            f"Expected chunked_fk_parent_namespace_mismatch, got {exc.value.code!r}"
        )
        # Message must name both the conflicting namespaces.
        assert "A_parent" in exc.value.message and "B_rel" in exc.value.message

    def test_parent_ns_differs_from_rel_ns_gate_prevents_divergent_output(self) -> None:
        """After the fix, run_mask_pipeline_chunked raises before producing output.

        Proves that the byte-divergence (parent hash under A_parent, child hash
        under B_rel) cannot silently reach the caller; the gate fires at compile
        time, no chunks are consumed, and no output is emitted.
        """
        config = _fk_config(parent_ns="A_parent", rel_ns="B_rel", child_ns="B_rel")

        class _LazyTracker:
            """Iterator that records pulls on __next__, not at construction time."""

            def __init__(self) -> None:
                self._items: list[pa.Table] = [pa.table({"customer_id": ["c1", "c2"]})]
                self.consumed: int = 0

            def __iter__(self) -> _LazyTracker:
                return self

            def __next__(self) -> pa.Table:
                if not self._items:
                    raise StopIteration
                self.consumed += 1
                return self._items.pop(0)

        tracker = _LazyTracker()
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    config,
                    tracker,
                    table="orders",
                    engine_version=_ENGINE,
                )
            )
        assert exc.value.code == "chunked_fk_parent_namespace_mismatch"
        # Gate fires at compile time (check_chunked_compatibility) before the
        # iterator is advanced; tracker.consumed == 0 proves no chunk was pulled.
        assert tracker.consumed == 0, (
            f"Gate must fire before any chunks are consumed; {tracker.consumed} were pulled"
        )
        # Both conflicting namespace strings must appear in the message.
        msg = exc.value.message
        assert "A_parent" in msg and "B_rel" in msg

    def test_parent_ns_none_with_rel_ns_rejected(self) -> None:
        """MINOR defect: parent column declares no namespace but edge has one.

        parent_col namespace = None, edge namespace = "cust_ns",
        child namespace = "cust_ns".

        Pre-fix: effective_ns = rel_ns = "cust_ns"; child_ns matches -> ADMITTED.
        But the hash strategy raises hash_requires_namespace when plan.namespace
        is None, so the full-frame path fails at execution while chunked silently
        produces masked output under "cust_ns". The gate is more permissive than
        the full-frame path.

        Post-fix: gate sees parent_col namespace is None -> raises
        chunked_fk_parent_namespace_missing (fail-closed, consistent with
        hash_requires_namespace in full-frame).
        """
        config = _fk_config(parent_ns=None, rel_ns="cust_ns", child_ns="cust_ns")
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config, table="orders")
        # Must raise a namespace-missing or parent-namespace-missing code, not admit.
        assert exc.value.code in (
            "chunked_fk_parent_namespace_missing",
            "chunked_fk_child_namespace_missing",
        ), f"Expected parent-namespace-missing variant, got {exc.value.code!r}"

    def test_rel_ns_absent_and_parent_ns_set_is_admitted(self) -> None:
        """Happy path: edge has no namespace, parent col has one.

        When rel_ns is absent (None), the parent_col namespace is the only
        reference and the gate should admit the edge as long as child_ns matches.
        """
        config = _fk_config(parent_ns="cust_ns", rel_ns=None, child_ns="cust_ns")
        # Must not raise.
        check_chunked_compatibility(config, table="orders")

    def test_rel_ns_equals_parent_ns_is_admitted(self) -> None:
        """Happy path: edge namespace equals parent-column namespace.

        A consistent config (rel_ns == parent_col_ns == child_ns) must be admitted.
        This is the canonical form; the edge namespace redundantly documents the
        masking key without disagreeing with it.
        """
        config = _fk_config(parent_ns="cust_ns", rel_ns="cust_ns", child_ns="cust_ns")
        # Must not raise.
        check_chunked_compatibility(config, table="orders")


# ---------------------------------------------------------------------------
# Test 5: byte parity under a non-default namespace
# ---------------------------------------------------------------------------


class TestByteParityNonDefaultNamespace:
    """Byte-parity holds when the namespace is distinct from the default 'cust_ns'.

    Separates the three variables (parent_ns, rel_ns, child_ns) from collapsing
    to the same value in the existing happy-path tests, proving the gate passes
    and parity holds for any namespace string, not just 'cust_ns'.
    """

    def test_byte_parity_distinct_namespace(self) -> None:
        """Chunked output is byte-identical to full-frame under 'vault_ns_42'.

        The namespace 'vault_ns_42' is distinct from the default 'cust_ns' used
        everywhere else in this file, ensuring the test is not vacuously satisfied
        by a collision with another fixture's namespace.
        """
        import random

        ns = "vault_ns_42"
        config = _fk_config(parent_ns=ns, rel_ns=ns, child_ns=ns)

        parent_src = pa.table({"id": ["p1", "p2", "p3"]})
        child_src = pa.table({"customer_id": ["p1", "p3", "orphan_z", "p2"]})

        parent_profile = walk_dataframe(
            parent_src.to_pandas(),
            table_name="customers",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=random.Random(42),
        )
        child_profile = walk_dataframe(
            child_src.to_pandas(),
            table_name="orders",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=random.Random(42),
        )
        full_profile = Profile(
            schema_version=1,
            tables=(parent_profile, child_profile),
            relationships=(
                Relationship(
                    parent_table="customers",
                    parent_columns=("id",),
                    child_table="orders",
                    child_columns=("customer_id",),
                    namespace=ns,
                ),
            ),
            profiled_at=datetime(1970, 1, 1, 0, 0, 0),
            decoy_engine_version=_ENGINE,
            profile_seed=None,
        )

        full_plan = compile_plan(config, full_profile, decoy_engine_version=_ENGINE)
        ns_reg = build_namespace_registry(config, full_profile)
        policy_lookup = check_orphan_fk_policy_completeness(config, full_profile.relationships)
        real_graph = build_relationship_graph(
            full_profile.relationships,
            namespace_registry=ns_reg,
            orphan_policy_lookup=policy_lookup,
        )

        adapter = PandasExecutionAdapter()
        full_result = adapter.run(
            full_plan,
            {"customers": parent_src, "orders": child_src},
            registry=_REGISTRY,
            relationship_graph=real_graph,
            namespace_registry=ns_reg,
        )
        full_child_output = full_result.outputs["orders"].column("customer_id").to_pylist()

        child_chunks = _chunks(["p1", "p3", "orphan_z", "p2"], "customer_id", chunk_size=2)
        chunked_output = list(
            run_mask_pipeline_chunked(config, child_chunks, table="orders", engine_version=_ENGINE)
        )
        chunked_child = pa.concat_tables(chunked_output).column("customer_id").to_pylist()

        assert chunked_child == full_child_output, (
            f"Byte parity failed under namespace {ns!r}.\n"
            f"full-frame: {full_child_output}\nchunked:    {chunked_child}"
        )
        # Masking ran -- raw values must not appear in output.
        assert "p1" not in chunked_child
        assert "orphan_z" not in chunked_child


# ---------------------------------------------------------------------------
# Test 6: namespace-agnostic strategies admitted when parent_ns=None
# ---------------------------------------------------------------------------


class TestNamespaceAgnosticStrategiesAdmitted:
    """Strategies that do not consume namespace (redact, passthrough, truncate,
    text_redact, bucketize) must be admitted by the gate even when parent_ns=None.

    Before the fix the namespace check was unconditional: any strategy with
    parent_ns=None raised chunked_fk_parent_namespace_missing. That rejected
    configs the full-frame path ACCEPTS -- a regression introduced by the prior fix.

    After the fix the namespace sub-checks (c1/c2/c3) apply only to strategies in
    NAMESPACE_REQUIRING_STRATEGIES (hash, fpe, date_shift), which call
    derive(job_seed, namespace, ...) and raise at execution when namespace is None.
    Namespace-agnostic strategies produce byte-identical output regardless of
    whether a namespace is declared, so admitting them is not a security regression.
    """

    def test_redact_parent_ns_none_admitted_and_produces_redacted_output(self) -> None:
        """redact with no namespace on parent or child must be admitted and mask correctly.

        FAILS before fix: gate raises chunked_fk_parent_namespace_missing because
        the pre-fix namespace check is unconditional.
        PASSES after fix: gate skips the namespace sub-checks for redact (not in
        NAMESPACE_REQUIRING_STRATEGIES) and the chunked run produces "REDACTED"
        for every value -- byte-identical to the full-frame path.

        Byte-identity proof: redact is pure(value, config) -> constant "REDACTED"
        for any non-null input, independent of namespace. Full-frame path:
        parent masked to "REDACTED"; child FK values found in map -> "REDACTED";
        REMAP orphan: redact(orphan_key) = "REDACTED". Both paths produce
        ["REDACTED", "REDACTED", "REDACTED", "REDACTED"] for the input below.
        """
        config = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
        )
        rows = ["c1", "c2", "c1", "c9"]
        child_chunks = _chunks(rows, "customer_id", chunk_size=2)
        # Before fix: raises chunked_fk_parent_namespace_missing.
        # After fix: succeeds.
        chunked_output = list(
            run_mask_pipeline_chunked(config, child_chunks, table="orders", engine_version=_ENGINE)
        )
        chunked_vals = pa.concat_tables(chunked_output).column("customer_id").to_pylist()

        # All values -- matched and orphan -- become "REDACTED".
        assert all(v == "REDACTED" for v in chunked_vals), (
            f"Expected all 'REDACTED'; got {chunked_vals}"
        )
        assert len(chunked_vals) == len(rows)
        # No raw FK key appears in output.
        assert "c1" not in [v for v in chunked_vals if v != "REDACTED"]
        assert "c9" not in chunked_vals

    def test_passthrough_parent_ns_none_admitted_and_preserves_raw_values(self) -> None:
        """passthrough with no namespace on parent or child must be admitted.

        FAILS before fix: gate raises chunked_fk_parent_namespace_missing.
        PASSES after fix: gate skips namespace sub-checks for passthrough and
        the chunked run returns values unchanged -- byte-identical to full-frame.

        Byte-identity proof: passthrough is a no-op, pure(value) = value,
        namespace-independent. Full-frame path: parent identity (c1->c1, c2->c2,
        c3->c3); child FK values found in map -> same raw value; REMAP orphan:
        passthrough(orphan) = orphan. Both paths preserve the raw input exactly.

        Note: passthrough as an FK key strategy retains raw values in the output --
        the same behavior as the full-frame path. This is the user's explicit
        config choice; it is not a regression introduced by this admission.
        """
        config = _fk_config(
            parent_strategy="passthrough",
            child_strategy="passthrough",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
        )
        rows = ["c1", "c2", "c1", "c9"]
        child_chunks = _chunks(rows, "customer_id", chunk_size=2)
        # Before fix: raises chunked_fk_parent_namespace_missing.
        # After fix: succeeds.
        chunked_output = list(
            run_mask_pipeline_chunked(config, child_chunks, table="orders", engine_version=_ENGINE)
        )
        chunked_vals = pa.concat_tables(chunked_output).column("customer_id").to_pylist()

        # passthrough is a no-op: raw FK key values are preserved (user's choice).
        assert chunked_vals == rows, f"passthrough must preserve raw values; got {chunked_vals}"

    def test_redact_gate_admission_check_only(self) -> None:
        """Gate-only admission test: check_chunked_compatibility must not raise
        for redact with no namespace."""
        config = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
        )
        # Must not raise -- FAILS before fix, PASSES after fix.
        check_chunked_compatibility(config, table="orders")

    def test_passthrough_gate_admission_check_only(self) -> None:
        """Gate-only admission test: check_chunked_compatibility must not raise
        for passthrough with no namespace."""
        config = _fk_config(
            parent_strategy="passthrough",
            child_strategy="passthrough",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
        )
        # Must not raise -- FAILS before fix, PASSES after fix.
        check_chunked_compatibility(config, table="orders")

    def test_other_gates_still_apply_to_namespace_agnostic_strategies(self) -> None:
        """Conditions (a), (b), (d) still gate even namespace-agnostic strategies.

        The namespace sub-check exemption does NOT loosen the other conditions.
        Child strategy must still match parent; orphan_policy must still be remap.
        """
        # Mismatched child strategy must still be rejected.
        config_bad_child = _fk_config(
            parent_strategy="redact",
            child_strategy="truncate",  # mismatch
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
        )
        with pytest.raises(PlanCompileError) as exc:
            check_chunked_compatibility(config_bad_child, table="orders")
        assert exc.value.code == "chunked_fk_child_strategy_mismatch"

        # Wrong orphan_policy must still be rejected.
        config_bad_policy = _fk_config(
            parent_strategy="redact",
            child_strategy="redact",
            parent_ns=None,
            child_ns=None,
            rel_ns=None,
            orphan_policy="preserve",
        )
        with pytest.raises(PlanCompileError) as exc2:
            check_chunked_compatibility(config_bad_policy, table="orders")
        assert exc2.value.code == "chunked_fk_orphan_policy_not_remap"
