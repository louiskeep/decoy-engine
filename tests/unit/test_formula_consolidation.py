"""F0 formula consolidation — pins behavior of the unified Python-expression
formula evaluator across mask + generate sides.

Pre-F0 there were three named formula_types (basic / template / composite)
that hid two independent choices:
  - syntax (expression vs template)
  - scope (this-row only vs cross-column)

F0 collapses to one knob: every formula is a Python expression. Cross-column
scope opens up by setting ``references: [col_a, col_b, ...]``. Template
syntax is just ``f"..."`` written by the user.

These tests pin:
  - Mask formula: single Python-expression evaluator works as expected;
    no ``formula_type`` field accepted.
  - Generate inline formula (no references): per-row deterministic seeding;
    ``random()``/``randint()`` reseed per row.
  - Generate referenced formula (post-pass): same per-row determinism as
    inline (the determinism FIX — pre-F0 composite did NOT reseed per row,
    so ``random()`` calls were silently non-deterministic across runs).
  - Validators no longer require/care about ``formula_type``.
"""

from __future__ import annotations

from logging import getLogger
from pathlib import Path

import pandas as pd
import yaml

from decoy_engine import validate_config
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.generators.generator import DataGenerator
from decoy_engine.transforms.formula import FormulaStrategy

_LOG = getLogger(__name__)


def _write_config(tmp_path: Path, config: dict, name: str = "config.yaml") -> str:
    """Write a generator config to YAML and return the path. DataGenerator
    only accepts a config_path, not a dict."""
    p = tmp_path / name
    p.write_text(yaml.dump(config), encoding="utf-8")
    return str(p)


# ── Mask formula: single Python-expression evaluator ────────────────────


class TestMaskFormulaSingleEvaluator:
    """Mask formula collapses basic + template into one Python-expression
    path. Users wanting template-style substitution write ``f"..."``."""

    def test_basic_expression(self):
        s = FormulaStrategy(logger=_LOG)
        out = s.apply(
            pd.Series(["hello", "world"]),
            {
                "column": "x",
                "type": "formula",
                "formula": "value.upper()",
            },
        )
        assert out.iloc[0] == "HELLO"
        assert out.iloc[1] == "WORLD"

    def test_fstring_template(self):
        # No more `formula_type: template` — users wrap the formula
        # themselves with `f"..."`.
        s = FormulaStrategy(logger=_LOG)
        out = s.apply(
            pd.Series(["alice", "bob"]),
            {
                "column": "name",
                "type": "formula",
                "formula": "f'USER-{value}'",
            },
        )
        assert out.iloc[0] == "USER-alice"
        assert out.iloc[1] == "USER-bob"

    def test_nulls_preserved(self):
        s = FormulaStrategy(logger=_LOG)
        out = s.apply(
            pd.Series(["a", None, "b"]),
            {
                "column": "x",
                "type": "formula",
                "formula": "value.upper()",
            },
        )
        assert out.iloc[0] == "A"
        assert pd.isna(out.iloc[1])
        assert out.iloc[2] == "B"

    def test_empty_formula_passes_through(self):
        s = FormulaStrategy(logger=_LOG)
        col = pd.Series(["a", "b"])
        out = s.apply(col, {"column": "x", "type": "formula", "formula": ""})
        pd.testing.assert_series_equal(out, col)

    def test_formula_type_field_is_ignored_now(self):
        # Accepts a stray `formula_type` for backwards-compat-ignore — F0
        # doesn't read it. No error, no special handling.
        s = FormulaStrategy(logger=_LOG)
        out = s.apply(
            pd.Series(["hi"]),
            {
                "column": "x",
                "type": "formula",
                "formula_type": "this-key-no-longer-matters",
                "formula": 'value + "!"',
            },
        )
        assert out.iloc[0] == "hi!"


# ── Generate inline formula: per-row deterministic seeding ──────────────


class TestGenerateInlineFormula:
    """Inline formula path (no `references`). Every row reseeds RNG +
    Faker via column_seed + row_index, so calls inside the formula stay
    stable across runs."""

    def _gen(self, formula: str, num_rows: int = 5, seed: int = 42):
        gen = ColumnGenerator(seed=seed, logger=_LOG)
        return gen._generate_formula_column(
            num_rows=num_rows,
            column_config={"name": "x", "type": "formula", "formula": formula},
            table_name="t",
            reference_data={},
        )

    def test_index_reachable(self):
        out = self._gen("f'USER-{i:06d}'")
        assert list(out) == [f"USER-{i:06d}" for i in range(5)]

    def test_random_reseeded_per_row_so_run_to_run_stable(self):
        # randint() inside the formula must be deterministic given the
        # same seed + column name + row index. Two separate ColumnGenerator
        # instances with the same seed must produce the same output.
        out1 = self._gen("randint(0, 1_000_000)")
        out2 = self._gen("randint(0, 1_000_000)")
        pd.testing.assert_series_equal(out1, out2)

    def test_different_rows_get_different_random(self):
        # Sanity: the per-row reseed actually produces different values
        # across rows (else seeding would be useless and every row would
        # share the same RNG snapshot).
        out = self._gen("randint(0, 1_000_000_000)", num_rows=20)
        assert len(set(out)) > 1  # at least two distinct values

    def test_different_seeds_yield_different_output(self):
        out1 = self._gen("randint(0, 1_000_000)", seed=42)
        out2 = self._gen("randint(0, 1_000_000)", seed=43)
        assert any(a != b for a, b in zip(out1, out2, strict=False))

    def test_no_formula_type_field_required(self):
        # Pre-F0 columns_py.formula dispatched on `formula_type`. Now the
        # field is gone; absence is the default and only path.
        gen = ColumnGenerator(seed=42, logger=_LOG)
        out = gen._generate_formula_column(
            num_rows=3,
            column_config={"name": "x", "type": "formula", "formula": "i * 2"},
            table_name="t",
            reference_data={},
        )
        assert list(out) == [0, 2, 4]

    def test_references_present_means_post_pass_placeholder(self):
        # When `references` is non-empty the inline path returns None
        # placeholders — the post-pass on DataGenerator fills them. We
        # test the post-pass behavior in TestGenerateReferencedFormula.
        gen = ColumnGenerator(seed=42, logger=_LOG)
        out = gen._generate_formula_column(
            num_rows=3,
            column_config={
                "name": "x",
                "type": "formula",
                "formula": "f'{a}_{b}'",
                "references": ["a", "b"],
            },
            table_name="t",
            reference_data={},
        )
        assert all(v is None for v in out)


# ── Generate referenced (post-pass) formula: determinism + cross-column ─


class TestGenerateReferencedFormula:
    """Post-pass evaluator (was `_evaluate_composite_formula`). The
    determinism FIX in F0: per-row reseeding via column_seed + row_index
    so `random()` / `randint()` stay stable across runs. Pre-F0 composite
    used whatever the global RNG state happened to be, which made any
    composite formula calling `randint()` silently non-deterministic."""

    def _make_config(self, output_dir: Path, csv_name: str = "people.csv") -> dict:
        # Minimal config exercising a referenced formula end-to-end: two
        # faker columns + one formula reading them. Run twice with the
        # same seed and compare CSVs to check determinism.
        return {
            "generator_settings": {"seed": 42, "output_directory": str(output_dir)},
            "tables": [
                {
                    "name": "people",
                    "rows": 5,
                    "output_path": str(output_dir / csv_name),
                    "columns": [
                        {"name": "first_name", "type": "faker", "faker_type": "first_name"},
                        {"name": "last_name", "type": "faker", "faker_type": "last_name"},
                        {
                            "name": "username",
                            "type": "formula",
                            "references": ["first_name", "last_name"],
                            "formula": "f'{first_name.lower()}.{last_name.lower()}'",
                        },
                    ],
                }
            ],
        }

    def test_referenced_formula_reads_sibling_columns(self, tmp_path):
        config = self._make_config(tmp_path)
        path = _write_config(tmp_path, config)
        DataGenerator(path, logger=_LOG).generate()
        df = pd.read_csv(tmp_path / "people.csv")
        for _, row in df.iterrows():
            expected = f"{row['first_name'].lower()}.{row['last_name'].lower()}"
            assert row["username"] == expected

    def test_referenced_formula_deterministic_across_runs(self, tmp_path):
        # The determinism FIX: pre-F0 composite formulas using random()
        # inside would silently produce different output across runs. F0
        # reseeds per row, so byte-identical CSVs are guaranteed.
        run1 = tmp_path / "run1"
        run1.mkdir()
        run2 = tmp_path / "run2"
        run2.mkdir()
        DataGenerator(
            _write_config(run1, self._make_config(run1), "config.yaml"),
            logger=_LOG,
        ).generate()
        DataGenerator(
            _write_config(run2, self._make_config(run2), "config.yaml"),
            logger=_LOG,
        ).generate()
        bytes1 = (run1 / "people.csv").read_bytes()
        bytes2 = (run2 / "people.csv").read_bytes()
        assert bytes1 == bytes2, "referenced formula output must be byte-stable across runs"

    def test_random_inside_referenced_formula_is_deterministic(self, tmp_path):
        # Regression guard for the pre-F0 silent bug: `randint()` calls
        # inside a referenced (post-pass) formula must be deterministic
        # per row + per key. Use a formula that ONLY depends on randint
        # so the determinism property is isolated from any other source.
        def make_config(out_dir: Path) -> dict:
            return {
                "generator_settings": {"seed": 42, "output_directory": str(out_dir)},
                "tables": [
                    {
                        "name": "t",
                        "rows": 10,
                        "output_path": str(out_dir / "t.csv"),
                        "columns": [
                            {"name": "a", "type": "faker", "faker_type": "first_name"},
                            {
                                "name": "noisy",
                                "type": "formula",
                                "references": ["a"],
                                "formula": "f'{a}-{randint(0, 1_000_000_000)}'",
                            },
                        ],
                    }
                ],
            }

        run1 = tmp_path / "run1"
        run1.mkdir()
        run2 = tmp_path / "run2"
        run2.mkdir()
        DataGenerator(_write_config(run1, make_config(run1)), logger=_LOG).generate()
        DataGenerator(_write_config(run2, make_config(run2)), logger=_LOG).generate()

        df1 = pd.read_csv(run1 / "t.csv")
        df2 = pd.read_csv(run2 / "t.csv")
        # `noisy` column must match between runs row-for-row — the randint
        # output is bound to (column, row) via the per-row reseed.
        assert df1["noisy"].tolist() == df2["noisy"].tolist()


# ── Validator behavior: formula_type ignored, references is a list ──────


class TestValidatorFormulaType:
    def test_mask_config_without_formula_type_passes(self):
        config = {
            "global_settings": {"seed": 42},
            "input": {
                "type": "csv",
                "path": "in.csv",
                "csv_options": {"delimiter": ",", "encoding": "utf-8"},
            },
            "output": {
                "type": "csv",
                "path": "out.csv",
                "csv_options": {"delimiter": ",", "encoding": "utf-8"},
            },
            "masking_rules": [
                {"column": "name", "type": "formula", "formula": "value.upper()"},
            ],
        }
        validate_config(config)

    def test_mask_config_with_stray_formula_type_passes(self):
        # F0 doesn't read formula_type — accepting old configs without
        # erroring keeps any in-flight YAML files from breaking on load.
        # The stray field has no effect.
        config = {
            "global_settings": {"seed": 42},
            "input": {
                "type": "csv",
                "path": "in.csv",
                "csv_options": {"delimiter": ",", "encoding": "utf-8"},
            },
            "output": {
                "type": "csv",
                "path": "out.csv",
                "csv_options": {"delimiter": ",", "encoding": "utf-8"},
            },
            "masking_rules": [
                {
                    "column": "name",
                    "type": "formula",
                    "formula_type": "composite",  # was rejected pre-F0
                    "formula": "value.upper()",
                },
            ],
        }
        validate_config(config)

    def test_generate_config_with_references_passes(self):
        config = {
            "generator_settings": {"seed": 42, "output_directory": "data/generated/"},
            "tables": [
                {
                    "name": "people",
                    "row_count": 10,
                    "columns": [
                        {"name": "a", "type": "faker", "faker_type": "first_name"},
                        {
                            "name": "b",
                            "type": "formula",
                            "references": ["a"],
                            "formula": "f'X-{a}'",
                        },
                    ],
                }
            ],
        }
        validate_config(config)

    # NOTE: A `references` type-check is wired into
    # `_validate_column_type_specific` (formula branch), but
    # `GeneratorConfigValidator.validate()` is shallow today and never
    # invokes the per-column-type validators. So an invalid
    # `references: "not-a-list"` won't be caught at config-validation
    # time — it'll fail later inside `_process_referenced_formulas`
    # when the post-pass tries to iterate it. Deepening the generate
    # validator is its own follow-up; out of F0 scope.
