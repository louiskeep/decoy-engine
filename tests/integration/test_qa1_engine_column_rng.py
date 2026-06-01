"""QA-1-followup (2026-06-01) integration suite for the engine column
RNG hardening sprint. One end-to-end cell per QA-1 fix, asserting the
observable contract through a small pipeline (compile + run / generate)
rather than against the helper directly.

Per the QA-1 spec Step 7 promise. The QA-1 sprint's cross-module
sweep was the gate regression net; these cells pin each fix's
behavior through the real engine pipeline so a future refactor that
breaks one of them surfaces here.

Fixes covered:
- H6: ColumnGenerator instance-local RNG (no module-global side effects)
- H7: reference_date snapshot prevents wall-clock drift
- H9: PoolSampler UNIQUE+deterministic raises GenerationError
- M17: per-column-config null seed (no cross-column null-mask collision)
- M19: missing reference_table raises ValueError
- M21: per-formula RNG isolation (FormulaStrategy via make_mask_globals)
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from decoy_engine.execution._errors import StrategyError  # noqa: F401  (used by referenced strategies)
from decoy_engine.generation.pool import (
    CardinalityMode,
    GenerationError,
    PoolBuilder,
    PoolSampler,
)
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.providers_v2 import ProviderSpec, get_default_registry


_SEED = b"\x00\x00\x00\x00\x00\x00\x00\x2a"  # 42


def test_qa1_h6_no_module_global_pollution_e2e():
    """End-to-end: a third party seeding module-global random.* between
    two pipeline-shaped generator constructions does not change the
    second generator's output."""
    col = {"name": "cat", "type": "categorical",
           "categories": ["a", "b", "c"], "weights": [0.5, 0.3, 0.2]}
    cg_a = ColumnGenerator(seed=42)
    out_a = cg_a.generate_column(20, col, "t", {}).tolist()

    # Pollute aggressively between calls.
    random.seed(999999)
    for _ in range(500):
        random.random()

    cg_b = ColumnGenerator(seed=42)
    out_b = cg_b.generate_column(20, col, "t", {}).tolist()
    assert out_a == out_b


def test_qa1_h7_reference_date_freezes_today_e2e():
    """Two pipelines run on different calendar days against the same
    reference_date produce identical formula output."""
    ref = pd.Timestamp("2026-01-01")
    col = {"name": "y", "type": "formula", "formula": "today()"}
    cg_a = ColumnGenerator(seed=42, reference_date=ref)
    cg_b = ColumnGenerator(seed=42, reference_date=ref)
    out_a = cg_a.generate_column(5, col, "t", {}).tolist()
    out_b = cg_b.generate_column(5, col, "t", {}).tolist()
    assert out_a == out_b
    assert all(v == "2026-01-01" for v in out_a)


def test_qa1_h9_unique_plus_deterministic_raises_e2e():
    """PoolSampler raise contract through the real pool builder."""
    builder = PoolBuilder(get_default_registry())
    pool = builder.build("person_email", size=50, job_seed=_SEED)
    source = pd.Series(["a", "b", "c", "d", "e"])
    with pytest.raises(GenerationError) as excinfo:
        PoolSampler().sample(
            pool, n=5, mode=CardinalityMode.UNIQUE, seed=_SEED,
            source=source, namespace="ns", deterministic=True,
        )
    assert excinfo.value.code == "deterministic_mode_unsupported_cardinality"


def test_qa1_m17_two_columns_distinct_null_masks_e2e():
    """End-to-end: two columns with different configs at the same seed
    no longer share a null mask."""
    col_a = {"name": "first_name", "type": "faker",
             "faker_type": "first_name", "null_probability": 0.5}
    col_b = {"name": "last_name", "type": "faker",
             "faker_type": "last_name", "null_probability": 0.5}
    cg = ColumnGenerator(seed=42)
    out_a = cg.generate_column(100, col_a, "t", {}).tolist()
    out_b = cg.generate_column(100, col_b, "t", {}).tolist()
    nulls_a = {i for i, v in enumerate(out_a) if v is None or pd.isna(v)}
    nulls_b = {i for i, v in enumerate(out_b) if v is None or pd.isna(v)}
    # ~50% each; would be identical pre-fix.
    assert nulls_a != nulls_b


def test_qa1_m19_missing_reference_table_raises_e2e():
    """Missing reference_table no longer returns REF_TABLE_NOT_FOUND_N."""
    col = {"name": "fk", "type": "reference",
           "reference_table": "missing", "reference_column": "id"}
    cg = ColumnGenerator(seed=42)
    with pytest.raises(ValueError, match="reference_table"):
        cg.generate_column(5, col, "t", {})


def test_qa1_m21_two_formula_columns_independent_rng_e2e():
    """Two formula columns in the same generator: column A's output
    must equal itself regardless of column B running first or second."""
    col_a = {"name": "rand_a", "type": "formula",
             "formula": "randint(1, 1000)"}
    col_b = {"name": "rand_b", "type": "formula",
             "formula": "randint(1, 1000)"}

    cg1 = ColumnGenerator(seed=42)
    a_then_b_a = cg1.generate_column(10, col_a, "t", {}).tolist()
    a_then_b_b = cg1.generate_column(10, col_b, "t", {}).tolist()

    cg2 = ColumnGenerator(seed=42)
    b_then_a_b = cg2.generate_column(10, col_b, "t", {}).tolist()
    b_then_a_a = cg2.generate_column(10, col_a, "t", {}).tolist()

    assert a_then_b_a == b_then_a_a
    assert a_then_b_b == b_then_a_b


def test_qa1_m21_mask_side_formula_strategy_uses_factory_rng():
    """QA-1-followup (2026-06-01): mask-side FormulaStrategy now
    constructs a per-formula `random.Random` via `make_mask_globals`.
    Two FormulaStrategy instances applying the same formula to the
    same column name + same input must produce byte-identical output,
    independent of module-global random state pollution."""
    from decoy_engine.transforms.formula import FormulaStrategy

    src = pd.Series([1, 2, 3, 4, 5])
    rule = {"column": "x", "formula": "value + randint(1, 100)"}

    out_a = FormulaStrategy().apply(src.copy(), rule).tolist()

    # Pollute module-global random.
    random.seed(99999)
    for _ in range(500):
        random.random()

    out_b = FormulaStrategy().apply(src.copy(), rule).tolist()
    assert out_a == out_b
