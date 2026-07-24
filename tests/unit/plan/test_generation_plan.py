"""DPS Scope B (guide section 4.7): `compile_plan`'s read-once snapshot
pinning and the resulting `GenerationPlan`'s immutability.

The load-bearing property here is structural, not behavioral: a snapshot
path referenced by more than one `type: statistical` generate column
must be opened exactly once per compile (a TOCTOU window otherwise
exists between two reads of the "same" path within one compilation), and
the parsed generation payload embedded into the Plan must be reachable
only through frozen containers, never a mutable dict/list a caller could
edit after the fact.
"""

from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from types import MappingProxyType

import numpy as np
import pandas as pd
import pytest

from decoy_engine.plan import compile_plan
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
        # Phase-5 `column_schema` fit API (guide section 3.5): a `text` carrier
        # for the categorical column, a `number` carrier + bounds for the numeric.
        {
            "state": {"kind": "categorical", "carrier": "text"},
            "age": {"kind": "numeric", "carrier": "number", "bounds": (0.0, 120.0)},
        },
        epsilon=epsilon,
        delta=delta,
    )
    path = tmp_path / "dp.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _dp_cfg(*, table_columns: list[dict], epsilon: float = 5.0, delta: float = 1e-6) -> dict:
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": epsilon, "delta": delta}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": table_columns}],
    }


@pytest.mark.dp_certified
def test_compile_plan_reads_each_snapshot_path_once(tmp_path, monkeypatch):
    """Two statistical columns (age, state) share ONE snapshot_file path.
    `compile_plan` must open that path exactly once across the whole
    compile, not once per referencing column and not once per check that
    consults it (guide section 4.7's "read-once-pinned" contract)."""
    path = _dp_fit_mixed(tmp_path)
    cfg = _dp_cfg(
        table_columns=[
            {"name": "age", "type": "statistical", "snapshot_file": path},
            {"name": "state", "type": "statistical", "snapshot_file": path},
        ]
    )

    open_calls: list[str] = []
    real_open = builtins.open

    def _counting_open(file, *args, **kwargs):
        if isinstance(file, str) and file == path:
            open_calls.append(file)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _counting_open)

    plan = compile_plan(cfg, _profile(), decoy_engine_version="test")

    assert open_calls == [path]
    assert plan.generation is not None
    assert len(plan.generation.snapshots) == 1  # deduped: one PinnedSnapshot for the shared path


@pytest.mark.dp_certified
def test_compile_plan_embeds_snapshot_bytes_and_digest(tmp_path):
    """The embedded `payload_b64` decodes back to the exact on-disk bytes,
    and `sha256` is that same content's digest -- not a placeholder and
    not derived from the parsed/re-serialized form (guide section 4.7:
    "embed exact bytes, not a path-based promise")."""
    import base64
    import hashlib

    path = _dp_fit_mixed(tmp_path)
    with open(path, "rb") as fh:
        raw_on_disk = fh.read()

    cfg = _dp_cfg(table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}])
    plan = compile_plan(cfg, _profile(), decoy_engine_version="test")

    assert plan.generation is not None
    (snapshot,) = plan.generation.snapshots
    assert snapshot.source_path == path
    assert base64.b64decode(snapshot.payload_b64) == raw_on_disk
    assert snapshot.sha256 == hashlib.sha256(raw_on_disk).hexdigest()


@pytest.mark.dp_certified
def test_compiled_generation_plan_is_recursively_immutable(tmp_path):
    """No mutable dict or list survives the freeze: a `PinnedStatisticalSpec.
    spec` is a `MappingProxyType` all the way down, so a caller holding a
    reference to the compiled Plan cannot reach into it and mutate a
    frozen field out from under later readers."""
    path = _dp_fit_mixed(tmp_path)
    cfg = _dp_cfg(table_columns=[{"name": "state", "type": "statistical", "snapshot_file": path}])
    plan = compile_plan(cfg, _profile(), decoy_engine_version="test")

    assert plan.generation is not None
    (spec,) = plan.generation.statistical_specs
    assert isinstance(spec.spec, MappingProxyType)

    # Top-level mapping refuses item assignment (MappingProxyType semantics).
    try:
        spec.spec["kind"] = "tampered"  # type: ignore[index]
        raised = False
    except TypeError:
        raised = True
    assert raised, "MappingProxyType must refuse mutation at the top level"

    # Nested mapping (StatisticalSpec.stats) is frozen too, not a plain dict
    # left reachable one level down.
    stats = spec.spec["stats"]
    assert isinstance(stats, MappingProxyType)

    # Any list-shaped stats field (e.g. bin_edges/bin_counts/top_values)
    # comes back as a tuple, never a mutable list, wherever it appears.
    def _assert_no_mutable_containers(value: object) -> None:
        assert not isinstance(value, (list, dict))
        if isinstance(value, MappingProxyType):
            for v in value.values():
                _assert_no_mutable_containers(v)
        elif isinstance(value, tuple):
            for v in value:
                _assert_no_mutable_containers(v)

    _assert_no_mutable_containers(spec.spec)
