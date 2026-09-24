"""A1: wiring the post-execution scan suite into `run_pipeline`.

These cells cover the opt-in surface, the finalize seam, and the routing
interaction for the default-OFF `post_validation` runtime flag. They do NOT
re-test scan logic (the eight scans + the merge site are covered by
tests/unit/validation/ and tests/privacy/); they prove the WIRING: a real leak
reaches `failed_checks` through `run_pipeline`, the flag off is inert, an
opted-in job is never sent to a bounded route where the scans cannot run, and
the config + enforce signal reach the runner / platform consumer.

The fail-before proof is `test_post_validation_on_injected_leak_populates_
failed_checks`: the same leaky config, off (leak uncaught) then on (caught).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionResult, run_pipeline
from decoy_engine.execution._pipeline_routing import _sequential_eligible, decide_execution_route
from decoy_engine.relationships import RelationshipGraph

_ENGINE_VERSION = "a1-post-validation-test"


def _validated(raw: dict[str, Any]) -> dict[str, Any]:
    """The run_pipeline contract: caller pre-validates, engine consumes the dump."""
    return PipelineConfig.model_validate(raw).model_dump()


def _single_mask_config(
    tmp_path: Path,
    *,
    column: str,
    strategy: str,
    provider_config: dict[str, Any],
    values: list[str],
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A validated single non-FK mask table + its resident source.

    No relationships -> routes full_frame; the unified slice declines whenever
    `post_validation` is set, so the finalize seam always runs.
    """
    src = pa.table({column: pa.array(values, type=pa.string())})
    src_path = tmp_path / f"{column}.parquet"
    pq.write_table(src, src_path)
    raw = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"t": {"type": "file", "path": str(src_path), "format": "parquet"}},
        "targets": {
            "t": {"type": "file", "path": str(tmp_path / "t.out.parquet"), "format": "parquet"}
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": column,
                        "strategy": strategy,
                        "provider": "person_email",
                        "namespace": "ns",
                        "provider_config": provider_config,
                    }
                ],
            }
        ],
    }
    return _validated(raw), {"t": src}


def _leaky_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A truncate column that leaks: "AAAAA"[:3] == "AAA", a source value.

    truncate is a substitution strategy, so a source value reappearing in the
    output is genuine leakage -> the leakage scan hard-fails. The leak is real
    on any engine (no wiring needed to produce it); only detection is wired.
    """
    return _single_mask_config(
        tmp_path,
        column="code",
        strategy="truncate",
        provider_config={"length": 3},
        values=["AAAAA", "AAA", "BBBBB"],
    )


def _clean_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A truncate column whose distinct prefixes match no source value."""
    return _single_mask_config(
        tmp_path,
        column="code",
        strategy="truncate",
        provider_config={"length": 3},
        values=["AAAxx", "BBByy", "CCCzz"],
    )


def _hash_config(tmp_path: Path, values: list[str]) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    return _single_mask_config(
        tmp_path, column="email", strategy="hash", provider_config={}, values=values
    )


def _dup_pk_fk_config(tmp_path: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """An FK job whose declared-PK parent column truncates to a single value.

    "p{i}"[:1] == "p" for every row, so every masked PK collides -> the
    pk_uniqueness scan hard-fails. The declared PK comes from the relationship's
    `parent.columns`. This job is sequential-eligible pure-mask FK, so it also
    exercises the routing decline (post_validation forces full_frame).
    """
    n = 5
    parent = pa.table({"id": pa.array([f"p{i}" for i in range(n)], type=pa.string())})
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
        }
    )
    pp = tmp_path / "parent.parquet"
    cp = tmp_path / "child.parquet"
    pq.write_table(parent, pp)
    pq.write_table(child, cp)
    trunc = {"strategy": "truncate", "provider": "person_email", "provider_config": {"length": 1}}
    raw = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            "parent": {"type": "file", "path": str(pp), "format": "parquet"},
            "child": {"type": "file", "path": str(cp), "format": "parquet"},
        },
        "targets": {
            "parent": {"type": "file", "path": str(tmp_path / "po.parquet"), "format": "parquet"},
            "child": {"type": "file", "path": str(tmp_path / "co.parquet"), "format": "parquet"},
        },
        "tables": [
            {"name": "parent", "columns": [{"name": "id", **trunc}]},
            {"name": "child", "columns": [{"name": "parent_id", **trunc}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    return _validated(raw), {"parent": parent, "child": child}


_POST_VALIDATION_KEYS = frozenset({"quality_summary", "failed_checks", "post_validation_enforce"})


# --------------------------------------------------------------------------
# Flag off: byte-identical, inert
# --------------------------------------------------------------------------


class TestFlagOffIsInert:
    def test_post_validation_off_is_byte_identical(self, tmp_path: Path) -> None:
        cfg, src = _leaky_config(tmp_path)
        off = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION)
        on = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        # Off adds none of the post-validation keys (the regression guard: a
        # future unconditional run would trip this).
        assert not (_POST_VALIDATION_KEYS & set(off.quality_metrics))
        # Validation never mutates: the masked output is byte-identical on vs off.
        assert off.outputs["t"].equals(on.outputs["t"])


# --------------------------------------------------------------------------
# Fail-before proof + clean job
# --------------------------------------------------------------------------


class TestInjectedLeakFailBefore:
    def test_post_validation_on_injected_leak_populates_failed_checks(self, tmp_path: Path) -> None:
        cfg, src = _leaky_config(tmp_path)

        # BEFORE (flag off, the main behavior): the leak is real in the output
        # AND no failed_checks are produced -- the leak is not caught.
        off = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION)
        assert "AAA" in off.outputs["t"].column("code").to_pylist()  # the leak is present
        assert "failed_checks" not in off.quality_metrics

        # AFTER (flag on): the leakage scan catches it.
        on = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        assert on.quality_metrics["quality_summary"]["failed_checks"] == ("leakage",)
        assert [c["code"] for c in on.quality_metrics["failed_checks"]] == ["leakage"]

    def test_post_validation_on_clean_job_succeeds_with_summary(self, tmp_path: Path) -> None:
        cfg, src = _clean_config(tmp_path)
        r = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        assert "quality_summary" in r.quality_metrics
        assert r.quality_metrics["quality_summary"]["failed_checks"] == ()
        assert r.quality_metrics["failed_checks"] == []
        assert r.quality_metrics["post_validation_enforce"] is False

    def test_post_validation_on_duplicate_pk_populates_failed_checks(self, tmp_path: Path) -> None:
        cfg, src = _dup_pk_fk_config(tmp_path)
        r = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        assert r.quality_metrics["quality_summary"]["failed_checks"] == ("pk_uniqueness",)
        assert [c["code"] for c in r.quality_metrics["failed_checks"]] == ["pk_uniqueness"]


# --------------------------------------------------------------------------
# Warn-only default vs enforce signal
# --------------------------------------------------------------------------


class TestWarnOnlyAndEnforce:
    def test_injected_leak_warn_only_does_not_fail_job(self, tmp_path: Path) -> None:
        cfg, src = _leaky_config(tmp_path)
        # The engine never fails the job on a hard-fail scan: run_pipeline
        # returns normally with the finding recorded and enforce off.
        r = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        assert isinstance(r, ExecutionResult)
        assert [c["code"] for c in r.quality_metrics["failed_checks"]] == ["leakage"]
        assert r.quality_metrics["post_validation_enforce"] is False

    def test_injected_leak_enforce_emits_job_failure_signal(self, tmp_path: Path) -> None:
        # The plan's `test_injected_leak_enforce_fails_job`: the node-run failure
        # is the platform companion change (decoy-platform), which branches on
        # the enforce signal the engine emits. The engine itself still returns.
        cfg, src = _leaky_config(tmp_path)
        r = run_pipeline(
            cfg,
            sources=src,
            engine_version=_ENGINE_VERSION,
            post_validation=True,
            post_validation_enforce=True,
        )
        assert r.quality_metrics["post_validation_enforce"] is True
        assert r.quality_metrics["failed_checks"]  # non-empty -> platform fails the node run


# --------------------------------------------------------------------------
# Routing interaction: never silently out-of-core
# --------------------------------------------------------------------------


class _FakeProfile:
    def __init__(self, relationships: tuple[Any, ...]) -> None:
        self.relationships = relationships


class TestRoutingDeclinesBoundedRoutes:
    def test_sequential_eligible_declines_on_post_validation(self) -> None:
        eligible, reason = _sequential_eligible(
            _FakeProfile((object(),)),
            has_generate_table=False,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            post_validation=True,
        )
        assert (eligible, reason) == (False, "post_validation_requested")

    def test_decide_route_flips_out_of_core_to_full_frame(self) -> None:
        # A large, OOC-eligible pure-mask FK job: out_of_core without the flag,
        # full_frame with it -- the job is NEVER sent to the bounded route where
        # the scans cannot run. Row-count mode (byte estimate off) isolates the
        # decline predicate. 5M < the 7.5M full-frame reject default, so the
        # declined job runs full_frame rather than fail-closed rejecting.
        common: dict[str, Any] = dict(
            has_generate_table=False,
            has_mask_table=True,
            validators=[],
            fidelity_report=False,
            vault_writer=None,
            execution_mode="auto",
            graph=RelationshipGraph(edges=(), ordering=()),
            out_of_core_compatible=True,
            largest_table_rows=5_000_000,
            out_of_core_threshold_rows=5_000_000,
            full_frame_reject_rows=7_500_000,
            use_byte_estimate_routing=False,
            use_probe_routing=False,
        )
        profile = _FakeProfile((object(),))
        route_off, _ = decide_execution_route(profile, post_validation=False, **common)
        route_on, reason_on = decide_execution_route(profile, post_validation=True, **common)
        assert route_off == "out_of_core"
        assert route_on == "full_frame"
        assert reason_on == "post_validation_requested"

    def test_forced_sequential_with_post_validation_fails_closed(self, tmp_path: Path) -> None:
        # An operator cannot force a job onto the sequential route (where the
        # scans cannot run) while asking for post_validation: fail closed rather
        # than silently skip the suite. Mirrors fidelity_report's forced-route
        # contract.
        from decoy_engine.errors import ConfigError

        cfg, src = _dup_pk_fk_config(tmp_path)
        with pytest.raises(ConfigError, match="post_validation_requested"):
            run_pipeline(
                cfg,
                sources=src,
                engine_version=_ENGINE_VERSION,
                execution_mode="sequential",
                post_validation=True,
            )

    def test_run_pipeline_fk_job_runs_full_frame_and_reaches_seam(self, tmp_path: Path) -> None:
        # End-to-end: an FK job under post_validation runs full_frame (never a
        # bounded route) and the finalize seam produces the summary.
        cfg, src = _dup_pk_fk_config(tmp_path)
        on = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        assert on.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert "quality_summary" in on.quality_metrics

    def test_post_validation_off_at_finalize_seam_is_inert(self, tmp_path: Path) -> None:
        # Flag-off inertness must hold AT the finalize seam, not only on the
        # unified-slice path the single-table off test exercises. An FK job
        # reaches full_frame finalize, so with the flag off none of the
        # post-validation keys may appear -- this guards the "a future
        # unconditional run trips this" invariant at the seam itself.
        cfg, src = _dup_pk_fk_config(tmp_path)
        off = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION)
        assert off.quality_metrics["execution"]["execution_mode"] == "full_frame"
        for key in ("quality_summary", "failed_checks", "post_validation_enforce"):
            assert key not in off.quality_metrics

    def test_forced_out_of_core_with_post_validation_fails_closed(self, tmp_path: Path) -> None:
        # Symmetric to the forced-sequential case: an operator cannot force a job
        # onto the out-of-core route (where the scans cannot run) while asking
        # for post_validation -- fail closed rather than silently skip the suite.
        from decoy_engine.errors import ConfigError

        cfg, src = _dup_pk_fk_config(tmp_path)
        with pytest.raises(ConfigError, match="post_validation_requested"):
            run_pipeline(
                cfg,
                sources=src,
                engine_version=_ENGINE_VERSION,
                execution_mode="out_of_core",
                post_validation=True,
            )


# --------------------------------------------------------------------------
# Config round-trip + privacy + combined flags
# --------------------------------------------------------------------------


class TestConfigRoundTripPrivacyCombined:
    def test_config_round_trips_post_validation_flags(self, tmp_path: Path) -> None:
        # post_validation_skip and post_validation_sample_size must reach the
        # runner. Skip 'leakage' on a leaky job -> no leakage failure, but the
        # rest of the suite still runs (the column is still sampled).
        cfg, src = _leaky_config(tmp_path)
        skipped = run_pipeline(
            cfg,
            sources=src,
            engine_version=_ENGINE_VERSION,
            post_validation=True,
            post_validation_skip=["leakage"],
        )
        summary = skipped.quality_metrics["quality_summary"]
        assert "leakage" not in summary["failed_checks"]
        assert "t.code" in summary["sampled_values"]  # the rest of the suite still ran

        # sample_size caps the per-column evidence.
        hcfg, hsrc = _hash_config(tmp_path, [f"u{i}@x.com" for i in range(10)])
        capped = run_pipeline(
            hcfg,
            sources=hsrc,
            engine_version=_ENGINE_VERSION,
            post_validation=True,
            post_validation_sample_size=2,
        )
        sampled = capped.quality_metrics["quality_summary"]["sampled_values"]
        assert all(len(v) == 2 for v in sampled.values())

    def test_summary_contains_no_source_pii(self, tmp_path: Path) -> None:
        source_values = ["a@x.com", "b@x.com", "c@x.com"]
        cfg, src = _hash_config(tmp_path, source_values)
        r = run_pipeline(cfg, sources=src, engine_version=_ENGINE_VERSION, post_validation=True)
        sampled = r.quality_metrics["quality_summary"]["sampled_values"]
        emitted = {v for values in sampled.values() for v in values}
        assert emitted  # synthetic spot-check rows were captured
        assert not (emitted & set(source_values)), "a source value leaked into the summary"

    def test_fidelity_and_post_validation_both_on(self, tmp_path: Path) -> None:
        # Both are full-frame-forcing report attachments; both must appear.
        cfg, src = _hash_config(tmp_path, ["a@x.com", "b@x.com", "c@x.com"])
        r = run_pipeline(
            cfg,
            sources=src,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            post_validation=True,
        )
        assert "fidelity_reports" in r.quality_metrics
        assert "quality_summary" in r.quality_metrics
        assert r.quality_metrics["execution"]["execution_mode"] == "full_frame"
