"""DPS Scope B: compile-time DP generate contract + consume-only lock.

Supersedes the Option A test suite entirely (that mechanism is deleted;
`apply_dp_noise` no longer exists). Covers guide section 5/6/8's named
assertions: categorical DP compiles without `allow_real_categories`
(guide binding decision + step 5), an exact snapshot still requires the
consent gate, an unverified `dp`-shaped key in an artifact cannot forge
the exemption, joint/condition_on is rejected under DP, and the
release-ID budget ledger (guide section 6 row F5/F6) fails closed on
every malformed shape the guide enumerates.
"""

from __future__ import annotations

import builtins
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.generation.statistical import load_spec
from decoy_engine.plan import PlanCompileError, compile_plan
from decoy_engine.plan._checks_dp import check_dp_generate_contract, verify_dp_snapshots
from decoy_engine.plan._generation import read_and_pin_snapshots
from decoy_engine.profile import Profile
from decoy_engine.quality.dp import fit_dp_snapshot
from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    compute_distribution_snapshot,
)


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def _write(tmp_path, name: str, snap: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _dp_fit_mixed(tmp_path, *, n=400, epsilon=5.0, delta=1e-6) -> str:
    rng = np.random.default_rng(3)
    df = pd.DataFrame(
        {
            "age": rng.integers(0, 120, size=n).astype(float),
            "state": rng.choice(["CA", "NY", "TX"], size=n, p=[0.5, 0.3, 0.2]),
        }
    )
    snap = fit_dp_snapshot(
        df,
        categorical_columns=["state"],
        numeric_domains={"age": (0.0, 120.0)},
        epsilon=epsilon,
        delta=delta,
    )
    return _write(tmp_path, "dp.json", snap)


def _dp_fit_categorical_only(
    tmp_path,
    filename: str,
    *,
    categories: list[str],
    p: list[float],
    n: int = 800,
    epsilon: float = 8.0,
    delta: float = 1e-6,
    seed: int = 11,
) -> str:
    """A categorical-only DP fit (no numeric columns) over a caller-chosen
    label alphabet -- used by the F4 file-swap test to construct two
    artifacts whose retained labels can never collide, since the
    alphabets themselves are disjoint. `n`/`p`/`epsilon` are generous
    (a strongly dominant label, high epsilon) so the unseeded OpenDP
    threshold mechanism retains at least the majority label with
    overwhelming probability; this mirrors `_dp_fit_mixed`'s already-
    reliable shape used throughout this file, just skewed further for a
    file-swap test that must not be flaky.
    """
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({"state": rng.choice(categories, size=n, p=p)})
    snap = fit_dp_snapshot(
        df,
        categorical_columns=["state"],
        numeric_domains={},
        epsilon=epsilon,
        delta=delta,
    )
    return _write(tmp_path, filename, snap)


def _dp_cfg(*, table_columns: list[dict], epsilon: float = 5.0, delta: float = 1e-6) -> dict:
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": epsilon, "delta": delta}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": table_columns}],
    }


class TestCheckDpGenerateContract:
    def test_dp_unset_never_raises_even_with_anti_dp_knobs(self):
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "dx",
                            "type": "statistical",
                            "snapshot_file": "whatever.json",
                            "allow_real_categories": True,
                            "high_cardinality": True,
                        }
                    ],
                }
            ],
        }
        check_dp_generate_contract(cfg)  # no raise

    def test_dp_rejects_high_cardinality(self, tmp_path):
        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "state",
                    "type": "statistical",
                    "snapshot_file": path,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"

    def test_dp_configuration_rejects_allow_real_categories_true(self, tmp_path):
        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "state",
                    "type": "statistical",
                    "snapshot_file": path,
                    "allow_real_categories": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_allow_real_categories_unsupported"

    def test_dp_configuration_rejects_joint_or_condition_on(self, tmp_path):
        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "state",
                    "type": "statistical",
                    "snapshot_file": path,
                    "condition_on": "age",
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_joint_unsupported"

    def test_non_statistical_generate_column_ignored(self):
        cfg = _dp_cfg(table_columns=[{"name": "id", "type": "sequence"}])
        check_dp_generate_contract(cfg)  # no raise


class TestDpCategoricalNowSupported:
    """Scope B binding decision: categorical DP IS supported when the
    snapshot is a verified `dps-marginal/v2` release. This directly
    supersedes Option A's blanket `dp_categorical_not_yet_supported`."""

    def test_dp_categorical_snapshot_compiles_without_allow_real_categories(self, tmp_path):
        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}]
        )
        names = run_config_only_checks(cfg)
        assert "statistical_columns" in names  # no raise; compiled clean

    def test_dp_categorical_snapshot_compiles_end_to_end_through_compile_plan(self, tmp_path):
        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[
                {"name": "age", "type": "statistical", "snapshot_file": path},
                {"name": "state", "type": "statistical", "snapshot_file": path},
            ]
        )
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
        assert plan.generation is not None
        assert plan.generation.dp_verification is not None
        assert plan.generation.dp_verification.epsilon_total <= 5.0

    def test_exact_categorical_snapshot_still_requires_allow_real_categories(self, tmp_path):
        df = pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})
        snap = compute_distribution_snapshot(df)  # ordinary, non-DP fit
        path = _write(tmp_path, "exact.json", snap)
        cfg = {
            "global_settings": {"seed": 1},  # no dp declared
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "state", "type": "statistical", "snapshot_file": path}
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_real_categories_not_allowed"

    def test_dp_exemption_ignores_unverified_dp_key_in_artifact(self, tmp_path):
        """An attacker (or a stale/hand-edited artifact) cannot forge the
        exemption by hand-writing a `dp`-shaped key onto an otherwise
        exact snapshot: the compiler's OWN verification is what grants
        `dp_verified`, never a truthy key read from the artifact itself."""
        df = pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})
        snap = compute_distribution_snapshot(df)
        snap["dp"] = {"schema": "dps-marginal/v2", "epsilon_total": 1.0, "delta_total": 1e-6}
        path = _write(tmp_path, "forged.json", snap)
        cfg = {
            "global_settings": {"seed": 1},  # dp NOT declared globally
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "state", "type": "statistical", "snapshot_file": path}
                    ],
                }
            ],
        }
        # Without global_settings.dp declared, verify_dp_snapshots never
        # runs at all -- dp_verified is always False regardless of the
        # artifact's own claims -- so the consent gate still applies.
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_real_categories_not_allowed"

    def test_load_spec_never_reads_snapshot_dp_key_for_the_exemption(self, tmp_path):
        """Direct unit proof at the `load_spec` seam: `dp_verified` is a
        plain argument, not derived from `snapshot["dp"]`."""
        df = pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})
        snap = compute_distribution_snapshot(df)
        snap["dp"] = {"schema": "dps-marginal/v2", "epsilon_total": 1.0, "delta_total": 1e-6}
        spec_kwargs = {"name": "state", "type": "statistical", "snapshot_file": "x"}
        from decoy_engine.generation.statistical._spec import StatisticalSpecError

        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(spec_kwargs, snapshot=snap, dp_verified=False)
        assert exc.value.code == "statistical_real_categories_not_allowed"
        # dp_verified=True (the compiler's verdict) is what exempts it,
        # not the presence of snap["dp"].
        spec = load_spec(spec_kwargs, snapshot=snap, dp_verified=True)
        assert spec.kind == "categorical"


class TestFailClosedDpDeclaration:
    """Guide section 6 row F6: presence is key membership, not truthiness,
    for every value except a bare `None` -- deliberately narrowed from the
    guide's literal text (see `_dp_declared`'s docstring in
    `plan/_checks_dp.py`): `PipelineConfig.model_validate(cfg).model_dump()`,
    the documented production choke point, always materializes
    `global_settings["dp"] = None` for a config that never touched `dp`,
    so a pure membership test would reject every ordinary non-DP pipeline
    compiled through that choke point. A present-but-otherwise-malformed
    value ({}, a list, a scalar, an incomplete mapping) still fails
    closed -- there is no equivalent false-positive risk for those,
    since ordinary PipelineConfig validation cannot produce them."""

    def test_empty_dp_block_fails_closed_with_dp_budget_declaration_malformed(self):
        cfg = {"global_settings": {"seed": 1, "dp": {}}, "tables": []}
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, {})
        assert exc.value.code == "dp_budget_declaration_malformed"

    def test_non_mapping_dp_block_fails_closed(self):
        for bad in ([], "x", 1):
            cfg = {"global_settings": {"seed": 1, "dp": bad}, "tables": []}
            with pytest.raises(PlanCompileError) as exc:
                verify_dp_snapshots(cfg, {})
            assert exc.value.code == "dp_budget_declaration_malformed"

    def test_dp_unset_passes(self):
        cfg = {"global_settings": {"seed": 1}, "tables": []}
        verified, receipt = verify_dp_snapshots(cfg, {})
        assert verified == frozenset()
        assert receipt is None

    def test_dp_key_present_as_none_passes_like_unset(self):
        """The `PipelineConfig.model_dump()` case: `dp` present, value
        `None`, because the field was never set. Must compile clean, not
        raise `dp_budget_declaration_malformed` (see class docstring)."""
        cfg = {"global_settings": {"seed": 1, "dp": None}, "tables": []}
        verified, receipt = verify_dp_snapshots(cfg, {})
        assert verified == frozenset()
        assert receipt is None

    def test_pipeline_config_dump_of_an_unset_dp_field_compiles_without_dp_error(self):
        """End-to-end reproduction of the real regression this guards:
        a config that never touches `global_settings.dp`, validated and
        dumped through the documented production choke point
        (`PipelineConfig.model_validate(cfg).model_dump()`), must compile
        through `run_config_only_checks` without a DP-declaration error."""
        from decoy_engine import run_config_only_checks
        from decoy_engine.config import PipelineConfig

        raw = {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-in.csv"}},
            "tables": [
                {
                    "name": "people",
                    "columns": [{"name": "email", "strategy": "faker", "provider": "person_email"}],
                }
            ],
            "targets": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-out.csv"}},
        }
        dumped = PipelineConfig.model_validate(raw).model_dump()
        assert dumped["global_settings"]["dp"] is None  # confirms the reproduction still applies
        run_config_only_checks(dumped)  # must not raise dp_budget_declaration_malformed


def _numeric_dp_artifact(*, epsilon_total, delta_total=0.0, release_id="r1", distinct_marker=0):
    return {
        "schema_version": DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
        "row_count": 5,
        "columns": {
            "age": {
                "dtype": "float64",
                "kind": "numeric",
                "null_count": 0,
                "non_null_count": 5,
                "distinct_count": 5,
                "stats": {"bin_edges": [0.0, 120.0], "bin_counts": [5]},
            }
        },
        "joints": [],
        "dp": {
            "schema": "dps-marginal/v2",
            "release_id": release_id,
            "scope": "single-column-marginals",
            "adjacency": "add-remove-one-row",
            "epsilon_total": epsilon_total,
            "delta_total": delta_total,
            "accountant": "dp_accounting PLD composition over OpenDP privacy maps",
            "opendp_version": _running_opendp_version(),
            "dp_accounting_version": _running_dp_accounting_version(),
            "query_count": 2,  # 1 row_count + 1 numeric column + 2*0 categorical
            "numeric_bins": 1,
            "categorical_columns": [],
            "numeric_domains": {"age": [0.0, 120.0]},
            "_marker": distinct_marker,
        },
    }


def _running_opendp_version() -> str:
    import importlib.metadata

    return importlib.metadata.version("opendp")


def _running_dp_accounting_version() -> str:
    import importlib.metadata

    return importlib.metadata.version("dp-accounting")


def _cfg_with_artifact(
    tmp_path, artifact, *, name="age", declared_epsilon=10.0, declared_delta=1e-6
):
    path = _write(tmp_path, f"{name}_{id(artifact)}.json", artifact)
    return {
        "global_settings": {
            "seed": 1,
            "dp": {"epsilon": declared_epsilon, "delta": declared_delta},
        },
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [
                    {
                        "name": name,
                        "type": "statistical",
                        "snapshot_file": path,
                        "source_column": "age",
                    }
                ],
            }
        ],
    }, path


class TestReleaseIdLedger:
    """Guide section 6 row F5: distinct release IDs always compose; the
    same ID referenced repeatedly is charged once; a reused ID with
    different bytes is rejected as a conflicting artifact."""

    def test_distinct_release_ids_with_identical_released_values_compose_budget(self, tmp_path):
        a = _numeric_dp_artifact(epsilon_total=0.6, release_id="rA")
        b = _numeric_dp_artifact(epsilon_total=0.6, release_id="rB", distinct_marker=1)
        cfg_a, path_a = _cfg_with_artifact(tmp_path, a, name="age_a")
        _, path_b = _cfg_with_artifact(tmp_path, b, name="age_b")
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age_a",
                            "type": "statistical",
                            "snapshot_file": path_a,
                            "source_column": "age",
                        },
                        {
                            "name": "age_b",
                            "type": "statistical",
                            "snapshot_file": path_b,
                            "source_column": "age",
                        },
                    ],
                }
            ],
        }
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_budget_exceeded"

    def test_same_release_id_referenced_twice_is_charged_once(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.6, release_id="rSAME")
        path = _write(tmp_path, "same.json", artifact)
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": f"age_{i}",
                            "type": "statistical",
                            "snapshot_file": path,
                            "source_column": "age",
                        }
                        for i in range(5)
                    ],
                }
            ],
        }
        pinned = read_and_pin_snapshots(cfg)
        verified, receipt = verify_dp_snapshots(cfg, pinned)
        assert receipt is not None
        assert receipt.epsilon_total == pytest.approx(0.6)
        assert receipt.release_ids == ("rSAME",)

    def test_same_release_id_with_different_digest_is_rejected(self, tmp_path):
        a = _numeric_dp_artifact(epsilon_total=0.5, release_id="rDUP")
        b = _numeric_dp_artifact(epsilon_total=0.9, release_id="rDUP")  # same ID, different bytes
        path_a = _write(tmp_path, "dupA.json", a)
        path_b = _write(tmp_path, "dupB.json", b)
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 10.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age_a",
                            "type": "statistical",
                            "snapshot_file": path_a,
                            "source_column": "age",
                        },
                        {
                            "name": "age_b",
                            "type": "statistical",
                            "snapshot_file": path_b,
                            "source_column": "age",
                        },
                    ],
                }
            ],
        }
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_release_id_conflict"

    def test_dp_snapshot_without_release_id_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        del artifact["dp"]["release_id"]
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_missing_release_id"

    def test_composed_release_budget_above_declared_ceiling_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=5.0, release_id="rBIG")
        cfg, _ = _cfg_with_artifact(tmp_path, artifact, declared_epsilon=1e-6)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_budget_exceeded"

    def test_dp_artifact_query_count_inconsistent_with_declared_columns_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        artifact["dp"]["query_count"] = 99
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_query_count_mismatch"

    def test_dp_artifact_from_a_different_library_version_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        artifact["dp"]["opendp_version"] = "0.0.0-not-installed"
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_library_version_mismatch"

    def test_dp_snapshot_budget_malformed_nan_delta(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=1.0, delta_total=float("nan"))
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_budget_malformed"

    def test_not_dp_fit_snapshot_rejected(self, tmp_path):
        df = pd.DataFrame({"age": [1, 2, 3]})
        snap = compute_distribution_snapshot(df)
        cfg, _ = _cfg_with_artifact(tmp_path, snap)
        pinned = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_not_dp_fit"


class TestConsumeOnlyContract:
    def test_generation_consumes_only_the_pinned_snapshot(self, tmp_path):
        """Sampling from a DP artifact needs no raw source frame -- and,
        under Scope B, no re-opened path either (guide section 4.7/4.8).
        """
        path = _dp_fit_mixed(tmp_path, n=300)
        cfg = _dp_cfg(
            table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}]
        )
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
        from decoy_engine.generation.synthesize import generate_tables

        out = generate_tables(plan)
        values = out["t"]["state"].to_pylist()
        assert len(values) == 5
        assert set(values) <= {"CA", "NY", "TX"}

    def test_generate_tables_uses_pinned_snapshot_after_file_swap(self, tmp_path, monkeypatch):
        """Guide section 4.8/F4 and defect closure matrix row F4. The
        previous regression here (`test_generation_consumes_only_the_
        pinned_snapshot` above) never overwrites the snapshot file after
        compiling, so it passes whether or not pinning actually works --
        it proves nothing about TOCTOU safety. This test runs the exact
        attack guide section 7.4 describes: compile against one DP
        artifact, then swap the SAME path's bytes for a second,
        independently-fit DP artifact whose retained categorical labels
        live in a completely disjoint alphabet, then generate.

        Two things must both hold for this to be a real proof rather than
        a restatement of the old test: (1) the generated values must be
        provably attributable to the PINNED (pre-swap) artifact, which a
        disjoint label alphabet gives us for free -- any value from the
        swapped alphabet could only have come from a runtime re-read; and
        (2) `open()` must never be called on the pinned path again after
        compile_plan returns, checked structurally, not inferred from the
        output alone.
        """
        pinned_path = _dp_fit_categorical_only(
            tmp_path,
            "pinned.json",
            categories=["CA", "NY", "TX"],
            p=[0.9, 0.07, 0.03],
            epsilon=8.0,
            delta=1e-6,
        )
        swapped_snapshot_path = _dp_fit_categorical_only(
            tmp_path,
            "swapped.json",
            categories=["ZQX", "WKR", "VPL"],
            p=[0.9, 0.07, 0.03],
            epsilon=8.0,
            delta=1e-6,
            seed=97,
        )
        swapped_bytes = Path(swapped_snapshot_path).read_bytes()
        # Sanity: the two artifacts are genuinely different bytes and
        # genuinely disjoint label spaces, or the assertion below would be
        # vacuous.
        assert swapped_bytes != Path(pinned_path).read_bytes()

        cfg = _dp_cfg(
            table_columns=[{"name": "state", "type": "statistical", "snapshot_file": pinned_path}],
            epsilon=8.0,
            delta=1e-6,
        )
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")

        # The TOCTOU swap: overwrite the SAME path compile_plan just read,
        # with the disjoint-alphabet artifact's exact bytes.
        with open(pinned_path, "wb") as fh:
            fh.write(swapped_bytes)
        assert Path(pinned_path).read_bytes() == swapped_bytes  # swap landed

        # Structural guard: nothing downstream of compile_plan may open
        # this path again. A generation path that reopens it to reread the
        # (now-swapped) snapshot must fail this test even if it happened
        # to sample a value that also looked plausible.
        real_open = builtins.open

        def _guarded_open(file, *args, **kwargs):
            try:
                target = os.fspath(file)
            except TypeError:
                target = None
            if target is not None and os.path.abspath(str(target)) == os.path.abspath(pinned_path):
                raise AssertionError(
                    f"generate_tables reopened pinned snapshot path {pinned_path!r} "
                    "after compile_plan -- the Plan must carry embedded bytes, not a "
                    "runtime path read (guide section 4.7/4.8, defect F4)."
                )
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", _guarded_open)

        from decoy_engine.generation.synthesize import generate_tables

        out = generate_tables(plan)
        values = set(out["t"]["state"].to_pylist())

        # Every generated value must be explainable by the PINNED artifact
        # (or empty, if -- vanishingly unlikely at this skew/epsilon -- no
        # label survived thresholding); none may come from the swapped
        # artifact's disjoint alphabet. If pinning were broken and
        # generation re-read the swapped file, this assertion is exactly
        # what would catch it: the swapped alphabet shares no strings with
        # the pinned one.
        assert values <= {"CA", "NY", "TX"}
        assert not values & {"ZQX", "WKR", "VPL"}


class TestGenerateTablesRejectsRawConfig:
    """Guide section 4.8/F3, defect closure matrix row F3: the public
    `generate_tables` entry point accepts only a compiled `Plan`. A raw
    configuration mapping -- the exact prior bypass shape (guide section
    7.4's entrypoint-bypass PoC) -- must be rejected with a typed error
    before any output is produced, not silently accepted and generated
    from directly.
    """

    def test_generate_tables_rejects_raw_config_and_requires_compiled_plan(self, tmp_path):
        path = _dp_fit_mixed(tmp_path, n=300)
        raw_config = _dp_cfg(
            table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}]
        )
        from decoy_engine.generation.synthesize import generate_tables

        with pytest.raises(TypeError, match="compiled decoy_engine.plan.Plan"):
            generate_tables(raw_config)

    def test_generate_tables_rejects_plan_without_generation_payload(self):
        # A Plan compiled from a config with no generate_columns at all has
        # no GenerationPlan to generate from.
        cfg = {"global_settings": {"seed": 1}, "tables": [{"name": "t", "row_count": 3}]}
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
        assert plan.generation is None
        from decoy_engine.generation.synthesize import generate_tables

        with pytest.raises(TypeError, match="no generation payload"):
            generate_tables(plan)
