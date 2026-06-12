"""Generation-time fidelity warn-gate (deferred follow-up 5, 2026-06-12).

After a generate table builds, statistical columns are scored against
their source snapshot (compute_distribution_snapshot + compute_fidelity)
and a warning is logged below global_settings.fidelity_warn_threshold.
Warn-only: output bytes never change, low scores never raise.

The doctored-artifact fixture exploits the split between what the
sampler reads (bin_counts / bin_edges) and what fidelity compares
(quantiles): shifting the artifact's quantiles far from its bins makes
the generated column score near zero without touching the sampler path.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation._fidelity_gate import (
    fidelity_warn_threshold,
    score_generated_fidelity,
)
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.quality.snapshot import compute_distribution_snapshot

_GATE_LOGGER = "decoy_engine.generation._fidelity_gate"


def _source_df() -> pd.DataFrame:
    rng = np.random.default_rng(11)
    return pd.DataFrame({"amount": rng.normal(100.0, 25.0, size=2_000).round(2)})


def _write_snapshot(tmp_path, df: pd.DataFrame, *, name: str = "snapshot.json") -> str:
    snap = compute_distribution_snapshot(df)
    path = tmp_path / name
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _write_doctored_snapshot(tmp_path, df: pd.DataFrame) -> str:
    """Quantiles shifted out of range; sampler-facing bins untouched."""
    snap = compute_distribution_snapshot(df)
    stats = snap["columns"]["amount"]["stats"]
    stats["quantiles"] = {k: float(v) + 10_000.0 for k, v in stats["quantiles"].items()}
    path = tmp_path / "doctored.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _config(snapshot_file: str, *, threshold: float | None = None, row_count: int = 500) -> dict:
    gs: dict = {"seed": 42}
    if threshold is not None:
        gs["fidelity_warn_threshold"] = threshold
    return {
        "version": 1,
        "global_settings": gs,
        "tables": [
            {
                "name": "synthetic",
                "row_count": row_count,
                "generate_columns": [
                    {"name": "amount", "type": "statistical", "snapshot_file": snapshot_file}
                ],
            }
        ],
    }


class TestWarnGate:
    def test_high_fidelity_logs_nothing(self, tmp_path, caplog):
        snap = _write_snapshot(tmp_path, _source_df())
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            out = generate_tables(_config(snap, row_count=2_000))
        assert out["synthetic"].num_rows == 2_000
        assert not [r for r in caplog.records if "generation_fidelity" in r.message]

    def test_doctored_artifact_warns(self, tmp_path, caplog):
        snap = _write_doctored_snapshot(tmp_path, _source_df())
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            generate_tables(_config(snap))
        warns = [r.message for r in caplog.records if "generation_fidelity" in r.message]
        assert len(warns) == 1
        msg = warns[0]
        assert "table=synthetic" in msg
        assert f"snapshot={snap}" in msg
        assert "threshold=0.8" in msg
        assert "amount:" in msg

    def test_warning_does_not_change_output_bytes(self, tmp_path, caplog):
        """The gate is read-only: warned and unwarned configs that share a
        seed produce byte-identical tables, and two warned runs produce
        identical warning strings."""
        snap = _write_doctored_snapshot(tmp_path, _source_df())
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            first = generate_tables(_config(snap))
            second = generate_tables(_config(snap))
        assert first["synthetic"].equals(second["synthetic"])
        warns = [r.message for r in caplog.records if "generation_fidelity" in r.message]
        assert len(warns) == 2
        assert warns[0] == warns[1]
        # Same seed, threshold silenced: same bytes with no warning.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            silenced = generate_tables(_config(snap, threshold=0.0))
        assert silenced["synthetic"].equals(first["synthetic"])
        assert not [r for r in caplog.records if "generation_fidelity" in r.message]

    def test_threshold_zero_silences_doctored_case(self, tmp_path, caplog):
        snap = _write_doctored_snapshot(tmp_path, _source_df())
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            generate_tables(_config(snap, threshold=0.0))
        assert not [r for r in caplog.records if "generation_fidelity" in r.message]

    def test_strict_threshold_flips_high_fidelity_case(self, tmp_path):
        """Direct gate call: a near-1.0 threshold warns even on a faithful
        sample (seeded, so the score is a fixed value below 1.0)."""
        df = _source_df()
        snap = _write_snapshot(tmp_path, df, name="strict.json")
        cols = [{"name": "amount", "type": "statistical", "snapshot_file": snap}]
        out = generate_tables(_config(snap, row_count=2_000))
        data = {"amount": out["synthetic"].column("amount").to_pylist()}
        assert score_generated_fidelity(cols, data, table_name="synthetic", threshold=0.999)
        assert not score_generated_fidelity(cols, data, table_name="synthetic", threshold=0.5)


class TestNonStatisticalTables:
    def test_faker_only_table_scores_nothing(self):
        cols = [{"name": "word", "type": "faker", "faker_type": "word"}]
        assert (
            score_generated_fidelity(cols, {"word": ["a", "b"]}, table_name="t", threshold=0.8)
            == []
        )

    def test_faker_only_config_logs_nothing(self, caplog):
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [{"name": "w", "type": "faker", "faker_type": "word"}],
                }
            ],
        }
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            out = generate_tables(cfg)
        assert out["t"].num_rows == 5
        assert not [r for r in caplog.records if "generation_fidelity" in r.message]


class TestThresholdConfig:
    def test_default_read_from_unvalidated_dict(self):
        assert fidelity_warn_threshold({}) == 0.8
        assert fidelity_warn_threshold({"global_settings": {"seed": 1}}) == 0.8
        assert fidelity_warn_threshold({"global_settings": {"fidelity_warn_threshold": 0.5}}) == 0.5

    def test_global_settings_model_bounds(self):
        from pydantic import ValidationError

        from decoy_engine.config._global_settings import GlobalSettings

        ok = GlobalSettings(seed=1, fidelity_warn_threshold=0.9)
        assert ok.fidelity_warn_threshold == 0.9
        assert GlobalSettings(seed=1).fidelity_warn_threshold == 0.8
        with pytest.raises(ValidationError):
            GlobalSettings(seed=1, fidelity_warn_threshold=1.5)
        with pytest.raises(ValidationError):
            GlobalSettings(seed=1, not_a_field=True)
