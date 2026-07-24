"""DPS-CODEC phase 5: end-to-end regression guard for the `flag` carrier.

Phase 5 wired the `flag` (bool-domain) carrier through the live fit
(`quality/dp.py`) and the compile-time verifier (`plan/_checks_dp.py`), but no
committed test drove `fit_dp_snapshot` with a flag column all the way to a
verified `dps-marginal/v3` artifact. This is that guard: a real OpenDP fit over
a bool column produces a v3 artifact whose flag column serializes as the
canonical `"true"`/`"false"` tokens against a `bool` dtype, whose DP block
counts the flag as a categorical PAIR, which `verify_dp_snapshots` accepts, and
whose per-column carrier fails closed when mutated.
"""

from __future__ import annotations

import copy
import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.plan import PlanCompileError
from decoy_engine.plan._checks_dp import verify_dp_snapshots
from decoy_engine.plan._generation import read_and_pin_snapshots
from decoy_engine.quality.dp import fit_dp_snapshot


def _fit_flag_plus_numeric(n: int = 3000, *, epsilon: float = 10.0, delta: float = 1e-5) -> dict:
    """A real OpenDP fit over one bool (`flag`) column and one numeric column.

    Strong skew + generous budget so the bool-domain threshold mechanism
    reliably retains the dominant label -- the artifact must actually carry a
    serialized flag token, not an empty (all-suppressed) top_values list.
    """
    rng = np.random.default_rng(7)
    df = pd.DataFrame(
        {
            "is_active": rng.choice([True, False], size=n, p=[0.95, 0.05]),
            "age": rng.integers(0, 120, size=n).astype(float),
        }
    )
    return fit_dp_snapshot(
        df,
        {
            "is_active": {"kind": "categorical", "carrier": "flag"},
            "age": {"kind": "numeric", "carrier": "number", "bounds": (0.0, 120.0)},
        },
        epsilon=epsilon,
        delta=delta,
    )


def _cfg_for(path: str, *, epsilon: float = 20.0, delta: float = 1e-4) -> dict:
    """A `dp`-declared config whose two statistical generate columns both point
    at the fitted flag+numeric artifact. The declared ceiling is generous so
    verification turns on carrier/identity, not the budget bound."""
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": epsilon, "delta": delta}},
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [
                    {"name": "is_active", "type": "statistical", "snapshot_file": path},
                    {"name": "age", "type": "statistical", "snapshot_file": path},
                ],
            }
        ],
    }


def _write(tmp_path, name: str, snap: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


class TestFlagCarrierEndToEnd:
    def test_flag_fit_produces_a_verified_v3_artifact(self, tmp_path):
        snap = _fit_flag_plus_numeric()

        # The flag column block: bool dtype, flag carrier, categorical kind.
        flag_col = snap["columns"]["is_active"]
        assert flag_col["dtype"] == "bool"
        assert flag_col["carrier"] == "flag"
        assert flag_col["kind"] == "categorical"

        # Categories serialize as the canonical "true"/"false" tokens -- never
        # Python str(bool) "True"/"False" and never "0"/"1" (guide section 3.4).
        tokens = {entry["value"] for entry in flag_col["stats"]["top_values"]}
        assert tokens  # the dominant label survived thresholding
        assert tokens <= {"true", "false"}

        # The v3 dp block records the flag column's carrier and counts it as a
        # categorical PAIR: query_count == 1 + numeric + 2*categorical.
        dp = snap["dp"]
        assert dp["column_schema"]["is_active"]["carrier"] == "flag"
        schema = dp["column_schema"]
        numeric_count = sum(1 for s in schema.values() if s["carrier"] == "number")
        categorical_count = sum(1 for s in schema.values() if s["carrier"] in ("text", "flag"))
        assert categorical_count == 1  # the flag column is the sole categorical
        assert dp["query_count"] == 1 + numeric_count + 2 * categorical_count

        # verify_dp_snapshots ACCEPTS the honest artifact.
        path = _write(tmp_path, "flag.json", snap)
        cfg = _cfg_for(path)
        pinned, _ = read_and_pin_snapshots(cfg)
        verified, receipt = verify_dp_snapshots(cfg, pinned)
        assert receipt is not None
        assert ("t", "is_active") in verified
        assert ("t", "age") in verified

    def test_flag_column_with_a_mutated_carrier_fails_closed(self, tmp_path):
        snap = _fit_flag_plus_numeric()
        mutated = copy.deepcopy(snap)
        # A `flag` categorical column relabelled `number` is not an allowed
        # kind x carrier pair; verification must refuse it.
        mutated["columns"]["is_active"]["carrier"] = "number"
        path = _write(tmp_path, "mutated.json", mutated)
        cfg = _cfg_for(path)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_carrier_invalid"
