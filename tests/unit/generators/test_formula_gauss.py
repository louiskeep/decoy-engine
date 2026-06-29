"""Unit tests for the gauss() formula function in the generation engine (HIGH-3).

gauss(mu, sigma) was added to the formula scope in Phase 3b (_formula.py:
_formula_scope) to support numeric generate columns with a normal distribution.
These tests put it under the regression gate (pytest -m "not benchmark") so
the shipped src change is covered independently of the CI-excluded testflight
job that exercises the full pipeline.

Two contracts:
  1. Determinism: same seed produces byte-identical output across calls, even
     when module-global random state is polluted between calls (M21 guard).
  2. Distribution fidelity: over enough rows the output mean and std fall
     within a 10% relative band of the declared gauss parameters.
"""

from __future__ import annotations

import random

from decoy_engine.config import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gauss_config(
    mu: float,
    sigma: float,
    n_rows: int = 500,
    seed: int = 42,
) -> dict:
    """Return a validated generate config with one formula column: gauss(mu, sigma)."""
    raw = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": [
            {
                "name": "t",
                "row_count": n_rows,
                "generate_columns": [
                    {
                        "name": "amount",
                        "type": "formula",
                        "formula": f"gauss({mu}, {sigma})",
                    }
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestGaussDeterminism:
    """gauss() rows are byte-identical under the same seed, regardless of module
    global random state between calls."""

    def test_same_seed_byte_identical(self) -> None:
        """Two generate_tables() calls with the same seed and formula produce
        identical output lists."""
        cfg = _gauss_config(50.0, 15.0, n_rows=200, seed=7)
        out_a = generate_tables(cfg)["t"].column("amount").to_pylist()
        out_b = generate_tables(cfg)["t"].column("amount").to_pylist()
        assert out_a == out_b, (
            "gauss formula column: two generate_tables() calls with the same "
            "seed produced different output. Determinism broken."
        )

    def test_module_global_pollution_does_not_change_output(self) -> None:
        """Seeding module-global random between calls must not change the output.

        M21 guard: the per-row RNG is isolated from module-global state. If
        _formula_scope binds gauss to the per-row rng instance (not to the module
        random), polluting module-global state between runs has no effect.
        """
        cfg = _gauss_config(100.0, 20.0, n_rows=100, seed=99)
        out_a = generate_tables(cfg)["t"].column("amount").to_pylist()
        # Pollute module-global random between calls.
        random.seed(12345)
        for _ in range(200):
            random.gauss(0, 1)
        out_b = generate_tables(cfg)["t"].column("amount").to_pylist()
        assert out_a == out_b, (
            "gauss formula column: module-global random pollution changed output. "
            "gauss() in _formula_scope must be bound to the per-row rng, not "
            "module-level random."
        )

    def test_different_seeds_produce_different_output(self) -> None:
        """Different seeds must produce different output (sanity check)."""
        out_a = (
            generate_tables(_gauss_config(50.0, 10.0, n_rows=50, seed=1))["t"]
            .column("amount")
            .to_pylist()
        )
        out_b = (
            generate_tables(_gauss_config(50.0, 10.0, n_rows=50, seed=2))["t"]
            .column("amount")
            .to_pylist()
        )
        assert out_a != out_b, (
            "gauss formula column: different seeds produced identical output. "
            "The per-row seeding is likely broken."
        )


# ---------------------------------------------------------------------------
# Distribution fidelity
# ---------------------------------------------------------------------------


class TestGaussDistribution:
    """gauss(mu, sigma) output mean and std fall within a 10% relative band
    of the declared parameters at n=1000 rows."""

    def test_mean_within_band(self) -> None:
        """Output mean is within 10% of declared mu=50.0 at n=1000."""
        mu, sigma = 50.0, 15.0
        tol = 0.10
        cfg = _gauss_config(mu, sigma, n_rows=1000, seed=42)
        vals = generate_tables(cfg)["t"].column("amount").to_pylist()
        non_null = [v for v in vals if v is not None]
        out_mean = sum(non_null) / len(non_null)
        band = tol * max(abs(mu), 1.0)
        assert abs(out_mean - mu) <= band, (
            f"gauss({mu}, {sigma}) mean={out_mean:.4f} outside "
            f"[{mu - band:.4f}, {mu + band:.4f}] (tol={tol})."
        )

    def test_std_within_band(self) -> None:
        """Output std is within 10% of declared sigma=15.0 at n=1000."""
        mu, sigma = 50.0, 15.0
        tol = 0.10
        cfg = _gauss_config(mu, sigma, n_rows=1000, seed=42)
        vals = generate_tables(cfg)["t"].column("amount").to_pylist()
        non_null = [v for v in vals if v is not None]
        n = len(non_null)
        mean = sum(non_null) / n
        out_std = (sum((x - mean) ** 2 for x in non_null) / (n - 1)) ** 0.5
        band = tol * max(sigma, 1.0)
        assert abs(out_std - sigma) <= band, (
            f"gauss({mu}, {sigma}) std={out_std:.4f} outside "
            f"[{sigma - band:.4f}, {sigma + band:.4f}] (tol={tol})."
        )

    def test_no_null_rows(self) -> None:
        """gauss() should not produce None for any row (always a float)."""
        cfg = _gauss_config(0.0, 1.0, n_rows=200, seed=5)
        vals = generate_tables(cfg)["t"].column("amount").to_pylist()
        null_count = sum(1 for v in vals if v is None)
        assert null_count == 0, (
            f"gauss(0.0, 1.0): {null_count} None value(s) in {len(vals)} rows. "
            "gauss() must not raise per-row (which produces None placeholders)."
        )

    def test_rows_are_floats(self) -> None:
        """Every output value must be a float (not int, str, etc.)."""
        cfg = _gauss_config(10.0, 2.0, n_rows=50, seed=8)
        vals = generate_tables(cfg)["t"].column("amount").to_pylist()
        non_float = [
            (i, type(v).__name__, v) for i, v in enumerate(vals) if not isinstance(v, float)
        ]
        assert not non_float, f"gauss(10.0, 2.0): non-float values at rows {non_float[:5]}."
