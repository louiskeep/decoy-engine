"""engine-v2 S9 slice 2d: hash (derive-keyed) + bucketize (no-backend) strategies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.determinism import derive
from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.execution._strategies._hash import HashStrategyHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x55).to_bytes(8, "big")


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class TestHash:
    def test_same_source_same_token_and_reproducible(self) -> None:
        src = pa.table({"id": ["alice", "bob", "alice"]})
        out = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out[0] == out[2]  # joinability: same source -> same token
        assert out[0] != out[1]
        out2 = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out == out2  # reproducible across runs

    def test_truncate(self) -> None:
        src = pa.table({"id": ["alice"]})
        seed = _col("hash", namespace="ids", provider_config=(("truncate", 12),))
        out = _run(_plan("id", seed), src).output.column("id").to_pylist()
        assert len(out[0]) == 12

    def test_null_preserved(self) -> None:
        src = pa.table({"id": ["alice", None]})
        out = _run(_plan("id", _col("hash", namespace="ids")), src).output.column("id").to_pylist()
        assert out[1] is None

    def test_missing_namespace_raises(self) -> None:
        src = pa.table({"id": ["alice"]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("id", _col("hash", namespace=None)), src)
        assert exc.value.code == "hash_requires_namespace"


class TestHashHandlerContract:
    """Direct-handler oracles pinning the machine-observable contract:
    the namespace-guard error identity and the truncate boundary."""

    def test_missing_namespace_error_carries_strategy_and_code(self) -> None:
        # The StrategyError machine fields (code + strategy) are the contract
        # the adapter routes on; the message is prose.
        df = pd.DataFrame({"id": ["alice"]})
        seed = _col("hash", namespace=None)
        with pytest.raises(StrategyError) as exc:
            HashStrategyHandler().run(df, "id", seed, _FakeCtx())
        assert exc.value.code == "hash_requires_namespace"
        assert exc.value.strategy == "hash"

    def test_truncate_zero_is_ignored_not_applied(self) -> None:
        # truncate is opt-in on a positive int; 0 (and any non-positive) means
        # "no truncation", never "truncate to empty".
        df = pd.DataFrame({"id": ["alice"]})
        seed = _col("hash", namespace="ids", provider_config=(("truncate", 0),))
        out, _ = HashStrategyHandler().run(df, "id", seed, _FakeCtx())
        expected = derive(_SEED, "ids", _canonicalize_source("alice")).hex()
        assert out["id"].iloc[0] == expected
        assert len(out["id"].iloc[0]) > 1

    def test_truncate_one_truncates_to_one_char(self) -> None:
        df = pd.DataFrame({"id": ["alice"]})
        seed = _col("hash", namespace="ids", provider_config=(("truncate", 1),))
        out, _ = HashStrategyHandler().run(df, "id", seed, _FakeCtx())
        expected = derive(_SEED, "ids", _canonicalize_source("alice")).hex()[:1]
        assert out["id"].iloc[0] == expected
        assert len(out["id"].iloc[0]) == 1


class TestBucketize:
    def test_lower_format_integer_width(self) -> None:
        src = pa.table({"age": [23, 47, 8, None]})
        seed = _col("bucketize", provider_config=(("width", 10),))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["20", "40", "0", None]

    def test_range_format(self) -> None:
        src = pa.table({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 10), ("format", "range")))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["20-29"]

    def test_preset_decade(self) -> None:
        src = pa.table({"age": [37]})
        seed = _col("bucketize", provider_config=(("preset", "by_decade"),))
        out = _run(_plan("age", seed), src).output.column("age").to_pylist()
        assert out == ["30"]

    def test_invalid_width_raises(self) -> None:
        """Sprint 13 / coercion-13 S3, GATE-1 Q4 (2026-07-03): an
        unresolvable width now fails closed instead of passing the source
        column through unmasked."""
        src = pa.table({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 0),))
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("age", seed), src)
        assert exc.value.code == "bucketize_width_unresolvable"


class _FakeCtx:
    """Minimal StrategyContext stand-in for direct handler.run() calls."""

    job_seed = _SEED
    # DE-02: keyed strategies read ctx.mask_key; no-secret path == job_seed.
    mask_key = _SEED


class TestMixedObjectColumnRegression:
    """P2 (Codex review): the SC1 kernel port converts a pandas column to
    ONE Arrow array up front (`pa.array(df[column], from_pandas=True)`)
    before handing it to `hash_array`/`truncate_array`. A pandas object
    column that legitimately mixes Python scalar types (str and int
    identifiers in one column -- a real shape for loosely-typed source data)
    has no single Arrow type, so that conversion raises `ArrowTypeError`
    where the pre-kernel per-value handlers masked the column successfully.

    These pin byte-identical output to the pre-kernel oracle: `derive` +
    `_canonicalize_source` for hash (the exact pre-kernel formula), and
    `str(value)[:length]` for truncate (the exact pre-kernel `astype(str)`
    behavior). The handler must go through the normal (arrow-array) path,
    NOT this fallback, whenever the whole column IS one Arrow type: only the
    genuinely-mixed column exercises the fallback.
    """

    def test_hash_masks_mixed_str_int_object_column(self) -> None:
        df = pd.DataFrame({"col": ["alice", 42, "bob"]})
        assert df["col"].dtype == object
        seed = _col("hash", namespace="ids")
        out, warnings = HashStrategyHandler().run(df.copy(), "col", seed, _FakeCtx())

        expected = [
            derive(_SEED, "ids", _canonicalize_source(value)).hex()
            for value in ["alice", 42, "bob"]
        ]
        assert out["col"].tolist() == expected
        assert warnings == []

    def test_hash_mixed_object_column_preserves_null(self) -> None:
        df = pd.DataFrame({"col": ["alice", None, 7]})
        seed = _col("hash", namespace="ids")
        out, _ = HashStrategyHandler().run(df.copy(), "col", seed, _FakeCtx())

        assert out["col"].iloc[1] is None
        assert out["col"].iloc[0] == derive(_SEED, "ids", _canonicalize_source("alice")).hex()
        assert out["col"].iloc[2] == derive(_SEED, "ids", _canonicalize_source(7)).hex()

    def test_truncate_masks_mixed_str_int_object_column(self) -> None:
        df = pd.DataFrame({"col": ["hello", 123, "ab"]})
        assert df["col"].dtype == object
        seed = _col("truncate", provider_config=(("length", 2),))
        out, warnings = TruncateHandler().run(df.copy(), "col", seed, _FakeCtx())

        # Pre-kernel oracle: non_na.astype(str).str[:length].
        assert out["col"].tolist() == ["he", "12", "ab"]
        assert warnings == []

    def test_truncate_mixed_object_column_preserves_null(self) -> None:
        df = pd.DataFrame({"col": ["hello", None, 999]})
        seed = _col("truncate", provider_config=(("length", 2),))
        out, _ = TruncateHandler().run(df.copy(), "col", seed, _FakeCtx())

        assert pd.isna(out["col"].iloc[1])
        assert out["col"].iloc[0] == "he"
        assert out["col"].iloc[2] == "99"


def _bucketize_ctx() -> SimpleNamespace:
    # Fresh per-call row_errors sink so direct handler runs never share state.
    return SimpleNamespace(job_seed=_SEED, mask_key=_SEED, row_errors=[])


class TestBucketizeSurvivors:
    """Direct-handler oracles pinning bucketize boundary math, format
    selection, the fail-closed contract, and the per-row error channel.
    These are the machine-observable outputs (bucket strings, error code +
    strategy, RowError machine fields), never message prose."""

    def test_unresolvable_width_error_carries_code_and_strategy(self) -> None:
        # The StrategyError machine fields (code + strategy) are the fail-closed
        # contract the adapter routes on; the message is prose.
        df = pd.DataFrame({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 0),))
        with pytest.raises(StrategyError) as exc:
            BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert exc.value.code == "bucketize_width_unresolvable"
        assert exc.value.strategy == "bucketize"

    def test_bool_width_fails_closed(self) -> None:
        # bool is an int subclass but is never a valid width; it must fail
        # closed, not silently resolve to 1 and bucketize by that.
        df = pd.DataFrame({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", True),))
        with pytest.raises(StrategyError) as exc:
            BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert exc.value.code == "bucketize_width_unresolvable"

    def test_width_one_is_resolvable(self) -> None:
        # width==1 is a valid positive width (identity bucketing), not the
        # unresolvable/non-positive boundary.
        df = pd.DataFrame({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 1),))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["23"]

    def test_unknown_format_falls_back_to_lower(self) -> None:
        # An unrecognized format resolves to "lower" output, never to the
        # midpoint (else) branch.
        df = pd.DataFrame({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 10), ("format", "bogus")))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["20"]

    def test_float_width_lower_keeps_fractional(self) -> None:
        # A float width is not an integer width: the lower edge stays a float
        # ("5.0"), it is not coerced to a nullable-int bucket ("5").
        df = pd.DataFrame({"age": [6]})
        seed = _col("bucketize", provider_config=(("width", 2.5),))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["5.0"]

    def test_float_width_range_upper_edge(self) -> None:
        # Float-width range uses the exclusive upper edge (lower + width), not
        # lower - width and not a dropped/None edge.
        df = pd.DataFrame({"age": [6]})
        seed = _col("bucketize", provider_config=(("width", 2.5), ("format", "range")))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["5.0-7.5"]

    def test_midpoint_even_int_width(self) -> None:
        # Even integer width: midpoint is lower + width/2 rendered as a whole
        # number ("25"), not lower - width/2, width*2, a float, or a crash.
        df = pd.DataFrame({"age": [23]})
        seed = _col("bucketize", provider_config=(("width", 10), ("format", "midpoint")))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["25"]

    def test_midpoint_odd_int_width_keeps_fractional(self) -> None:
        # Odd integer width: midpoint has a half and must stay fractional
        # ("12.5"), not be truncated to a whole number.
        df = pd.DataFrame({"age": [12]})
        seed = _col("bucketize", provider_config=(("width", 5), ("format", "midpoint")))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].tolist() == ["12.5"]

    def test_midpoint_preserves_null(self) -> None:
        # Nullable-int midpoint carries source nulls through; the null cell must
        # not force a non-nullable cast that would reject the NaN.
        df = pd.DataFrame({"age": [23, None]})
        seed = _col("bucketize", provider_config=(("width", 10), ("format", "midpoint")))
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, _bucketize_ctx())
        assert out["age"].iloc[0] == "25"
        assert pd.isna(out["age"].iloc[1])

    def test_non_numeric_cell_records_row_error_and_keeps_original(self) -> None:
        # A non-null, non-coercible cell is a per-row format error: recorded on
        # ctx.row_errors with its machine fields, output keeps the ORIGINAL
        # value (quarantine carries originals), and null/numeric rows record no
        # error.
        df = pd.DataFrame({"age": ["10", "abc"]})
        seed = _col("bucketize", provider_config=(("width", 10),))
        ctx = _bucketize_ctx()
        out, _ = BucketizeStrategyHandler().run(df, "age", seed, ctx)
        assert out["age"].tolist() == ["10", "abc"]
        assert len(ctx.row_errors) == 1
        err = ctx.row_errors[0]
        assert err.column == "age"
        assert err.row_index == 1
        assert err.trigger == "format_error"
