"""F4 fast wiring test: a tiny generate + mask job through the production mixed route.

Plan: docs/plans/2026-10-17-phase-f-f4-capacity.md section 4. The capacity worker runs in
a subprocess (so the external peak RSS is real) over a 2.5k-row job and the test checks
that the harness itself works: the run completed, the peak stayed under a generous small
ceiling, F2's own route evidence says `mixed` with the native backend and a streamed
sink, FK/payload parity held, and (in a separate process) the output is byte-identical
to the full-frame oracle. It also proves the verifier catches a corrupted FK, since a
verifier that cannot fail would make the scale evidence worthless. The 1M/10M tiers live
in `test_f4_mixed_capacity_scale.py` (opt-in).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.perf._f4_mixed_runner import run_worker

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(
        importlib.util.find_spec("decoy_engine_native") is None,
        reason="decoy-engine-native companion not installed",
    ),
]

PARENTS, CHILDREN = 500, 2000
# Force several chunks on a tiny table; real tiers use the engine defaults.
TINY = [
    "--parent-rows", str(PARENTS), "--child-rows", str(CHILDREN),
    "--chunk-rows", "300", "--chunk-threshold", "1",
]  # fmt: skip
CEILING_MB = 2000  # generous: the interpreter plus engine imports sit near 200 MB


def test_tiny_mixed_job_completes_on_the_mixed_route() -> None:
    record = run_worker([*TINY, "--rss-ceiling-mb", str(CEILING_MB)], timeout_s=300)
    assert record["completed"] is True, record["failure"]
    assert record["rss_within_ceiling"] is True, record["peak_rss_mb"]

    route = record["route"]
    assert route["execution_mode"] == "mixed"
    assert route["outputs_streamed"] is True
    assert route["result_outputs"] == 0
    assert all(route["checks"].values()), route["checks"]
    disp = route["disposition"]
    assert disp["chunk_count"] > 1
    assert disp["native_hash_columns"] == disp["hash_columns"] > 0

    parity = record["fk_parity"]
    assert parity["status"] == "ok", parity["problems"]
    assert parity["rows_out"] == {"people": PARENTS, "accounts": CHILDREN}
    assert (
        parity["fk_full_scan"]["orphan_rows"] == parity["fk_full_scan"]["expected_orphan_rows"] > 0
    )
    assert record["generation_phase"]["parent_arrow_nbytes"]["people"] > 0
    assert record["carrier_phase"]["resident_key_mb"] >= 0


def test_tiny_mixed_job_is_byte_identical_to_the_oracle() -> None:
    record = run_worker(["--mode", "oracle", *TINY], timeout_s=300)
    assert record["completed"] is True, record["failure"]
    oracle = record["oracle"]
    assert oracle["production_mode"] == "mixed"
    assert oracle["byte_identical"] is True, oracle["mismatch"]
    assert oracle["compared_tables"] == ["accounts", "people"]
    assert record["capacity_figure"] is False


def test_verifier_flags_a_corrupted_child_fk(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fk-scale"))
    import _mixed_layout as layout

    parents, rows, every, width = 10, 400, 25, 1
    src = tmp_path / "src.parquet"
    layout.write_child(src, rows=rows, parents=parents, orphan_every=every, width=width)
    committed = tmp_path / "committed"
    committed.mkdir()
    pq.write_table(
        pa.table({"id": [str(i) for i in range(parents)], "tier": ["gold"] * parents}),
        committed / "people.parquet",
    )
    child = pq.read_table(src)
    # Stand in for the engine: redact the note, rewrite hashed columns to something else.
    cols = {n: child.column(n) for n in child.column_names}
    cols["note"] = pa.array([layout.REDACTED] * rows)
    for name in ("email", *layout.payload_names(width)):
        cols[name] = pa.array([f"h{i}" for i in range(rows)])
    out = committed / "accounts.parquet"
    pq.write_table(pa.table(cols), out)

    kw = {"parents": parents, "child_rows": rows, "orphan_every": every, "width": width}
    assert layout.verify_committed(committed, src, **kw)["status"] == "ok"

    bad = cols["person_id"].to_pylist()
    bad[rows // 2] = str(int(bad[rows // 2]) + 1)  # one row points at the wrong parent (numeric string FK)
    cols["person_id"] = pa.array(bad, cols["person_id"].type)
    pq.write_table(pa.table(cols), out)
    verdict = layout.verify_committed(committed, src, **kw)
    assert verdict["status"] == "mismatch"
    assert "child_fk_full_scan" in verdict["problems"]
