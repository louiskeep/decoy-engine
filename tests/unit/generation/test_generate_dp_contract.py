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
import uuid
from collections import UserDict
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
        """BLOCKER 2 (adversarial finding): `verify_dp_snapshots`'s verdict
        is a PURE FUNCTION of the artifact's OWN `dp` key -- it checks
        internal consistency (library versions, query_count recomputation,
        kind eligibility, cheap numeric shape evidence), never that a real
        OpenDP fit actually produced the numbers. The reviewer demonstrated
        this concretely: take an ordinary EXACT `compute_distribution_
        snapshot`, attach a fabricated but internally-consistent `dp`
        block (correct library versions, correct `query_count`, a fresh
        `release_id`, `epsilon_total: 0.01`), and it compiled clean and
        generated real source values into synthetic output.

        The PREVIOUS version of this test left `global_settings.dp`
        UNDECLARED, so `verify_dp_snapshots` returned early
        (`_checks_dp.py:241`) and never examined the forged block at all
        -- it proved nothing about forgery resistance, only that the
        ordinary non-DP consent gate still applies when DP isn't even in
        play. This version declares `global_settings.dp` (so verification
        actually RUNS against the forgery) and reproduces the exact
        forged-block shape the reviewer used, on a NUMERIC column. The
        BLOCKER 2 item 3 shape-evidence check (an exact snapshot's real
        `mean`/`quantiles` can never coincidentally look like a DP
        release's always-null/always-empty shape) is what now catches
        this realistic case: compilation is rejected instead of silently
        granting the exemption and letting the real `mean`-bearing exact
        snapshot's synthesis proceed. `docs/what-we-cannot-prove.md`
        states plainly that this check does not stop a forger who
        replicates the DP shape from scratch -- only this realistic,
        demonstrated case."""
        df = pd.DataFrame({"age": [12.0, 45.0, 67.0, 89.0, 30.0]})
        snap = compute_distribution_snapshot(df)  # ordinary, EXACT fit
        assert snap["columns"]["age"]["stats"]["mean"] is not None  # a REAL exact statistic
        snap["dp"] = {
            "schema": "dps-marginal/v2",
            "release_id": uuid.uuid4().hex,
            "scope": "single-column-marginals",
            "adjacency": "add-remove-one-row",
            "epsilon_total": 0.01,
            "delta_total": 1e-6,
            "accountant": "dp_accounting PLD composition over OpenDP privacy maps",
            "opendp_version": _running_opendp_version(),
            "dp_accounting_version": _running_dp_accounting_version(),
            "query_count": 2,  # 1 row_count + 1 numeric column + 2*0 categorical: recomputes clean
            "numeric_bins": 10,
            "categorical_columns": [],
            "numeric_domains": {"age": [0.0, 120.0]},
        }
        path = _write(tmp_path, "forged_numeric.json", snap)
        cfg = _dp_cfg(table_columns=[{"name": "age", "type": "statistical", "snapshot_file": path}])
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_snapshot_numeric_shape_mismatch"

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
    full stop -- a present `dp` key fails closed on ANY malformed value,
    including a bare `None` (C-B3, Codex round-3 blocker: `dp: null` is a
    malformed declaration, not a synonym for unset). The ambiguity that
    used to force a `None` carve-out here is fixed upstream instead:
    `config.PipelineConfig.model_dump` (the documented production choke
    point) now omits the `dp` key entirely when `GlobalSettings.
    model_fields_set` shows it was never assigned, and leaves it present
    (including a bare `None`) when the operator explicitly wrote `dp:
    null`. See `_dp_declared`'s docstring in `plan/_checks_dp.py` and
    `PipelineConfig.model_dump`'s docstring in `config/_pipeline.py`."""

    def test_empty_dp_block_fails_closed_with_dp_budget_declaration_malformed(self):
        cfg = {"global_settings": {"seed": 1, "dp": {}}, "tables": []}
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, {})
        assert exc.value.code == "dp_budget_declaration_malformed"

    def test_non_mapping_dp_block_fails_closed(self):
        for bad in ([], "x", 1, None):
            cfg = {"global_settings": {"seed": 1, "dp": bad}, "tables": []}
            with pytest.raises(PlanCompileError) as exc:
                verify_dp_snapshots(cfg, {})
            assert exc.value.code == "dp_budget_declaration_malformed"

    def test_dp_unset_passes(self):
        cfg = {"global_settings": {"seed": 1}, "tables": []}
        verified, receipt = verify_dp_snapshots(cfg, {})
        assert verified == frozenset()
        assert receipt is None

    def test_dp_key_present_as_none_fails_closed(self):
        """C-B3 (Codex round-3 blocker): `dp` present with value `None` --
        an operator writing `dp: null` explicitly -- must fail closed with
        `dp_budget_declaration_malformed`, not compile clean like unset.
        Codex executed exactly this: an exact categorical snapshot under
        an explicitly present `dp: null` bypassed provenance, budget,
        categorical-consent, and receipt gates entirely."""
        cfg = {"global_settings": {"seed": 1, "dp": None}, "tables": []}
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, {})
        assert exc.value.code == "dp_budget_declaration_malformed"

    def test_pipeline_config_dump_of_an_unset_dp_field_omits_the_key(self):
        """End-to-end reproduction of the real regression this guards: a
        config that never touches `global_settings.dp`, validated and
        dumped through the documented production choke point
        (`PipelineConfig.model_validate(cfg).model_dump()`), must compile
        through `run_config_only_checks` without a DP-declaration error --
        and the dumped dict must not even carry a `dp` key, so an
        explicit `dp: null` elsewhere can never be confused with this
        case again."""
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
        assert "dp" not in dumped["global_settings"]  # unset -> key omitted entirely, not None
        run_config_only_checks(dumped)  # must not raise dp_budget_declaration_malformed

    @pytest.mark.parametrize(
        "serialize",
        [
            lambda m: m.model_dump(),
            lambda m: m.model_dump(mode="json"),
            lambda m: json.loads(m.model_dump_json()),
        ],
        ids=["model_dump", "model_dump_json_mode", "model_dump_json"],
    )
    def test_every_serialization_path_agrees_on_whether_dp_was_declared(self, serialize):
        """D-M3 (dennis round 4): the unset/explicit-null distinction was
        first implemented as a `model_dump` override on `PipelineConfig`.
        `model_dump_json` goes through pydantic-core and never consulted
        it, so that path still emitted `dp: None` for a never-assigned
        field. `_dp_declared` is a key-membership test, so any caller
        routing a config through `model_dump_json` got a hard
        `dp_budget_declaration_malformed` on EVERY ordinary non-DP
        pipeline. Fail-closed rather than a leak, but that path was
        broken outright.

        The fix moved the omission to a serializer on `GlobalSettings`,
        which every path runs. Parametrizing over all three is the point:
        the single-path version passed while two paths disagreed.

        The explicit-null half of this test moved to
        `test_pipeline_config_refuses_an_explicit_dp_null_at_validation`:
        that config no longer validates at all, so there is no dump of
        one left to compare. What remains here is the serializer's own
        job, omitting a never-assigned `dp`, plus the positive case that
        a real declaration reaches `_dp_declared` on every path."""
        from decoy_engine.config import PipelineConfig
        from decoy_engine.plan._checks_dp import _dp_declared

        base = {
            "version": 1,
            "sources": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-in.csv"}},
            "tables": [
                {
                    "name": "people",
                    "columns": [{"name": "email", "strategy": "faker", "provider": "person_email"}],
                }
            ],
            "targets": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-out.csv"}},
        }
        unset = PipelineConfig.model_validate({**base, "global_settings": {"seed": 1}})
        declared = PipelineConfig.model_validate(
            {**base, "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}}}
        )
        assert _dp_declared(serialize(unset)) is False
        assert _dp_declared(serialize(declared)) is True

    def test_pipeline_config_refuses_an_explicit_dp_null_at_validation(self):
        """The other half of the C-B3 fix, moved earlier.

        This used to assert that an explicit `dp: null` survived into the
        dumped dict so `run_config_only_checks` could reject it. Codex
        round 5 showed that carrying the distinction through
        serialization is lossy: pydantic strips a `None` value under
        `exclude_none=True` or `exclude_defaults=True` BEFORE the wrap
        serializer runs, so the key was gone and every DP gate was
        silently skipped. The declaration is now refused at validation,
        so no dump of an explicitly-null config exists to be misread.
        Raw-dict callers that never build this model still get
        `dp_budget_declaration_malformed` from `verify_dp_snapshots`,
        which `test_explicit_dp_null_fails_closed` covers."""
        import pydantic

        from decoy_engine.config import PipelineConfig

        raw = {
            "version": 1,
            "global_settings": {"seed": 1, "dp": None},
            "sources": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-in.csv"}},
            "tables": [
                {
                    "name": "people",
                    "columns": [{"name": "email", "strategy": "faker", "provider": "person_email"}],
                }
            ],
            "targets": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-out.csv"}},
        }
        with pytest.raises(pydantic.ValidationError, match="present but null"):
            PipelineConfig.model_validate(raw)

    @pytest.mark.parametrize(
        "wrap",
        [dict, UserDict],
        ids=["dict", "user_dict"],
    )
    def test_explicit_dp_null_is_refused_through_any_mapping_type(self, wrap):
        """C-B-2 (Codex round 6): the before-validator tested
        `isinstance(data, dict)`, but pydantic accepts ANY mapping. A
        `UserDict` carrying an explicit `dp: None` therefore validated,
        and `exclude_none` / `exclude_defaults` then erased the key so
        `_dp_declared` returned False -- the same fail-open the validator
        exists to close, reached through a different container type. A
        built-in dict cannot catch this, which is why it is parametrized
        over the mapping type."""
        import pydantic

        from decoy_engine.config import GlobalSettings

        with pytest.raises(pydantic.ValidationError, match="present but null"):
            GlobalSettings.model_validate(wrap({"seed": 1, "dp": None}))

    def test_dp_budget_is_the_one_that_was_validated_not_a_later_read(self):
        """C-H-1 (Codex round 7): the validator returned the CALLER'S
        object rather than what it had just checked, and pydantic then
        read that object a SECOND time. Validating one value and building
        the model from another is a TOCTOU, and on this field the value
        is the privacy budget.

        Measured, with the pre-fix `return data`: a mapping yielding
        `epsilon=0.1, delta=1e-9` on the first read and
        `epsilon=1000.0, delta=0.5` on the second produced a model
        carrying 1000.0/0.5 while the validator had approved 0.1/1e-9.
        The pipeline still calls itself DP; its budget is meaningless.

        Codex reported the defect as a LOST declaration (`dp is None`,
        silently non-DP). That specific outcome does not reproduce here
        and the distinction is worth recording: pydantic's second pass
        walks model fields through `__getitem__`, so the only way to
        report the key absent is to raise `KeyError`, which surfaces as a
        `ValidationError` -- fail-closed, not fail-open. A mapping that
        merely drifts its `keys()` or `__iter__` does not lose the block
        at all. The reachable defect is value substitution, which is why
        this test asserts on the BUDGET rather than on presence.

        `dict` and `UserDict` cannot falsify any of it: both are stable,
        so every read agrees. Drift is the mechanism, so the fixture must
        drift, and it must drift on `__getitem__` -- the first version of
        this test drifted on `keys()`, which pydantic never calls twice,
        and the mutant survived it."""
        from collections.abc import Mapping

        from decoy_engine.config import GlobalSettings
        from decoy_engine.plan._checks_dp import _dp_declared

        class _DriftingMapping(Mapping):
            """Yields a tight budget on the first read, a loose one after.

            Drift is keyed on `__getitem__` because that is the access
            pydantic re-reads with; a fixture drifting on `keys()` or
            `__iter__` is indistinguishable from a stable mapping and
            lets the defect through."""

            TIGHT = {"epsilon": 0.1, "delta": 1e-9}
            LOOSE = {"epsilon": 1000.0, "delta": 0.5}

            def __init__(self):
                self._payload = {"seed": 1, "dp": dict(self.TIGHT)}
                self.dp_reads = 0

            def __getitem__(self, key):
                if key == "dp":
                    self.dp_reads += 1
                    return dict(self.TIGHT if self.dp_reads == 1 else self.LOOSE)
                return self._payload[key]

            def __iter__(self):
                return iter(self._payload)

            def __len__(self):
                return len(self._payload)

        drifting = _DriftingMapping()
        settings = GlobalSettings.model_validate(drifting)

        assert drifting.dp_reads >= 1  # the fixture really was read
        assert settings.dp is not None
        # The budget that was VALIDATED, not whatever a later read returned.
        assert settings.dp.epsilon == 0.1, "privacy budget substituted after validation"
        assert settings.dp.delta == 1e-9
        assert "dp" in settings.model_fields_set
        assert _dp_declared({"global_settings": settings.model_dump()}) is True

    @pytest.mark.parametrize(
        "dump_kwargs",
        [{}, {"exclude_none": True}, {"exclude_defaults": True}, {"exclude_unset": True}],
        ids=["plain", "exclude_none", "exclude_defaults", "exclude_unset"],
    )
    def test_dp_declaration_survives_every_exclusion_option(self, dump_kwargs):
        """Codex round 5: the parametrized test above covered three
        output METHODS but no exclusion OPTIONS, so it passed while
        `exclude_none=True` and `exclude_defaults=True` both erased the
        declaration. A real `dp` block must reach `_dp_declared` under
        every exclusion mode, and an unset one must never look declared
        under any of them."""
        from decoy_engine.config import PipelineConfig
        from decoy_engine.plan._checks_dp import _dp_declared

        base = {
            "version": 1,
            "sources": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-in.csv"}},
            "tables": [
                {
                    "name": "people",
                    "columns": [{"name": "email", "strategy": "faker", "provider": "person_email"}],
                }
            ],
            "targets": {"people": {"type": "file", "format": "csv", "path": "/tmp/dps-out.csv"}},
        }
        declared = PipelineConfig.model_validate(
            {**base, "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}}}
        )
        unset = PipelineConfig.model_validate({**base, "global_settings": {"seed": 1}})
        assert _dp_declared(declared.model_dump(**dump_kwargs)) is True
        assert _dp_declared(unset.model_dump(**dump_kwargs)) is False


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
                "stats": {
                    "bin_edges": [0.0, 120.0],
                    "bin_counts": [5],
                    "min": 0.0,
                    "max": 120.0,
                    "mean": None,
                    "std": None,
                    "quantiles": {},
                },
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
        pinned, _ = read_and_pin_snapshots(cfg)
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
        pinned, _ = read_and_pin_snapshots(cfg)
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
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_release_id_conflict"

    def test_dp_snapshot_without_release_id_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        del artifact["dp"]["release_id"]
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_missing_release_id"

    def test_composed_release_budget_above_declared_ceiling_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=5.0, release_id="rBIG")
        cfg, _ = _cfg_with_artifact(tmp_path, artifact, declared_epsilon=1e-6)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_budget_exceeded"

    def test_dp_artifact_query_count_inconsistent_with_declared_columns_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        artifact["dp"]["query_count"] = 99
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_query_count_mismatch"

    def test_dp_artifact_from_a_different_library_version_is_rejected(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=0.5)
        artifact["dp"]["opendp_version"] = "0.0.0-not-installed"
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_library_version_mismatch"

    def test_dp_snapshot_budget_malformed_nan_delta(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=1.0, delta_total=float("nan"))
        cfg, _ = _cfg_with_artifact(tmp_path, artifact)
        pinned, _ = read_and_pin_snapshots(cfg)
        with pytest.raises(PlanCompileError) as exc:
            verify_dp_snapshots(cfg, pinned)
        assert exc.value.code == "dp_snapshot_budget_malformed"

    def test_not_dp_fit_snapshot_rejected(self, tmp_path):
        df = pd.DataFrame({"age": [1, 2, 3]})
        snap = compute_distribution_snapshot(df)
        cfg, _ = _cfg_with_artifact(tmp_path, snap)
        pinned, _ = read_and_pin_snapshots(cfg)
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


class TestDpDeclaredWithNoStatisticalColumns:
    """D-M7 (dennis must-fix): a `dp`-declared pipeline with ZERO `type:
    statistical` generate columns used to compile clean (`verify_dp_
    snapshots` returns `dp_verification=None` -- nothing to verify) but
    then `generate_tables` raised `TypeError` UNCONDITIONALLY whenever
    `_dp_declared(config)` was true, regardless of whether the config
    referenced any DP-fit column at all. That is a config-compiles/
    generate-always-raises trap with no way out for an operator who
    declares `global_settings.dp` alongside ordinary non-statistical
    generate columns (faker, sequence, ...), which is not a contradiction:
    the DP contract only concerns statistical generate columns."""

    def test_dp_declared_plan_with_a_statistical_column_still_requires_the_receipt(self, tmp_path):
        """D-H3 (dennis round 4): the D-M7 relaxation above shipped a
        test for what it newly PERMITS and none for what must still
        hold, so stubbing `_config_declares_statistical_column` to
        `False` -- which deletes the receipt requirement from the public
        generation boundary outright -- survived every test in this
        file. This is the other half: a `dp`-declared Plan that DOES
        reference a DP-fit column must refuse to generate without a
        reproduced receipt."""
        import dataclasses

        path = _dp_fit_mixed(tmp_path)
        cfg = _dp_cfg(
            table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}]
        )
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
        assert plan.generation is not None
        assert plan.generation.dp_verification is not None  # the honest compile

        stripped = dataclasses.replace(
            plan,
            generation=dataclasses.replace(plan.generation, dp_verification=None),
        )

        from decoy_engine.generation.synthesize import generate_tables

        with pytest.raises(TypeError, match="no reproduced DpVerification receipt"):
            generate_tables(stripped)

    def test_dp_declared_pipeline_with_only_non_statistical_columns_can_generate(self):
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                }
            ],
        }
        plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
        assert plan.generation is not None
        assert plan.generation.dp_verification is None  # nothing to verify -- no raise for that

        from decoy_engine.generation.synthesize import generate_tables

        out = generate_tables(plan)  # must not raise TypeError
        assert out["t"].column("id").to_pylist() == ["1", "2", "3", "4", "5"]
