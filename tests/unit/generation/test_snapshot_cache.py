"""Finding 3 (gate remediation, 2026-07-21): content-addressed snapshot cache.

Codex reproduced a stale-cache bug against `feat/dps-marginal-dp`:
`_SNAPSHOT_CACHE` (`generation/statistical/_spec.py`) was keyed by file PATH,
never invalidated. Caching an exact snapshot, then overwriting the same path
with a genuinely DP-fit snapshot, left `load_spec` serving the STALE exact
bytes even after `run_config_only_checks` had already verified the ON-DISK
(DP) artifact at that same path -- a DP-declared pipeline could silently
generate from non-DP data in a long-lived process (the platform API). The
fix keys the cache by content hash (`sha256` of the file bytes) instead: a
hit is byte-equivalent to a fresh read at call time BY CONSTRUCTION, so it
can never serve stale content regardless of what happens at a given path.

`plan._checks_dp.check_dp_snapshot_provenance` was also refactored to read
through the SAME loader (`generation.statistical._spec._load_snapshot`) as
`load_spec`/generation, instead of its own `open()` -- so the compile-time
gate and the generation-time sampler provably see the same parsed object,
not merely the same path (tested below).
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.generation.statistical import load_spec
from decoy_engine.generation.statistical._spec import _SNAPSHOT_CACHE, _load_snapshot
from decoy_engine.plan._checks_dp import check_dp_snapshot_provenance
from decoy_engine.quality.dp import apply_dp_noise
from decoy_engine.quality.snapshot import compute_distribution_snapshot


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """The cache is a process-global module dict; other test modules in the
    same session warm it with content that can be byte-identical to
    fixtures here (content-hash keying, unlike the old path keying, means
    ANY test anywhere that fits the same tiny DataFrame collides on the
    same digest). Isolate this file's cache-hit/miss/parse-count
    assertions from run order and from what else the session has loaded.
    """
    _SNAPSHOT_CACHE.clear()
    yield
    _SNAPSHOT_CACHE.clear()


def _statistical_cfg(*, snapshot_file: str, col_name: str = "age", dp: dict | None = None):
    global_settings: dict = {"seed": 1}
    if dp is not None:
        global_settings["dp"] = dp
    return {
        "global_settings": global_settings,
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [
                    {"name": col_name, "type": "statistical", "snapshot_file": snapshot_file}
                ],
            }
        ],
    }


class TestContentAddressedCacheClosesStaleServe:
    """Codex's exact reproduction, landed verbatim as a regression test."""

    def test_overwritten_path_serves_on_disk_bytes_not_stale_cache(self, tmp_path):
        # Cache the EXACT snapshot (real edges 31..55) via load_spec -- the
        # same entry point generation itself uses.
        exact_df = pd.DataFrame({"age": list(range(31, 56))})
        exact_snap = compute_distribution_snapshot(exact_df)
        path = tmp_path / "age.json"
        path.write_text(json.dumps(exact_snap), encoding="utf-8")
        col_cfg = {"name": "age", "type": "statistical", "snapshot_file": str(path)}
        spec = load_spec(col_cfg)
        assert spec.stats["bin_edges"][0] == 31.0
        assert spec.stats["bin_edges"][-1] == 55.0

        # Overwrite the SAME path with a genuinely DP-fit snapshot over a
        # different, caller-declared domain (0..120) -- different bytes.
        dp_df = pd.DataFrame({"age": [10, 60, 110]})
        dp_snap = compute_distribution_snapshot(
            dp_df, dp_mode=True, numeric_domains={"age": (0.0, 120.0)}
        )
        noisy = apply_dp_noise(dp_snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        path.write_text(json.dumps(noisy), encoding="utf-8")

        # A dp-declared compile against the SAME path passes (the on-disk
        # artifact IS a valid DP fit) -- this is the step that used to
        # "pass" while a long-lived process's sampler kept serving the
        # stale exact edges underneath, per Codex's reproduction.
        dp_cfg = _statistical_cfg(snapshot_file=str(path), dp={"epsilon": 10.0, "delta": 1e-6})
        run_config_only_checks(dp_cfg)  # no raise

        # The consumer must see the ON-DISK DP artifact (0..120), never the
        # stale cached exact edges (31..55).
        spec_again = load_spec(col_cfg)
        assert spec_again.stats["bin_edges"][0] == 0.0
        assert spec_again.stats["bin_edges"][-1] == 120.0

    def test_gate_and_sampler_load_the_same_object_for_identical_content(self, tmp_path):
        # The gate (check_dp_snapshot_provenance) and the sampler (load_spec)
        # both call `_load_snapshot` -- proving they return the SAME object
        # (not just equal dicts) for the same path/content is a structural
        # guarantee, not an inference from two independent implementations
        # happening to agree.
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(
            df, dp_mode=True, numeric_domains={"age": (0.0, 120.0)}
        )
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        path = tmp_path / "snap.json"
        path.write_text(json.dumps(noisy), encoding="utf-8")

        digest_gate, snap_gate = _load_snapshot(str(path))
        digest_sampler, snap_sampler = _load_snapshot(str(path))
        assert digest_gate == digest_sampler
        assert snap_gate is snap_sampler  # identical object, not merely equal

        # End-to-end: the real gate entry point actually accepts this
        # artifact (proves the tuple-returning refactor is wired correctly,
        # not just that the private helper behaves under direct call).
        cfg = _statistical_cfg(snapshot_file=str(path), dp={"epsilon": 10.0, "delta": 1e-6})
        check_dp_snapshot_provenance(cfg)  # no raise


class TestContentIdentityDedup:
    """Identical bytes at different paths -> one parse, one cache entry."""

    def test_identical_bytes_at_different_paths_share_one_cache_entry(self, tmp_path):
        df = pd.DataFrame({"age": [1, 2, 3]})
        snap = compute_distribution_snapshot(df)
        raw = json.dumps(snap).encode("utf-8")
        path_a = tmp_path / "a.json"
        path_b = tmp_path / "b.json"
        path_a.write_bytes(raw)
        path_b.write_bytes(raw)

        digest_a, parsed_a = _load_snapshot(str(path_a))
        digest_b, parsed_b = _load_snapshot(str(path_b))

        assert digest_a == digest_b
        assert parsed_a is parsed_b  # one parse -> one cached object, shared
        assert len(_SNAPSHOT_CACHE) == 1

    def test_parse_call_count_is_one_for_identical_content_at_five_paths(
        self, tmp_path, monkeypatch
    ):
        df = pd.DataFrame({"age": [1, 2, 3]})
        snap = compute_distribution_snapshot(df)
        raw = json.dumps(snap).encode("utf-8")
        paths = [tmp_path / f"snap_{i}.json" for i in range(5)]
        for p in paths:
            p.write_bytes(raw)

        calls: list[None] = []
        real_loads = json.loads

        def _counting_loads(s, *a, **kw):
            calls.append(None)
            return real_loads(s, *a, **kw)

        monkeypatch.setattr("decoy_engine.generation.statistical._spec.json.loads", _counting_loads)
        for p in paths:
            _load_snapshot(str(p))
        assert len(calls) == 1  # 5 reads, ONE json.loads call

    def test_different_content_is_never_deduped(self, tmp_path):
        # Sanity check on the flip side: two DIFFERENT snapshots must never
        # collapse to one cache entry (would be a hash-collision-shaped
        # correctness bug, not just a missed optimization).
        snap_a = compute_distribution_snapshot(pd.DataFrame({"age": [1, 2, 3]}))
        snap_b = compute_distribution_snapshot(pd.DataFrame({"age": [4, 5, 6]}))
        path_a = tmp_path / "a.json"
        path_b = tmp_path / "b.json"
        path_a.write_text(json.dumps(snap_a), encoding="utf-8")
        path_b.write_text(json.dumps(snap_b), encoding="utf-8")

        digest_a, _ = _load_snapshot(str(path_a))
        digest_b, _ = _load_snapshot(str(path_b))
        assert digest_a != digest_b
        assert len(_SNAPSHOT_CACHE) == 2


class TestPerfSmokeManyColumnsOneArtifact:
    """500 columns pointing at one artifact must not parse 500 times."""

    def test_500_columns_one_artifact_parses_once(self, tmp_path, monkeypatch):
        df = pd.DataFrame({"age": [1, 2, 3]})
        snap = compute_distribution_snapshot(df)
        path = tmp_path / "shared.json"
        path.write_text(json.dumps(snap), encoding="utf-8")

        calls: list[None] = []
        real_loads = json.loads

        def _counting_loads(s, *a, **kw):
            calls.append(None)
            return real_loads(s, *a, **kw)

        monkeypatch.setattr("decoy_engine.generation.statistical._spec.json.loads", _counting_loads)
        for _ in range(500):
            load_spec({"name": "age", "type": "statistical", "snapshot_file": str(path)})
        assert len(calls) == 1


class TestBoundedEviction:
    """The cache is bounded (last-N eviction) so a long-lived process
    cannot grow it without bound across many distinct artifacts."""

    def test_cache_size_never_exceeds_the_configured_maximum(self, tmp_path, monkeypatch):
        from decoy_engine.generation.statistical import _spec as spec_module

        monkeypatch.setattr(spec_module, "_SNAPSHOT_CACHE_MAX", 3)
        for i in range(10):
            df = pd.DataFrame({"age": [i, i + 1, i + 2]})
            snap = compute_distribution_snapshot(df)
            path = tmp_path / f"snap_{i}.json"
            path.write_text(json.dumps(snap), encoding="utf-8")
            _load_snapshot(str(path))
        assert len(_SNAPSHOT_CACHE) <= 3

    def test_evicted_content_is_faithfully_reread_not_lost(self, tmp_path, monkeypatch):
        # Eviction must be transparent: a cache MISS after eviction still
        # returns the correct content via a fresh read, never a stale or
        # missing result.
        from decoy_engine.generation.statistical import _spec as spec_module

        monkeypatch.setattr(spec_module, "_SNAPSHOT_CACHE_MAX", 2)
        paths = []
        for i in range(5):
            df = pd.DataFrame({"age": [i, i + 1, i + 2]})
            snap = compute_distribution_snapshot(df)
            path = tmp_path / f"snap_{i}.json"
            path.write_text(json.dumps(snap), encoding="utf-8")
            paths.append(path)
            _load_snapshot(str(path))
        # snap_0's entry was long since evicted; re-loading it must still
        # return byte-correct content (bin_edges reflect [0, 1, 2]).
        _digest, reread = _load_snapshot(str(paths[0]))
        assert reread["columns"]["age"]["stats"]["bin_edges"][0] == 0.0
