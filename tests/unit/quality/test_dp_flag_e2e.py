"""DPS-CODEC phases 5-6: end-to-end regression guard for the `flag` carrier.

Phase 5 wired the `flag` (bool-domain) carrier through the live fit
(`quality/dp.py`) and the compile-time verifier (`plan/_checks_dp.py`), but no
committed test drove `fit_dp_snapshot` with a flag column all the way to a
verified `dps-marginal/v3` artifact. This is that guard: a real OpenDP fit over
a bool column produces a v3 artifact whose flag column serializes as the
canonical `"true"`/`"false"` tokens against a `bool` dtype, whose DP block
counts the flag as a categorical PAIR, which `verify_dp_snapshots` accepts, and
whose per-column carrier fails closed when mutated.

Phase 6 wired the flag decoder into the sampler (`generation/statistical/
_sample.py`) and lifted the phase-5 generate-side refusal, so this file also
carries the real fit all the way through `compile_plan` -> `generate_tables`
and asserts the output is genuine Python `bool`, not the `"true"`/`"false"`
strings a pre-phase-6 sampler would have emitted.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan._checks_dp import verify_dp_snapshots
from decoy_engine.plan._generation import read_and_pin_snapshots
from decoy_engine.profile import Profile
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

    def test_column_schema_is_snapshot_once_at_entry(self):
        """The fit must freeze the caller's schema on entry so a mutable mapping
        cannot drift the routing decision (which mechanism a column takes) away
        from the carrier recorded into the artifact, which the verifier trusts.
        The tightest guard is that the caller's own mapping is iterated exactly
        once: every later read hits the frozen copy, not the original."""
        rng = np.random.default_rng(3)
        df = pd.DataFrame(
            {
                "is_active": rng.choice([True, False], size=2000, p=[0.9, 0.1]),
                "age": rng.integers(0, 120, size=2000).astype(float),
            }
        )

        calls = {"n": 0}

        class CountingSchema(dict):
            def items(self):
                calls["n"] += 1
                return super().items()

        schema = CountingSchema(
            {
                "is_active": {"kind": "categorical", "carrier": "flag"},
                "age": {"kind": "numeric", "carrier": "number", "bounds": (0.0, 120.0)},
            }
        )
        snap = fit_dp_snapshot(df, schema, epsilon=10.0, delta=1e-5)

        # Iterated exactly once (the entry snapshot); the recorded carrier is the
        # frozen value, consistent between the dp block and the column block.
        assert calls["n"] == 1
        assert snap["dp"]["column_schema"]["is_active"]["carrier"] == "flag"
        assert snap["columns"]["is_active"]["carrier"] == "flag"

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

    def test_columns_block_carrier_must_agree_with_dp_column_schema(self, tmp_path):
        """HIGH-2 (Codex phase-5): the `columns` block carrier (which the M-2
        flag-sampler stop and generation read) must equal the dp block's
        column_schema entry. Relabelling the columns block `text` while the dp
        block still declares `flag` would slip the unwired flag release onto the
        text sampler; verification must catch the disagreement."""
        snap = _fit_flag_plus_numeric()
        tampered = copy.deepcopy(snap)
        # Leave dp.column_schema.is_active.carrier == "flag" (valid categorical),
        # but relabel ONLY the columns-block carrier to "text": each field is
        # categorical-family-valid on its own, so only cross-field agreement
        # catches it.
        assert tampered["dp"]["column_schema"]["is_active"]["carrier"] == "flag"
        tampered["columns"]["is_active"]["carrier"] = "text"
        path = _write(tmp_path, "split_carrier.json", tampered)
        cfg = _cfg_for(path)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_column_block_schema_mismatch"

    def test_column_schema_numeric_bounds_contradiction_fails_closed(self, tmp_path):
        """MEDIUM-5 (Codex phase-5): the v3 column_schema must agree with the
        legacy numeric_domains in KIND and BOUNDS, not only carrier and set
        membership. A column_schema that records different bounds while keeping a
        valid numeric_domains (which the numeric path trusts) would otherwise
        verify."""
        snap = _fit_flag_plus_numeric()
        tampered = copy.deepcopy(snap)
        # numeric_domains.age stays the honest domain; only column_schema.age
        # bounds are contradicted.
        tampered["dp"]["column_schema"]["age"]["bounds"] = [0.0, 999.0]
        path = _write(tmp_path, "bad_bounds.json", tampered)
        cfg = _cfg_for(path)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_column_schema_mismatch"

    def test_freeze_column_schema_freezes_nested_bounds(self):
        """MEDIUM-3 (Codex phase-5): the entry snapshot must freeze the nested
        `bounds` too, not only the carrier/kind entries, or a caller mutating the
        bounds list after entry could desync the recorded domain from the
        schedule edges."""
        from decoy_engine.quality.dp_fit_schema import freeze_column_schema

        bounds = [0.0, 100.0]
        schema = {"x": {"kind": "numeric", "carrier": "number", "bounds": bounds}}
        frozen = freeze_column_schema(schema)
        assert frozen["x"]["bounds"] == (0.0, 100.0)
        assert isinstance(frozen["x"]["bounds"], tuple)
        assert frozen["x"] is not schema["x"]
        bounds.append(999.0)  # mutate the caller's original list after the freeze
        assert frozen["x"]["bounds"] == (0.0, 100.0)  # frozen copy is unaffected


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


class TestFlagCarrierGeneratesRealBoolEndToEnd:
    """Phase 6: the flag decoder is wired into the sampler, so the M-2
    generate-side refusal lifts and a real OpenDP fit over a flag column
    generates real Python `bool` values through compile_plan ->
    generate_tables, both when the release retains both categories and when
    it retains only one."""

    def test_both_categories_generate_real_bool_values(self, tmp_path):
        snap = _fit_flag_plus_numeric()
        tokens = {e["value"] for e in snap["columns"]["is_active"]["stats"]["top_values"]}
        assert tokens == {"true", "false"}  # both categories survived thresholding
        path = _write(tmp_path, "flag_gen_both.json", snap)
        plan = compile_plan(_cfg_for(path), _profile(), decoy_engine_version="test")
        out = generate_tables(plan)
        values = out["t"]["is_active"].to_pylist()
        assert len(values) == 5
        assert all(isinstance(v, bool) for v in values)

    def test_one_category_generates_the_single_surviving_bool_value(self, tmp_path):
        # Every row is True, so the thresholded release retains only "true".
        rng = np.random.default_rng(9)
        n = 2000
        df = pd.DataFrame(
            {
                "is_active": np.ones(n, dtype=bool),
                "age": rng.integers(0, 120, size=n).astype(float),
            }
        )
        snap = fit_dp_snapshot(
            df,
            {
                "is_active": {"kind": "categorical", "carrier": "flag"},
                "age": {"kind": "numeric", "carrier": "number", "bounds": (0.0, 120.0)},
            },
            epsilon=10.0,
            delta=1e-5,
        )
        tokens = {e["value"] for e in snap["columns"]["is_active"]["stats"]["top_values"]}
        assert tokens == {"true"}
        path = _write(tmp_path, "flag_gen_one.json", snap)
        plan = compile_plan(_cfg_for(path), _profile(), decoy_engine_version="test")
        out = generate_tables(plan)
        values = out["t"]["is_active"].to_pylist()
        assert len(values) == 5
        assert all(v is True for v in values)
