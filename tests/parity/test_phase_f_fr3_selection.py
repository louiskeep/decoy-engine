"""Phase F FR3: kernel selection preserves each distribution, order and membership.

The unified core draws uniform and weighted selections from the shared HMAC index kernel. The
re-baseline permits the emitted values to change; it does not permit the distribution to. These
tests check the claims the plan's section 6 lists for F3:

- UNIFORMITY of the uniform-adopted columns (categorical, reference random, pooled Faker);
- WEIGHT PRESERVATION of the weighted ones (a configured 90/10 stays 90/10);
- SEQUENCE order, uniqueness, step and formatting;
- identical values with and without the compiled companion;
- the Python reference reachable only for non-admitted cells (and companion-absent hosts);
- mutants that flatten a weight, bias a frequency or break membership each fail the gate.

Statistical bounds are five binomial standard deviations on a fixed seed, so a result is exactly
repeatable rather than flaky.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pyarrow as pa
import pytest

from decoy_engine.generation import _column_streams, _kernel_select
from decoy_engine.generation.pool import _sampler
from decoy_engine.generators.derivation import GenDeriveContext
from tests.parity import _f0_fixtures as fx
from tests.parity import _fr0_refreeze as rf
from tests.parity import _fr0_semantic_gate as gate
from tests.parity._fr0_support import fingerprints, partition_from_cuts, run_resident

BIG = 200_000


def _table(rows: int, cols: list[dict[str, Any]]) -> dict[str, pa.Table]:
    return run_resident(fx.config([fx.table("t", rows, cols)]))


# ---- uniformity ------------------------------------------------------------------------------


def test_uniform_categorical_is_uniform_over_its_categories() -> None:
    cats = ["a", "b", "c", "d", "e"]
    t = _table(BIG, [{"name": "u", "type": "categorical", "categories": cats}])
    gate.check_marginal(t["t"].column("u").to_pylist(), cats)


def test_uniform_categorical_over_numbers_and_booleans() -> None:
    t = _table(
        BIG,
        [
            {"name": "i", "type": "categorical", "categories": [1, 2, 3]},
            {"name": "b", "type": "categorical", "categories": [True, False]},
            {"name": "f", "type": "categorical", "categories": [0.5, 1.5, 2.5, 3.5]},
        ],
    )["t"]
    gate.check_marginal(t.column("i").to_pylist(), [1, 2, 3])
    gate.check_marginal(t.column("b").to_pylist(), [True, False])
    gate.check_marginal(t.column("f").to_pylist(), [0.5, 1.5, 2.5, 3.5])


def test_a_duplicated_category_keeps_its_list_multiplicity() -> None:
    t = _table(BIG, [{"name": "d", "type": "categorical", "categories": ["x", "x", "y"]}])
    gate.check_marginal(t["t"].column("d").to_pylist(), ["x", "y"], [2, 1])


def test_a_single_category_column_is_constant() -> None:
    t = _table(500, [{"name": "one", "type": "categorical", "categories": ["only"]}])
    assert set(t["t"].column("one").to_pylist()) == {"only"}


def test_uniform_reference_is_uniform_over_the_parent_keys_and_inside_them() -> None:
    out = rf.large_reference()
    rf.g_reference_core(out)
    rf.g_reference_core_marginal(out)


def test_pooled_faker_is_uniform_over_its_pool() -> None:
    out = fx.run_generate_tables(fx.cfg_pooled_faker())
    rf.g_pooled(out)


# ---- weight preservation ---------------------------------------------------------------------


def test_ninety_ten_categorical_stays_ninety_ten() -> None:
    t = _table(
        BIG,
        [{"name": "w", "type": "categorical", "categories": ["hot", "cold"], "weights": [90, 10]}],
    )
    values = t["t"].column("w").to_pylist()
    gate.check_marginal(values, ["hot", "cold"], [90, 10])
    # The failure this guards: a uniform kernel draw would put each category near one half.
    assert values.count("hot") / len(values) > 0.88


@pytest.mark.parametrize(
    "weights",
    [[5, 3, 2, 1], [1, 1, 1, 1], [97, 1, 1, 1], [1, 0, 1, 2], [0.001, 0.499, 0.5, 0.0]],
)
def test_weighted_categorical_matches_its_configured_weights(weights: list[float]) -> None:
    cats = ["a", "b", "c", "d"]
    t = _table(BIG, [{"name": "w", "type": "categorical", "categories": cats, "weights": weights}])
    values = t["t"].column("w").to_pylist()
    gate.check_marginal(values, cats, weights)
    for cat, weight in zip(cats, weights, strict=True):
        if weight == 0:
            assert cat not in set(values), "a zero-weight category was drawn"


def test_weighted_reference_matches_its_configured_weights() -> None:
    out = rf.large_reference()
    gate.check_marginal(
        out["rc_child"].column("r_weighted").to_pylist(), rf.REF_KEYS, [1, 2, 3, 4, 5, 6]
    )


def test_declined_weights_keep_random_choices_and_their_weights() -> None:
    """A weight the 2**32-slot CDF cannot hold stays on random.choices, still weighted."""
    weights = [1, 1e-12, 2]
    cats = ["a", "b", "c"]
    col = {"name": "w", "type": "categorical", "categories": cats, "weights": weights}
    stream = _column_streams.CategoricalStream(col, GenDeriveContext.for_column(None, col, 1))
    assert stream._indexer is None and stream._rng is not None
    t = _table(BIG, [col])
    gate.check_marginal(t["t"].column("w").to_pylist(), cats, weights)


# ---- sequence --------------------------------------------------------------------------------


def test_sequence_keeps_order_uniqueness_step_and_format_at_scale() -> None:
    cols = [
        {"name": "plain", "type": "sequence", "start": 1},
        {
            "name": "fmt",
            "type": "sequence",
            "start": 100,
            "step": 7,
            "prefix": "INV-",
            "suffix": "/x",
            "pad_length": 8,
        },
        {"name": "down", "type": "sequence", "start": 500, "step": -3},
    ]
    t = run_resident(
        fx.config([fx.table("t", 100_000, cols)]), partition_from_cuts({"t": [1, 4_096, 77_777]})
    )["t"]
    gate.check_sequence(t.column("plain").to_pylist(), start=1, step=1)
    gate.check_sequence(
        t.column("fmt").to_pylist(), start=100, step=7, prefix="INV-", suffix="/x", pad=8
    )
    gate.check_sequence(t.column("down").to_pylist(), start=500, step=-3)


def test_sequence_is_not_a_kernel_draw() -> None:
    """The stream must not touch the index kernel at all (it would destroy order)."""
    import inspect

    assert "_kernel_select" not in inspect.getsource(_column_streams.SequenceStream)
    stream = _column_streams.SequenceStream({"start": 1})
    assert stream.next_batch(0, 4) == ["1", "2", "3", "4"]
    assert stream.next_batch(4, 2) == ["5", "6"]


def test_the_sequence_and_sequential_reference_goldens_are_the_pre_fr3_bytes() -> None:
    from tests.parity._f0_canonical import assert_matches_oracle

    assert_matches_oracle("sequence", fx.FIXTURES_BY_ID["sequence"].run())


# ---- the host does not change a value --------------------------------------------------------


def _core_fixture_outputs() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for fid in ("categorical", "pooled_faker", "nulls", "reference_core"):
        out[fid] = fingerprints(fx.FIXTURES_BY_ID[fid].run())
    return out


def test_values_are_identical_with_and_without_the_compiled_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_host = _core_fixture_outputs()
    monkeypatch.setattr(_sampler, "_compiled_index_kernel", lambda: None)
    assert _core_fixture_outputs() == with_host


def test_every_admitted_column_requires_the_index_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise AssertionError("index kernel looked up")

    cfg = fx.config(
        [
            fx.table(
                "t",
                50,
                [
                    {"name": "s", "type": "sequence", "start": 1},
                    {"name": "live", "type": "faker", "faker_type": "email"},
                    {
                        "name": "py",
                        "type": "categorical",
                        "categories": ["x", "y", "z"],
                        "weights": [1, 1e-12, 2],
                    },
                    {
                        "name": "nm",
                        "type": "categorical",
                        "categories": ["a"],
                        "null_probability": 0.5,
                    },
                ],
            )
        ]
    )
    monkeypatch.setattr(_sampler, "_compiled_index_kernel", boom)
    monkeypatch.setattr(_sampler, "_reference_index_kernel", boom)
    # Non-admitted columns (sequence, per-row Faker, declined weights) never look the kernel up.
    plain = cfg["tables"][0]["generate_columns"]
    cfg["tables"][0]["generate_columns"] = [c for c in plain if c["name"] != "nm"]
    assert run_resident(cfg)["t"].num_rows == 50
    # An admitted column needs it: with both lookups poisoned the run fails loudly.
    cfg["tables"][0]["generate_columns"] = plain
    with pytest.raises(AssertionError, match="index kernel looked up"):
        run_resident(cfg)


# ---- mutants: each must fail the semantic gate -----------------------------------------------


def _replace(table: pa.Table, name: str, values: list[Any]) -> pa.Table:
    idx = table.column_names.index(name)
    return table.set_column(idx, name, pa.array(values, type=table.schema.field(name).type))


def test_flattening_a_weighted_categorical_to_uniform_fails_the_gate() -> None:
    builder, check = rf.LARGE_GATES["categorical"]
    sample = builder()
    check(sample)
    uniform = np.random.default_rng(1).choice(
        ["red", "green", "blue", "amber"], size=len(sample["cat"])
    )
    flat = {"cat": _replace(sample["cat"], "weighted", uniform.tolist())}
    with pytest.raises(AssertionError, match="frequency"):
        check(flat)


def test_flattening_a_weighted_reference_to_uniform_fails_the_gate() -> None:
    sample = rf.large_reference()
    rf.g_reference_core_marginal(sample)
    child = sample["rc_child"]
    uniform = np.random.default_rng(1).choice(rf.REF_KEYS, size=child.num_rows).tolist()
    flat = {**sample, "rc_child": _replace(child, "r_weighted", uniform)}
    with pytest.raises(AssertionError, match="frequency"):
        rf.g_reference_core_marginal(flat)


def test_frequency_biased_pooled_column_fails_the_pool_gate() -> None:
    out = fx.run_generate_tables(fx.cfg_pooled_faker())
    rf.g_pooled(out)
    pooled = out["pooled"]
    values = pooled.column("city").to_pylist()
    for i in range(0, len(values), 7):
        values[i] = values[0]
    with pytest.raises(AssertionError, match="chi-square"):
        rf.g_pooled({"pooled": _replace(pooled, "city", values)})


def test_a_foreign_reference_key_fails_the_membership_gate() -> None:
    sample = fx.FIXTURES_BY_ID["reference_core"].run()
    rf.g_reference_core(sample)
    child = sample["rc_child"]
    values = child.column("r_random").to_pylist()
    values[3] = "P-99"
    with pytest.raises(AssertionError, match="not in the parent key set"):
        rf.g_reference_core({**sample, "rc_child": _replace(child, "r_random", values)})


def test_a_sequence_replaced_by_random_draws_fails_the_sequence_gate() -> None:
    n = 5_000
    shuffled = np.random.default_rng(3).permutation(n).tolist()
    with pytest.raises(AssertionError, match="sequence order"):
        gate.check_sequence([str(i + 1) for i in shuffled], start=1, step=1)
    drawn = np.random.default_rng(4).integers(1, n, size=n).tolist()
    with pytest.raises(AssertionError, match="sequence order"):
        gate.check_sequence([str(v) for v in drawn], start=1, step=1)


def test_a_sorted_but_degenerate_selection_fails_the_marginal_gate() -> None:
    """Index = row mod n is uniform in aggregate; the weighted gate still catches a weight loss."""
    cats = ["a", "b"]
    degenerate = [cats[i % 2] for i in range(10_000)]
    with pytest.raises(AssertionError, match="frequency"):
        gate.check_marginal(degenerate, cats, [90, 10])


def test_the_kernel_select_module_is_what_the_streams_use() -> None:
    col = {"name": "c", "categories": ["a", "b"]}
    ctx = GenDeriveContext.for_column(None, col, 1)
    assert isinstance(
        _column_streams.CategoricalStream(col, ctx)._indexer, _kernel_select.PositionalIndexer
    )
