"""S6-ENG-1: the v2 generation config contract + the generation op spine.

Reading B (parity-frozen vs V1 DataGenerator). S6-ENG-1 ships the spine + the
``sequence`` generator and the gate "a single-column generate config produces
row_count rows on the v2 path"; the per-generator V1-parity tests land in S6-ENG-2.
Also pins the mask contract is unchanged (mask still requires a source; a table is
mask XOR generate).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decoy_engine.config import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables


def _generate_config(row_count: int = 5) -> dict:
    return {
        "version": 1,
        "mode": "generate",
        "global_settings": {"seed": 42},
        "sources": {},
        "tables": [
            {
                "name": "customers",
                "row_count": row_count,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1000, "step": 1},
                ],
            }
        ],
        "targets": {"customers": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _mask_config() -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {"customers": {"type": "file", "format": "csv", "path": "in.csv"}},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "person_email",
                        "namespace": "ns",
                        "deterministic": True,
                    }
                ],
            }
        ],
        "targets": {"customers": {"type": "file", "format": "csv", "path": "o.csv"}},
    }


class TestGenerateConfigContract:
    def test_generate_config_validates(self):
        cfg = PipelineConfig.model_validate(_generate_config()).model_dump()
        assert cfg["mode"] == "generate"
        assert cfg["sources"] == {}
        assert cfg["tables"][0]["row_count"] == 5
        assert cfg["tables"][0]["generate_columns"][0]["type"] == "sequence"

    def test_generate_column_carries_flat_params(self):
        # extra="allow" mirror-V1: per-type params (start/step) ride flat.
        cfg = PipelineConfig.model_validate(_generate_config()).model_dump()
        col = cfg["tables"][0]["generate_columns"][0]
        assert col["start"] == 1000 and col["step"] == 1

    def test_generate_table_requires_row_count(self):
        cfg = _generate_config()
        del cfg["tables"][0]["row_count"]
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_table_cannot_be_both_mask_and_generate(self):
        cfg = _generate_config()
        cfg["tables"][0]["columns"] = [{"name": "x", "strategy": "faker"}]
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_generate_mode_rejects_mask_columns(self):
        # mode generate but a table declares mask columns (no generate_columns).
        cfg = _generate_config()
        cfg["tables"][0] = {
            "name": "customers",
            "columns": [{"name": "x", "strategy": "faker"}],
        }
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)


class TestMaskContractUnchanged:
    def test_mask_config_still_validates(self):
        out = PipelineConfig.model_validate(_mask_config()).model_dump()
        assert out["mode"] == "mask"  # default when omitted

    def test_mask_mode_still_requires_sources(self):
        cfg = _mask_config()
        cfg["sources"] = {}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_mask_table_still_requires_columns(self):
        cfg = _mask_config()
        cfg["tables"][0]["columns"] = []
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)


class TestGenerationOpSpine:
    def test_produces_row_count_rows(self):
        # THE S6-ENG-1 GATE: a single-column generate config produces row_count rows.
        # ENG-2 M1 fix: V1 sequence ALWAYS returns strings (columns.py:305-319 wraps
        # every value through f"{prefix}{value_str}{suffix}"), even unformatted.
        cfg = PipelineConfig.model_validate(_generate_config(row_count=7)).model_dump()
        tables = generate_tables(cfg)
        assert set(tables) == {"customers"}
        t = tables["customers"]
        assert t.num_rows == 7
        assert t.column_names == ["id"]
        assert t.column("id").to_pylist() == [
            "1000", "1001", "1002", "1003", "1004", "1005", "1006",
        ]

    def test_zero_rows(self):
        cfg = PipelineConfig.model_validate(_generate_config(row_count=0)).model_dump()
        tables = generate_tables(cfg)
        assert tables["customers"].num_rows == 0

    def test_sequence_formatting(self):
        cfg = _generate_config(row_count=3)
        cfg["tables"][0]["generate_columns"][0] = {
            "name": "acct",
            "type": "sequence",
            "start": 1,
            "step": 1,
            "prefix": "ACCT-",
            "pad_length": 4,
        }
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        vals = generate_tables(cfg)["customers"].column("acct").to_pylist()
        assert vals == ["ACCT-0001", "ACCT-0002", "ACCT-0003"]

    # The runtime "not yet implemented" ValueError is gone (all four generators
    # land in S6-ENG-2). The closed Literal on GenerateColumnConfig.type now
    # enforces the supported set at validation; the dispatch's defensive `else`
    # only fires on a bypass (an unvalidated dict), which is a programmer error
    # not worth a happy-path test.


# ----------------------------------------------------------------------------
# V1 parity oracle (Reading B)
# ----------------------------------------------------------------------------
# Dennis S6-ENG-2 plan: a shared helper runs V1 ``ColumnGenerator`` against the v2
# column dict shape (which mirrors V1's flat column dict per Q-S6-1) and returns
# the values list. Each parity test class asserts ``_v2_run == _v1_run`` for its
# generator under a fixed seed + ``derive_key=None``. ENG-4 adds the derive-key
# determinism tests; ENG-2 pins seed-only-path V1 equivalence.


def _v1_run(col: dict, n: int, seed: int = 42) -> list:
    """Run V1 ``ColumnGenerator.generate_column`` (the public entry, including the
    ``null_probability`` post-process) against a single v2-shape column dict and
    return the values list -- the parity oracle covers BOTH the generator output
    AND the null injection."""
    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=None)
    return cg.generate_column(n, col, "t", {}).tolist()


def _v2_run(col: dict, n: int, seed: int = 42) -> list:
    """Run v2 ``generate_tables`` on a single-column generate config and return the
    values list for that column."""
    cfg = {
        "version": 1,
        "mode": "generate",
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": [{"name": "t", "row_count": n, "generate_columns": [col]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
    }
    cfg = PipelineConfig.model_validate(cfg).model_dump()
    return generate_tables(cfg)["t"].column(col["name"]).to_pylist()


class TestSequenceParityV1:
    """Reading B: v2 ``sequence`` is byte-identical to V1 ``_generate_sequence_column``."""

    def test_unformatted(self):
        # M1 oracle: V1 returns STRINGS even with no prefix/suffix/pad. The v2 spine
        # used to return ints here -- corrected in this sub-commit of S6-ENG-2.
        col = {"name": "id", "type": "sequence", "start": 1, "step": 1}
        assert _v2_run(col, 5) == _v1_run(col, 5)

    def test_formatted_pad(self):
        col = {
            "name": "acct",
            "type": "sequence",
            "start": 1,
            "step": 1,
            "prefix": "ACCT-",
            "pad_length": 4,
        }
        assert _v2_run(col, 3) == _v1_run(col, 3)

    def test_step_and_start(self):
        col = {"name": "n", "type": "sequence", "start": 100, "step": 7}
        assert _v2_run(col, 10) == _v1_run(col, 10)


class TestCategoricalParityV1:
    """Reading B: v2 ``categorical`` is byte-identical to V1 ``_generate_categorical_column``
    under the same seed (no ``derive_key``)."""

    def test_weighted(self):
        col = {
            "name": "tier",
            "type": "categorical",
            "categories": ["A", "B", "C"],
            "weights": [10, 1, 1],
        }
        assert _v2_run(col, 20) == _v1_run(col, 20)

    def test_uniform_no_weights(self):
        col = {
            "name": "flavor",
            "type": "categorical",
            "categories": ["red", "green", "blue"],
        }
        assert _v2_run(col, 15) == _v1_run(col, 15)

    def test_default_categories(self):
        # V1 default when `categories` omitted is ["Category A", "Category B"];
        # the v2 helper carries the same default (mirror V1).
        col = {"name": "x", "type": "categorical"}
        assert _v2_run(col, 5) == _v1_run(col, 5)


class TestFakerParityV1:
    """Reading B: v2 ``faker`` is byte-identical to V1 ``_generate_faker_column`` +
    the V1 ``generate_column`` null_probability post-process under fixed seed."""

    def test_provider_no_kwargs(self):
        col = {"name": "fn", "type": "faker", "faker_type": "first_name"}
        assert _v2_run(col, 10) == _v1_run(col, 10)

    def test_provider_with_kwargs(self):
        # `pyint` with min / max via faker_kwargs.
        col = {
            "name": "n",
            "type": "faker",
            "faker_type": "pyint",
            "faker_kwargs": {"min_value": 0, "max_value": 100},
        }
        assert _v2_run(col, 10) == _v1_run(col, 10)

    def test_unknown_faker_type_falls_back_to_word(self):
        # V1: unknown faker_type silently falls back to providers["word"]
        # (columns.py:246-247). The v2 mirrors the silent fallback.
        col = {
            "name": "x",
            "type": "faker",
            "faker_type": "this_provider_does_not_exist",
        }
        assert _v2_run(col, 5) == _v1_run(col, 5)

    def test_null_injection(self):
        # null_probability is V1's generic post-process; parity covers both the
        # per-row faker value AND the null/non-null row positions.
        col = {
            "name": "fn",
            "type": "faker",
            "faker_type": "first_name",
            "null_probability": 0.3,
        }
        assert _v2_run(col, 20) == _v1_run(col, 20)


class TestFormulaParityV1:
    """Reading B: v2 ``formula`` is byte-identical to V1 ``_generate_formula_column``
    + V1 ``generate_column`` null injection under fixed seed. V1's inline path is
    delegated to (pragmatic parity for the generic expression-eval machinery);
    references-deferred and empty-formula paths return ``[None] * n`` as V1 does."""

    def test_numeric_expression(self):
        # Inline path: a deterministic per-row Python expression.
        col = {"name": "twice", "type": "formula", "formula": "i * 2"}
        assert _v2_run(col, 5) == _v1_run(col, 5)

    def test_inline_with_random(self):
        # Inline path: random.randint(1, 100) is row-seeded via local_seed.
        col = {
            "name": "rand",
            "type": "formula",
            "formula": "random.randint(1, 100)",
        }
        assert _v2_run(col, 10) == _v1_run(col, 10)

    def test_references_defers_to_post_pass(self):
        # V1 returns [None]*n for the per-column phase when `references` is set
        # (DataGenerator._process_referenced_formulas fills them later); v2 mirrors
        # the placeholder behavior. The v2 post-pass machinery is a later sprint.
        col = {
            "name": "greet",
            "type": "formula",
            "formula": "f'Hello {name}'",
            "references": ["name"],
        }
        assert _v2_run(col, 5) == _v1_run(col, 5)
        assert _v2_run(col, 5) == [None] * 5

    def test_empty_formula_returns_nulls(self):
        col = {"name": "x", "type": "formula", "formula": ""}
        assert _v2_run(col, 5) == _v1_run(col, 5)
        assert _v2_run(col, 5) == [None] * 5


# ----------------------------------------------------------------------------
# Multi-table parity helpers (S6-ENG-3: reference / mint-a-pool)
# ----------------------------------------------------------------------------


def _v1_run_multi(tables_cfg: list[dict], seed: int = 42) -> dict[str, dict]:
    """Run V1 ``ColumnGenerator`` across multiple tables in declared order,
    accumulating a ``reference_data`` dict the same way ``DataGenerator._generate_table``
    does, but in-memory (no CSV writes). Returns ``{table: {col: values_list}}``.

    Used by TestReferenceParityV1 as the parity oracle for cross-table FK
    (mint-a-pool) generation."""
    import pandas as pd

    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=None)
    reference_data: dict[str, "pd.DataFrame"] = {}
    out: dict[str, dict] = {}
    for table in tables_cfg:
        name = table["name"]
        n = table["row_count"]
        df = pd.DataFrame()
        for col in table["generate_columns"]:
            series = cg.generate_column(n, col, name, reference_data)
            df[col["name"]] = series
        reference_data[name] = df
        out[name] = {c: df[c].tolist() for c in df.columns}
    return out


def _v2_run_multi(tables_cfg: list[dict], seed: int = 42) -> dict[str, dict]:
    """Run v2 ``generate_tables`` against a multi-table generate config; returns
    ``{table: {col: values_list}}`` for parity comparison vs ``_v1_run_multi``."""
    cfg = {
        "version": 1,
        "mode": "generate",
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": tables_cfg,
        "targets": {
            t["name"]: {"type": "file", "format": "csv", "path": "o.csv"}
            for t in tables_cfg
        },
    }
    cfg = PipelineConfig.model_validate(cfg).model_dump()
    result = generate_tables(cfg)
    return {
        name: {c: tbl.column(c).to_pylist() for c in tbl.column_names}
        for name, tbl in result.items()
    }


class TestReferenceParityV1:
    """Reading B: v2 ``reference`` is byte-identical to V1 ``_generate_reference_column``
    + the V1 ``generate_column`` null_probability post-process across multi-table
    generation under fixed seed (``derive_key=None``)."""

    def _customers_then_orders(self, distribution: str, child_n: int = 10, **child_extras) -> list[dict]:
        parent = {
            "name": "customers",
            "row_count": 5,
            "generate_columns": [
                {"name": "id", "type": "sequence", "start": 1, "step": 1}
            ],
        }
        child_col: dict = {
            "name": "customer_id",
            "type": "reference",
            "reference_table": "customers",
            "reference_column": "id",
            "distribution": distribution,
        }
        child_col.update(child_extras)
        child = {
            "name": "orders",
            "row_count": child_n,
            "generate_columns": [
                {"name": "order_id", "type": "sequence", "start": 100, "step": 1},
                child_col,
            ],
        }
        return [parent, child]

    def test_random_distribution(self):
        tables = self._customers_then_orders("random")
        v2 = _v2_run_multi(tables)
        v1 = _v1_run_multi(tables)
        assert v2 == v1
        # Orphan-freeness: every child customer_id is one of the parent's ids.
        parents = set(v2["customers"]["id"])
        assert all(c in parents for c in v2["orders"]["customer_id"])

    def test_sequential_distribution(self):
        tables = self._customers_then_orders("sequential")
        assert _v2_run_multi(tables) == _v1_run_multi(tables)

    def test_weighted_distribution(self):
        tables = self._customers_then_orders(
            "weighted", weights=[5, 1, 1, 1, 1]
        )
        assert _v2_run_multi(tables) == _v1_run_multi(tables)

    def test_min_per_parent(self):
        tables = self._customers_then_orders(
            "random", child_n=20, min_per_parent=2
        )
        assert _v2_run_multi(tables) == _v1_run_multi(tables)

    def test_max_per_parent(self):
        tables = self._customers_then_orders(
            "random", child_n=20, max_per_parent=5
        )
        assert _v2_run_multi(tables) == _v1_run_multi(tables)

    def test_empty_parent_pool(self):
        # 0-row parent -> child reference column is [None]*n.
        parent = {
            "name": "customers",
            "row_count": 0,
            "generate_columns": [
                {"name": "id", "type": "sequence", "start": 1, "step": 1}
            ],
        }
        child = {
            "name": "orders",
            "row_count": 5,
            "generate_columns": [
                {
                    "name": "customer_id",
                    "type": "reference",
                    "reference_table": "customers",
                    "reference_column": "id",
                    "distribution": "random",
                }
            ],
        }
        tables = [parent, child]
        v2 = _v2_run_multi(tables)
        assert v2 == _v1_run_multi(tables)
        assert v2["orders"]["customer_id"] == [None] * 5

    def test_null_probability_on_reference(self):
        tables = self._customers_then_orders(
            "random", child_n=20, null_probability=0.3
        )
        # Parity covers the per-row reference value AND the null/non-null positions.
        assert _v2_run_multi(tables) == _v1_run_multi(tables)

    def test_repeatability(self):
        # Same config + same seed across two runs -> byte-identical output.
        tables = self._customers_then_orders("random")
        assert _v2_run_multi(tables) == _v2_run_multi(tables)

    def test_many_to_many_junction(self):
        # m:n via a junction table with TWO reference columns.
        users = {
            "name": "users",
            "row_count": 3,
            "generate_columns": [
                {"name": "id", "type": "sequence", "start": 1, "step": 1}
            ],
        }
        groups = {
            "name": "groups",
            "row_count": 4,
            "generate_columns": [
                {"name": "id", "type": "sequence", "start": 100, "step": 1}
            ],
        }
        membership = {
            "name": "memberships",
            "row_count": 10,
            "generate_columns": [
                {
                    "name": "user_id",
                    "type": "reference",
                    "reference_table": "users",
                    "reference_column": "id",
                    "distribution": "random",
                },
                {
                    "name": "group_id",
                    "type": "reference",
                    "reference_table": "groups",
                    "reference_column": "id",
                    "distribution": "random",
                },
            ],
        }
        tables = [users, groups, membership]
        assert _v2_run_multi(tables) == _v1_run_multi(tables)


class TestReferenceConfigValidation:
    """v2-specific validation behaviors (not parity-tested): the contract enforces
    a reference graph that's resolvable + acyclic + properly declared at validation
    time. V1 was permissive (placeholder strings); the v2 fails fast at validate."""

    def _two_table_cfg(self, **child_extras) -> dict:
        child_col: dict = {
            "name": "customer_id",
            "type": "reference",
            "reference_table": "customers",
            "reference_column": "id",
            "distribution": "random",
        }
        child_col.update(child_extras)
        return {
            "version": 1,
            "mode": "generate",
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "customers",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "id", "type": "sequence", "start": 1, "step": 1}
                    ],
                },
                {
                    "name": "orders",
                    "row_count": 10,
                    "generate_columns": [child_col],
                },
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": "c.csv"},
                "orders": {"type": "file", "format": "csv", "path": "o.csv"},
            },
        }

    def test_reference_requires_reference_table(self):
        cfg = self._two_table_cfg()
        del cfg["tables"][1]["generate_columns"][0]["reference_table"]
        with pytest.raises(ValidationError, match="reference_table"):
            PipelineConfig.model_validate(cfg)

    def test_reference_requires_reference_column(self):
        cfg = self._two_table_cfg()
        del cfg["tables"][1]["generate_columns"][0]["reference_column"]
        with pytest.raises(ValidationError, match="reference_column"):
            PipelineConfig.model_validate(cfg)

    def test_unknown_reference_table_rejected(self):
        cfg = self._two_table_cfg(reference_table="does_not_exist")
        with pytest.raises(ValidationError, match="unknown table"):
            PipelineConfig.model_validate(cfg)

    def test_unknown_reference_column_rejected(self):
        cfg = self._two_table_cfg(reference_column="not_a_column")
        with pytest.raises(ValidationError, match="declares no such generate_column"):
            PipelineConfig.model_validate(cfg)

    def test_cycle_rejected(self):
        # customers.parent_id -> orders.id; orders.customer_id -> customers.id => cycle.
        cfg = self._two_table_cfg()
        cfg["tables"][0]["generate_columns"].append(
            {
                "name": "parent_id",
                "type": "reference",
                "reference_table": "orders",
                "reference_column": "customer_id",
                "distribution": "random",
            }
        )
        with pytest.raises(ValidationError, match="reference cycle"):
            PipelineConfig.model_validate(cfg)

    def test_topo_sort_handles_declared_order_reverse(self):
        # Declare child BEFORE parent. v2's topo-sort generates parent first;
        # the result should still be a valid orphan-free child set.
        cfg = self._two_table_cfg()
        cfg["tables"] = list(reversed(cfg["tables"]))  # orders first, customers second
        validated = PipelineConfig.model_validate(cfg).model_dump()
        result = generate_tables(validated)
        parents = set(result["customers"].column("id").to_pylist())
        children = result["orders"].column("customer_id").to_pylist()
        assert all(c in parents for c in children)
