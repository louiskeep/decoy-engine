"""C1b-i: non-deterministic categorical is SEEDED and keyed by handler-frame ordinal.

Plan: docs/plans/2026-10-04-c1b-i-seeded-nondet-categorical-everywhere.md (section 5).

The draw for the non-null row at ordinal ``g = ctx.row_offset + local_index`` is
``derive_index(mask_key, namespace, encode_int(g), pool_size=...)``, with the weighted
variant reduced through the shared CDF. The expected indices below are FROZEN LITERALS
captured once from direct scalar ``derive_index`` calls, so a change to the key encoding,
the CDF, or the key order fails here instead of being recomputed by the code under test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import kernel
from decoy_engine.determinism import derive_index
from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies import SCALAR_HANDLERS, _categorical
from decoy_engine.execution._strategies._categorical import (
    _WEIGHTED_CDF_RES,
    CategoricalStrategyHandler,
)
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution._strategies._orphan import make_remap_fn
from decoy_engine.execution._when_gate import run_with_when_gate
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._index_ext import (
    load_compiled_index_kernel,
    reference_index_derivation,
)
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

MK = (0x0123456789).to_bytes(8, "big")
MK_OTHER = (0x77).to_bytes(8, "big")
CATS = ["A", "B", "C", "D"]

# derive_index(MK, "ns", encode_int(g), pool_size=4) for g in 0..11
KAT_UNIFORM_NS = [2, 3, 1, 3, 0, 0, 0, 1, 3, 3, 1, 1]
# derive_index(MK, "other", encode_int(g), pool_size=4) for g in 0..11
KAT_UNIFORM_OTHER = [1, 1, 3, 0, 3, 0, 2, 3, 2, 2, 2, 1]
# derive_index(MK_OTHER, "ns", encode_int(g), pool_size=4) for g in 0..11
KAT_UNIFORM_OTHER_KEY = [3, 1, 0, 3, 3, 3, 3, 2, 1, 2, 1, 3]
# derive_index(MK, "ns", encode_int(g), pool_size=_WEIGHTED_CDF_RES) for g in 0..7
KAT_BUCKETS_NS = [50862, 705795, 680373, 622207, 733696, 472172, 22976, 460237]
# weights [0.5, 0.3, 0.2] -> cdf [500000, 800000, 1000000]; bisect_right over KAT_BUCKETS_NS
KAT_WEIGHTED_INDEX = [0, 1, 1, 1, 1, 0, 0, 0]


@dataclass(frozen=True)
class _Ctx:
    # A frozen dataclass so the `when` gate's `gated_context` (dataclasses.replace) works on it,
    # the same shape a real StrategyContext has for the fields these tests touch.
    mask_key: bytes = MK
    row_offset: int = 0
    job_seed: bytes = MK
    current_table: str = "t"
    gate_positions: Any = None
    row_errors: list[Any] = field(default_factory=list)


def _seed(
    config: dict[str, Any],
    *,
    namespace: str | None = "ns",
    deterministic: bool = False,
    when: str | None = None,
    strategy: str = "categorical",
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="bijective",
        deterministic=deterministic,
        provider_config=tuple(sorted(config.items())),
        when=when,
    )


def _run(
    values: list[Any],
    config: dict[str, Any] | None = None,
    *,
    ctx: Any = None,
    namespace: str | None = "ns",
) -> list[Any]:
    cfg = config if config is not None else {"categories": CATS}
    out, _ = CategoricalStrategyHandler().run(
        pd.DataFrame({"col": values}), "col", _seed(cfg, namespace=namespace), ctx or _Ctx()
    )
    return out["col"].tolist()


def _cats(indices: list[int], cats: list[str] = CATS) -> list[str]:
    return [cats[i] for i in indices]


# ---- 5.1 reproducibility -------------------------------------------------------------


class TestReproducible:
    def test_uniform_is_byte_identical_across_runs(self) -> None:
        values = [f"v{i}" for i in range(500)]
        assert _run(values) == _run(values)

    def test_weighted_is_byte_identical_across_runs(self) -> None:
        values = [f"v{i}" for i in range(500)]
        cfg = {"categories": CATS, "weights": [4.0, 3.0, 2.0, 1.0]}
        assert _run(values, cfg) == _run(values, cfg)

    def test_frozen_uniform_vector(self) -> None:
        assert _run(["x"] * 12) == _cats(KAT_UNIFORM_NS)

    def test_frozen_weighted_vector(self) -> None:
        cfg = {"categories": ["X", "Y", "Z"], "weights": [0.5, 0.3, 0.2]}
        assert _run(["x"] * 8, cfg) == _cats(KAT_WEIGHTED_INDEX, ["X", "Y", "Z"])

    def test_buckets_literal_matches_the_primitive(self) -> None:
        """Pins the literal table the weighted KAT relies on to the live primitive."""
        got = [
            derive_index(MK, "ns", kernel.encode_int(g), pool_size=_WEIGHTED_CDF_RES)
            for g in range(8)
        ]
        assert got == KAT_BUCKETS_NS

    def test_different_namespace_changes_output(self) -> None:
        assert _run(["x"] * 12, namespace="other") == _cats(KAT_UNIFORM_OTHER)
        assert KAT_UNIFORM_OTHER != KAT_UNIFORM_NS

    def test_different_mask_key_changes_output(self) -> None:
        assert _run(["x"] * 12, ctx=_Ctx(MK_OTHER)) == _cats(KAT_UNIFORM_OTHER_KEY)
        assert KAT_UNIFORM_OTHER_KEY != KAT_UNIFORM_NS

    def test_namespace_is_required(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _run(["x", "y"], namespace=None)
        assert exc.value.code == "categorical_requires_namespace"

    def test_no_unseeded_generator_remains_in_the_module(self) -> None:
        import inspect

        src = inspect.getsource(_categorical)
        assert "default_rng" not in src
        assert "np.random" not in src


# ---- 5.2 position keyed, not value keyed ---------------------------------------------


class TestPositionKeyed:
    def test_same_value_at_two_positions_can_differ(self) -> None:
        out = _run(["same"] * 12)
        assert out[0] != out[1]  # literal positions 0 and 1 draw indices 2 and 3

    def test_different_values_at_same_position_give_same_category(self) -> None:
        a = _run([f"alpha{i}" for i in range(12)])
        b = _run([f"zzz-{i * 7}" for i in range(12)])
        assert a == b == _cats(KAT_UNIFORM_NS)

    def test_exact_output_equals_scalar_derive_index_at_frozen_literals(self) -> None:
        out = _run(["x"] * 12)
        assert [CATS.index(c) for c in out] == KAT_UNIFORM_NS

    def test_null_stays_null_and_nulls_do_not_shift_later_rows(self) -> None:
        values: list[Any] = ["x"] * 12
        baseline = _run(values)
        with_null = list(values)
        with_null[2] = None
        out = _run(with_null)
        assert out[2] is None
        # Position g=2 is consumed by the null: every other row keeps its baseline draw.
        assert [o for i, o in enumerate(out) if i != 2] == [
            b for i, b in enumerate(baseline) if i != 2
        ]
        # Replacing the null with a non-null changes only that row.
        assert out[3:] == baseline[3:]

    def test_nan_and_none_are_both_null(self) -> None:
        out = _run(["x", float("nan"), None, "y"])
        assert out[1] is None and out[2] is None
        assert out[0] == CATS[KAT_UNIFORM_NS[0]]
        assert out[3] == CATS[KAT_UNIFORM_NS[3]]

    def test_row_offset_shifts_the_key(self) -> None:
        out = _run(["x"] * 4, ctx=_Ctx(row_offset=4))
        assert [CATS.index(c) for c in out] == KAT_UNIFORM_NS[4:8]


# ---- 5.3 key encoding forward-compat --------------------------------------------------


class TestKeyEncoding:
    def test_strategy_uses_the_public_kernel_encoder(self) -> None:
        assert _categorical.encode_int is kernel.encode_int

    def test_encoded_bytes_are_the_documented_canonical_integer_form(self) -> None:
        assert kernel.encode_int(0) == b"\x00\x00\x00\x01\x00"
        assert kernel.encode_int(7) == b"\x00\x00\x00\x01\x07"
        assert kernel.encode_int(300) == b"\x00\x00\x00\x02\x01,"

    def _kernels(self) -> list[Any]:
        ks = [reference_index_derivation()]
        if native_companion_status().ok:
            ks.append(load_compiled_index_kernel())
        return ks

    def test_batch_kernel_on_an_int_column_equals_the_oracle_indices(self) -> None:
        """C1b-ii feeds a global-index int column to the batch kernel; it must match."""
        for k in self._kernels():
            batch = k.derive_index_batch(
                pa.array(range(12), type=pa.int64()), mask_key=MK, namespace="ns", pool_size=4
            )
            assert batch.to_pylist() == KAT_UNIFORM_NS
            wide = k.derive_index_batch(
                pa.array(range(8), type=pa.int64()),
                mask_key=MK,
                namespace="ns",
                pool_size=_WEIGHTED_CDF_RES,
            )
            assert wide.to_pylist() == KAT_BUCKETS_NS


# ---- 5.4 distribution -----------------------------------------------------------------


class TestDistribution:
    def test_uniform_frequencies(self) -> None:
        n = 8000
        out = _run([f"v{i}" for i in range(n)])
        for c in CATS:
            assert abs(out.count(c) / n - 0.25) < 0.03

    def test_weighted_frequencies_follow_the_weights(self) -> None:
        n = 20000
        cfg = {"categories": CATS, "weights": [9.0, 1.0, 0.0, 2.0]}
        out = _run([f"v{i}" for i in range(n)], cfg)
        assert out.count("C") == 0
        total = 12.0
        for c, w in zip(CATS, [9.0, 1.0, 0.0, 2.0], strict=True):
            assert abs(out.count(c) / n - w / total) < 0.02

    def test_all_picks_are_valid_categories(self) -> None:
        out = _run([f"v{i}" for i in range(500)], {"categories": ["X", "Y"]})
        assert len(out) == 500 and set(out) == {"X", "Y"}

    def test_nonpositive_weights_still_raise(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _run(["a"], {"categories": ["X", "Y"], "weights": [0.0, 0.0]})
        assert exc.value.code == "categorical_weights_nonpositive"


# ---- 5.5 determinism-mode separation ---------------------------------------------------


class TestModeSeparation:
    def test_deterministic_mode_is_still_value_keyed(self) -> None:
        values = ["alice", "bob", "alice", "carol"]
        out, _ = CategoricalStrategyHandler().run(
            pd.DataFrame({"col": values}),
            "col",
            _seed({"categories": CATS}, deterministic=True),
            _Ctx(),
        )
        got = out["col"].tolist()
        assert got[0] == got[2]  # same source value, same category
        expected = [
            CATS[derive_index(MK, "ns", kernel.canonicalize_derive_source(v), pool_size=len(CATS))]
            for v in values
        ]
        assert got == expected

    def test_nondeterministic_output_ignores_source_values(self) -> None:
        a = _run(["alice", "bob", "alice", "carol"])
        b = _run(["x", "x", "x", "x"])
        assert a == b


# ---- 5.8 nested whole-frame leaf ordinal ------------------------------------------------


class TestNestedWholeFrame:
    def _nested_run(self, cells: list[Any]) -> list[Any]:
        seed = _seed(
            {
                "target": "$.items[*].k",
                "strategy": "categorical",
                "strategy_config": {"categories": CATS},
            },
            strategy="nested",
        )
        df = pd.DataFrame({"data": cells})
        out, _ = NestedStrategyHandler().run(df, "data", seed, _Ctx())
        return out["data"].tolist()

    def _leaves(self, masked: list[Any]) -> list[str]:
        out: list[str] = []
        for cell in masked:
            if cell is None:
                continue
            for item in json.loads(cell).get("items", []):
                out.append(item["k"])
        return out

    def test_multiple_leaves_sparse_rows_and_nulls_use_leaf_ordinals_from_zero(self) -> None:
        cells: list[Any] = [
            json.dumps({"items": [{"k": "a"}, {"k": "b"}, {"k": "c"}]}),
            json.dumps({"other": 1}),  # sparse: no matching leaf
            None,
            json.dumps({"items": [{"k": "d"}, {"k": None}, {"k": "e"}]}),  # null leaf
            json.dumps({"items": [{"k": "f"}]}),
        ]
        first = self._nested_run(list(cells))
        assert first == self._nested_run(list(cells))  # reproducible
        # Leaf ordinals count only collected leaves, from zero, in outer-row order.
        # Leaves: a b c d <null> e f -> 7 leaves; the null leaf keeps its ordinal.
        got = self._leaves(first)
        expected_idx = KAT_UNIFORM_NS[:7]
        expected = [CATS[i] for i in expected_idx]
        expected[4] = None  # type: ignore[call-overload]
        assert [g for i, g in enumerate(got) if i != 4] == [
            e for i, e in enumerate(expected) if i != 4
        ]

    def test_leaf_values_do_not_influence_the_draw(self) -> None:
        a = [json.dumps({"items": [{"k": "a"}, {"k": "b"}]})]
        b = [json.dumps({"items": [{"k": "zzz"}, {"k": "yyy"}]})]
        assert self._leaves(self._nested_run(a)) == self._leaves(self._nested_run(b))


# ---- 5.11 when: / FK / orphan reproducibility -------------------------------------------


class TestHandlerFrameOrdinal:
    def test_when_gate_keys_by_full_table_row_and_is_reproducible(self) -> None:
        # C8-iii-d: a `when:`-selected row keys on its full-table row, so its value equals the
        # same row's value in an ungated run (not the match ordinal). KAT_UNIFORM_NS[p] is the
        # ungated value at row p.
        def go() -> pd.DataFrame:
            df = pd.DataFrame({"col": [f"v{i}" for i in range(6)], "keep": [0, 1, 0, 1, 1, 0]})
            plan = _seed({"categories": CATS}, when="keep == 1")
            out, _ = run_with_when_gate(CategoricalStrategyHandler(), df, "col", plan, _Ctx())
            return out

        out = go()
        assert out.equals(go())
        # Rows {1,3,4} are keyed on their full-table rows {1,3,4}; unmatched rows are untouched.
        assert out["col"].tolist()[1] == CATS[KAT_UNIFORM_NS[1]]
        assert out["col"].tolist()[3] == CATS[KAT_UNIFORM_NS[3]]
        assert out["col"].tolist()[4] == CATS[KAT_UNIFORM_NS[4]]
        assert [out["col"].tolist()[i] for i in (0, 2, 5)] == ["v0", "v2", "v5"]

    def test_when_gate_selected_rows_equal_the_ungated_run(self) -> None:
        # The core C8-iii-d invariance property, asserted directly against an ungated baseline.
        src = [f"v{i}" for i in range(6)]
        base_df = pd.DataFrame({"col": list(src)})
        base, _ = CategoricalStrategyHandler().run(
            base_df, "col", _seed({"categories": CATS}), _Ctx()
        )
        baseline = base["col"].tolist()
        df = pd.DataFrame({"col": list(src), "keep": [0, 1, 0, 1, 1, 0]})
        gated, _ = run_with_when_gate(
            CategoricalStrategyHandler(),
            df,
            "col",
            _seed({"categories": CATS}, when="keep == 1"),
            _Ctx(),
        )
        got = gated["col"].tolist()
        for i in (1, 3, 4):
            assert got[i] == baseline[i]
        for i in (0, 2, 5):
            assert got[i] == src[i]

    def test_when_gate_with_nulls_in_the_matched_subset(self) -> None:
        # Selected rows {0,1,3}; row 1 is null and stays null. Keys are full-table rows 0 and 3.
        df = pd.DataFrame({"col": ["a", None, "c", "d"], "keep": [1, 1, 0, 1]})
        plan = _seed({"categories": CATS}, when="keep == 1")
        out, _ = run_with_when_gate(CategoricalStrategyHandler(), df, "col", plan, _Ctx())
        got = out["col"].tolist()
        assert got[0] == CATS[KAT_UNIFORM_NS[0]]
        assert got[1] is None
        assert got[2] == "c"
        assert got[3] == CATS[KAT_UNIFORM_NS[3]]

    def test_orphan_remap_frame_keys_by_synthetic_frame_ordinal(self) -> None:
        pseed = _seed({"categories": CATS})
        node = SimpleNamespace(plan_slice=pseed, strategy="categorical")
        edge = SimpleNamespace(parent_table="p", parent_columns=("pk",))
        remap = make_remap_fn(
            edge,  # type: ignore[arg-type]
            {("p", ("pk",)): node},  # type: ignore[dict-item]
            _Ctx(),  # type: ignore[arg-type]
            {"categorical": CategoricalStrategyHandler()},
        )
        keys = [("o1",), ("o2",), ("o3",), ("o4",)]
        out = remap(keys)
        assert out == remap(keys)
        assert [k[0] for k in out] == _cats(KAT_UNIFORM_NS[:4])

    def test_pipeline_when_and_unmatched_rows_are_reproducible(self, tmp_path: Any) -> None:
        from decoy_engine.execution import run_pipeline
        from tests.unit.execution import _multi_table_support as mt

        n = 40
        table = pa.table(
            {
                "v": pa.array([f"s{i}" for i in range(n)]),
                "keep": pa.array([i % 3 == 0 for i in range(n)]),
            }
        )
        cols = [
            {
                "name": "v",
                "strategy": "categorical",
                "deterministic": False,
                "namespace": "cat_ns",
                "provider_config": {"categories": CATS},
            },
            {"name": "keep", "strategy": "passthrough"},
        ]
        cfg, _ = mt.build_job(tmp_path, {"t": (cols, table)})
        # `when` is not part of the validated config schema; it is set on the compiled dict.
        cfg["tables"][0]["columns"][0]["when"] = "keep == True"
        a = run_pipeline(cfg, sources={"t": table}, **mt.kw(auto_chunk=False))
        b = run_pipeline(cfg, sources={"t": table}, **mt.kw(auto_chunk=False))
        assert a.outputs["t"].column("v").to_pylist() == b.outputs["t"].column("v").to_pylist()
        masked = [
            v
            for v, k in zip(
                a.outputs["t"].column("v").to_pylist(), table["keep"].to_pylist(), strict=True
            )
            if k
        ]
        assert set(masked) <= set(CATS) and masked
        untouched = [
            v
            for v, k in zip(
                a.outputs["t"].column("v").to_pylist(), table["keep"].to_pylist(), strict=True
            )
            if not k
        ]
        assert untouched == [f"s{i}" for i in range(n) if i % 3 != 0]


# ---- end to end through the adapter -------------------------------------------------------


def test_adapter_run_is_reproducible_with_the_same_job_seed() -> None:
    seed = ColumnSeed(
        namespace="adapter_ns",
        strategy="categorical",
        provider="categorical",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("categories", CATS),),
        coherent_with=(),
    )
    plan: Any = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=MK,
            per_table=(("t", TableSeed(per_column=(("g", seed),), per_group=())),),
        )
    )
    src = pa.table({"g": ["x"] * 200})

    def go() -> list[Any]:
        res = PandasExecutionAdapter().run_single(
            plan,
            src,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        return res.output.column("g").to_pylist()

    assert go() == go()
    assert set(go()) == set(CATS)


def test_handler_registry_still_maps_categorical() -> None:
    assert isinstance(SCALAR_HANDLERS["categorical"], CategoricalStrategyHandler)
