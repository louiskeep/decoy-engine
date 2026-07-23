"""DPS Scope B (guide section 4.7): YAML round trip of the `generation`
block.

`plan_from_yaml` is not a blind round trip for this block: it recomputes
both the embedded-snapshot digests and the `dp_verification` receipt
from the pinned bytes, because a YAML manifest is untrusted input the
moment it leaves the process that wrote it. These tests pin that
distinction directly -- a hand-edited manifest with a mismatched digest,
or a forged `dp_verification` receipt, must not survive deserialization
unnoticed.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from decoy_engine.plan import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.profile import Profile
from decoy_engine.quality.dp import fit_dp_snapshot


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def _dp_fit_mixed(tmp_path, *, n=400, epsilon=5.0, delta=1e-6, name: str = "dp.json") -> str:
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
    path = tmp_path / name
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _compiled_dp_plan(tmp_path, *, epsilon: float = 5.0, delta: float = 1e-6):
    path = _dp_fit_mixed(tmp_path, epsilon=epsilon, delta=delta)
    cfg = {
        "global_settings": {"seed": 1, "dp": {"epsilon": epsilon, "delta": delta}},
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
    return compile_plan(cfg, _profile(), decoy_engine_version="test")


def _compiled_dp_plan_two_snapshots(tmp_path, *, epsilon: float = 2.0, delta: float = 1e-6):
    """D-H2 (dennis HIGH): a Plan with TWO statistical columns pointing at
    TWO DIFFERENT `snapshot_file` paths, each its own independent DP fit
    (own release_id). Declares a generous ceiling since two independent
    releases compose (never dedup by content)."""
    path_a = _dp_fit_mixed(tmp_path, name="dp_a.json", epsilon=epsilon, delta=delta)
    path_b = _dp_fit_mixed(tmp_path, name="dp_b.json", epsilon=epsilon, delta=delta)
    cfg = {
        "global_settings": {"seed": 1, "dp": {"epsilon": 5 * epsilon, "delta": 5 * delta}},
        "tables": [
            {
                "name": "t",
                "row_count": 5,
                "generate_columns": [
                    {"name": "state", "type": "statistical", "snapshot_file": path_a},
                    {"name": "age", "type": "statistical", "snapshot_file": path_b},
                ],
            }
        ],
    }
    return compile_plan(cfg, _profile(), decoy_engine_version="test")


def test_plan_yaml_round_trip_preserves_pinned_snapshot(tmp_path):
    plan = _compiled_dp_plan(tmp_path)
    assert plan.generation is not None
    original = plan.generation.snapshots[0]

    reloaded = plan_from_yaml(plan_to_yaml(plan))

    assert reloaded.generation is not None
    restored = reloaded.generation.snapshots[0]
    assert restored.source_path == original.source_path
    assert restored.sha256 == original.sha256
    assert restored.payload_b64 == original.payload_b64
    assert restored.release_id == original.release_id
    assert reloaded.generation.config_json == plan.generation.config_json
    assert reloaded.generation.dp_verification == plan.generation.dp_verification


def test_plan_from_yaml_rejects_pinned_snapshot_digest_mismatch(tmp_path):
    """Corrupting the embedded bytes (payload changes, sha256 field left
    stale) must be caught on load, not silently trusted."""
    plan = _compiled_dp_plan(tmp_path)
    rendered = plan_to_yaml(plan)
    data = __import__("yaml").safe_load(rendered)

    tampered_bytes = base64.b64decode(data["generation"]["snapshots"][0]["payload_b64"]) + b"\n"
    data["generation"]["snapshots"][0]["payload_b64"] = base64.b64encode(tampered_bytes).decode(
        "ascii"
    )
    # sha256 field deliberately left unchanged: a manifest editor who forgot
    # (or declined) to update the digest to match tampered bytes.
    tampered_yaml = __import__("yaml").safe_dump(data, sort_keys=False)

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(tampered_yaml)
    assert exc.value.code == "dp_pinned_snapshot_digest_mismatch"


def test_plan_from_yaml_recomputes_dp_verification_receipt(tmp_path):
    """A manifest claiming a `dp_verification` receipt the embedded bytes
    do not actually support must not survive deserialization unnoticed:
    the loaded Plan's receipt is recomputed from the pinned snapshot
    bytes via `verify_dp_snapshots`, discarding whatever the serialized
    `dp_verification` block claims."""
    plan = _compiled_dp_plan(tmp_path, epsilon=5.0, delta=1e-6)
    rendered = plan_to_yaml(plan)
    data = __import__("yaml").safe_load(rendered)

    real_epsilon_total = data["generation"]["dp_verification"]["epsilon_total"]
    real_release_ids = list(data["generation"]["dp_verification"]["release_ids"])
    # Forge a receipt claiming a smaller epsilon_total and a fake extra
    # release_id -- neither is true of the embedded bytes.
    data["generation"]["dp_verification"]["epsilon_total"] = 1e-9
    data["generation"]["dp_verification"]["release_ids"] = [*real_release_ids, "forged-release-id"]
    forged_yaml = __import__("yaml").safe_dump(data, sort_keys=False)

    reloaded = plan_from_yaml(forged_yaml)

    assert reloaded.generation is not None
    recomputed = reloaded.generation.dp_verification
    assert recomputed is not None
    # The recomputed receipt matches what verify_dp_snapshots derives from
    # the actual pinned bytes -- the original, un-forged value -- not the
    # forged claim that was on the wire.
    assert recomputed.epsilon_total == pytest.approx(real_epsilon_total)
    assert recomputed.release_ids == tuple(real_release_ids)
    assert "forged-release-id" not in recomputed.release_ids


def test_plan_from_yaml_rebuilds_statistical_spec_from_pinned_bytes_not_the_serialized_spec(
    tmp_path,
):
    """H5: `_pinned_statistical_spec_from_dict` used to only refreeze the
    serialized `statistical_specs[i].spec` dict, never re-running
    `load_spec` against the pinned snapshot bytes. A manifest whose
    declared spec disagreed with `snapshots[i].payload_b64` passed every
    check, and generation would sample from the untrusted spec rather
    than the bytes it was supposedly pinned to. This forges exactly that
    disagreement -- flips the serialized spec's `other_mode` to a value
    the real pinned snapshot's config never declared -- and asserts the
    loaded Plan's spec reflects the REAL config + pinned bytes, not the
    forged one."""
    plan = _compiled_dp_plan(tmp_path)
    rendered = plan_to_yaml(plan)
    data = __import__("yaml").safe_load(rendered)

    specs = data["generation"]["statistical_specs"]
    assert len(specs) == 1
    real_other_mode = specs[0]["spec"]["other_mode"]
    forged_other_mode = "emit" if real_other_mode != "emit" else "redistribute"
    assert forged_other_mode != real_other_mode
    specs[0]["spec"]["other_mode"] = forged_other_mode
    forged_yaml = __import__("yaml").safe_dump(data, sort_keys=False)

    reloaded = plan_from_yaml(forged_yaml)

    assert reloaded.generation is not None
    rebuilt_specs = reloaded.generation.statistical_specs
    assert len(rebuilt_specs) == 1
    # The rebuilt spec matches what load_spec derives from the embedded
    # config + pinned bytes -- the real other_mode -- not the forged value
    # that was on the wire.
    assert rebuilt_specs[0].spec["other_mode"] == real_other_mode
    assert rebuilt_specs[0].spec["other_mode"] != forged_other_mode


def test_plan_from_yaml_refuses_unpinned_snapshot_file_instead_of_reading_disk(tmp_path):
    """HIGH H-A: deserialization must be a pure function of the manifest
    bytes, never a filesystem read plus an existence oracle over a path
    named in the untrusted document. This empties `generation.snapshots`
    (so `pinned_by_path` at load time covers nothing) and overwrites the
    on-disk `snapshot_file` with unparseable content, so any fallback to
    a direct read would raise `statistical_snapshot_unreadable` -- the
    exact defect demonstrated against the reviewer's finding. The fixed
    behavior must instead raise `statistical_snapshot_not_pinned` without
    ever touching the path again."""
    plan = _compiled_dp_plan(tmp_path)
    rendered = plan_to_yaml(plan)
    data = __import__("yaml").safe_load(rendered)

    snapshot_path = data["generation"]["snapshots"][0]["source_path"]
    data["generation"]["snapshots"] = []
    tampered_yaml = __import__("yaml").safe_dump(data, sort_keys=False)

    with open(snapshot_path, "w", encoding="utf-8") as fh:
        fh.write("NOT JSON AT ALL")

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(tampered_yaml)
    assert exc.value.code == "statistical_snapshot_not_pinned"


def test_plan_from_yaml_refuses_when_only_one_of_two_pinned_snapshots_is_removed(tmp_path):
    """D-H2 (dennis HIGH): the regression test above only covers emptying
    `generation.snapshots` ENTIRELY, which a guard shaped like `if
    snapshot_file and str(snapshot_file) not in pinned and not pinned:`
    would also pass (`pinned` is empty either way, so the `and not
    pinned` clause is trivially true and never distinguishes anything).
    This compiles a Plan with TWO statistical columns pointing at two
    DIFFERENT snapshot_file paths and removes only the SECOND pinned
    entry, leaving `pinned` non-empty (it still contains the first). A
    guard with that extra clause would see a non-empty `pinned`, take the
    `and not pinned` branch to False, and silently fall through to
    `_load_snapshot` reopening the removed path -- exactly the concrete
    escape the finding names. The shipped guard here has no such clause
    and does not change; this only adds the missing fixture."""
    plan = _compiled_dp_plan_two_snapshots(tmp_path)
    rendered = plan_to_yaml(plan)
    data = __import__("yaml").safe_load(rendered)

    assert len(data["generation"]["snapshots"]) == 2
    removed_path = data["generation"]["snapshots"][1]["source_path"]
    data["generation"]["snapshots"] = [data["generation"]["snapshots"][0]]
    tampered_yaml = __import__("yaml").safe_dump(data, sort_keys=False)

    # Overwrite the removed path with unparseable content: any fallback to
    # a direct read would raise statistical_snapshot_unreadable instead.
    with open(removed_path, "w", encoding="utf-8") as fh:
        fh.write("NOT JSON AT ALL")

    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(tampered_yaml)
    assert exc.value.code == "statistical_snapshot_not_pinned"


def test_plan_from_yaml_without_generation_block_round_trips_none(tmp_path):
    """A Plan compiled with no generate_columns has no `generation` key at
    all on the wire (guide section 4.7); it must deserialize back to
    `generation=None`, not an empty/default GenerationPlan."""
    cfg = {"global_settings": {"seed": 1}, "tables": []}
    plan = compile_plan(cfg, _profile(), decoy_engine_version="test")
    assert plan.generation is None

    reloaded = plan_from_yaml(plan_to_yaml(plan))
    assert reloaded.generation is None
