"""Synthea scale-characterization harness: fixture builder, config, parity, worker.

Handoff RW-A / A-2. Small-N correctness of the tooling the 1M/10M/100M
characterization runs depend on; the runs themselves are evidence, not tests. The
worker is exercised through its real CLI in a subprocess (it measures a child
process), which also proves the reused supervisor contract end to end: a forced
OOM or timeout still yields a classified, non-silent record.

The harness reuses `scripts/fk-scale/_supervisor` rather than forking it, so the
supervisor's own contract tests live in `test_fk_scale_infra.py`; these cover the
Synthea-specific layout, PHI config, order-independent parity and worker record.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts" / "synthea-scale"
sys.path.insert(0, str(_SCRIPTS))

import _synthea_config as cfg  # noqa: E402
import _synthea_layout as layout  # noqa: E402
import _synthea_parity as parity  # noqa: E402
import build_synthea_dataset as builder  # noqa: E402

# patient < encounter < observation, asymmetric, matching the 1:5:14 production shape.
_COUNTS = {"patients": 1_000, "encounters": 5_000, "observations": 14_000}


# ------------------------------------------------------------------ fixture builder


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return builder.build(tmp_path_factory.mktemp("synthea"), _COUNTS, chunk_rows=700)


def test_builder_writes_the_declared_asymmetric_counts(chain: dict[str, Path]) -> None:
    assert {t: parity.table_rows(p) for t, p in chain.items()} == _COUNTS


def test_builder_is_three_tables_of_about_fifteen_columns(chain: dict[str, Path]) -> None:
    assert set(chain) == set(layout.TABLE_NAMES) and len(layout.TABLE_NAMES) == 3
    for table in layout.TABLE_NAMES:
        ncols = len(pq.ParquetFile(chain[table]).schema_arrow.names)
        assert ncols == layout.column_count(table)
        assert 10 <= ncols <= 15
    assert layout.total_columns() >= 30  # a wide, multi-table PHI workload


def test_builder_fk_references_resolve_and_are_coherent(chain: dict[str, Path]) -> None:
    patients = set(pq.read_table(chain["patients"], columns=["id"]).column(0).to_pylist())
    enc = pq.read_table(chain["encounters"], columns=["id", "patient"])
    obs = pq.read_table(chain["observations"], columns=["encounter", "patient"])
    enc_patient = dict(
        zip(enc.column("id").to_pylist(), enc.column("patient").to_pylist(), strict=True)
    )
    # encounters.patient points at a real patient, and every patient is referenced
    # (encounters >> patients, positional wrap).
    assert set(enc.column("patient").to_pylist()) == patients
    # observations.encounter points at a real encounter, and observations.patient is
    # the SAME patient that its encounter carries (denormalized, coherent).
    for e, p in zip(
        obs.column("encounter").to_pylist(), obs.column("patient").to_pylist(), strict=True
    ):
        assert enc_patient[e] == p


def test_builder_writes_chunk_by_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No table is built whole: every write is at most one chunk, one row group each."""
    sizes: list[int] = []
    real = pq.ParquetWriter.write_table

    def spy(self: pq.ParquetWriter, table: pa.Table, *a: object, **k: object) -> None:
        sizes.append(table.num_rows)
        real(self, table, *a, **k)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", spy)
    paths = builder.build(tmp_path, _COUNTS, chunk_rows=700)
    assert max(sizes) <= 700 and sum(sizes) == sum(_COUNTS.values())
    assert pq.ParquetFile(paths["observations"]).metadata.num_row_groups == 20  # ceil(14000/700)


def test_builder_is_deterministic(tmp_path: Path) -> None:
    a = builder.build(tmp_path / "a", _COUNTS, seed=3)
    b = builder.build(tmp_path / "b", _COUNTS, seed=3)
    assert pq.read_table(a["encounters"]).equals(pq.read_table(b["encounters"]))


def test_named_tiers_are_total_row_counts() -> None:
    for tier, counts in builder.TIERS.items():
        expected = {"1m": 1_000_000, "10m": 10_000_000, "100m": 100_000_000}[tier]
        assert sum(counts.values()) == expected
        assert counts["patients"] < counts["encounters"] < counts["observations"]


# ------------------------------------------------------------------------- config


def test_config_validates_against_pipeline_config(chain: dict[str, Path], tmp_path: Path) -> None:
    from decoy_engine.config import PipelineConfig

    config = cfg.build_config(chain, tmp_path / "out")
    PipelineConfig.model_validate(config)  # extra="forbid": a bad key is a hard reject


def test_config_covers_the_phi_strategy_families() -> None:
    strategies = cfg.declared_strategies()
    # The hospital PHI families: FPE identifiers, bucket-generalized dates, categorical
    # remap (names/address/city and the coded fields), text-redaction on the free-text
    # descriptions, ZIP truncation, and hashed FK keys/ids. All are out-of-core-supported
    # so the job runs on the bounded route.
    for required in (
        "fpe",
        "bucket_perturb",
        "categorical",
        "hash",
        "truncate",
        "text_redact",
    ):
        assert strategies.get(required, 0) >= 1


def test_config_binds_each_fk_edge_to_one_shared_namespace(
    chain: dict[str, Path], tmp_path: Path
) -> None:
    config = cfg.build_config(chain, tmp_path / "out")
    rels = {r["children"][0]["table"]: r for r in config["relationships"]}
    assert set(rels) == {"encounters", "observations"}
    assert rels["encounters"]["namespace"] == layout.NS_PATIENT
    assert rels["observations"]["namespace"] == layout.NS_ENCOUNTER


# -------------------------------------------------------------------------- parity


def _masked_copy(
    chain: dict[str, Path], out: Path, *, break_link: bool = False, leave_unmasked: bool = False
) -> None:
    """A stand-in for a correct run: every key and FK mapped through one consistent
    function (so the child FK resolves to a masked parent key), unless a flag injects a defect."""
    out.mkdir()
    key_like = {"id", "patient", "encounter"}

    def mask(col: pa.ChunkedArray) -> pa.ChunkedArray:
        return pc.binary_join_element_wise(pa.scalar("m_"), col, pa.scalar(""))

    for table, path in chain.items():
        t = pq.read_table(path)
        if not leave_unmasked:
            for col in [c for c in key_like if c in t.column_names]:
                t = t.set_column(t.column_names.index(col), col, mask(t.column(col)))
        if break_link and table == "observations":
            bad = t.column("encounter").to_pylist()
            bad[1] = "tampered_no_such_encounter"
            t = t.set_column(t.column_names.index("encounter"), "encounter", pa.array(bad))
        pq.write_table(t, out / f"{table}.parquet", row_group_size=700)


def test_parity_accepts_a_consistent_masking(chain: dict[str, Path], tmp_path: Path) -> None:
    _masked_copy(chain, tmp_path / "ok")
    verdict = parity.verify_committed_chain(chain, tmp_path / "ok", sample=300)
    assert verdict["rowcount_match"] and verdict["parity"]["status"] == "ok"
    assert set(verdict["parity"]["edges"]) == {"patients->encounters", "encounters->observations"}


def test_parity_rejects_a_broken_fk(chain: dict[str, Path], tmp_path: Path) -> None:
    _masked_copy(chain, tmp_path / "bad", break_link=True)
    verdict = parity.verify_committed_chain(chain, tmp_path / "bad", sample=300)
    assert verdict["parity"]["status"] == "MISMATCH"
    assert verdict["parity"]["edges"]["encounters->observations"]["reason"] == "unresolved_fk"


def test_parity_rejects_unmasked_keys(chain: dict[str, Path], tmp_path: Path) -> None:
    _masked_copy(chain, tmp_path / "raw", leave_unmasked=True)
    verdict = parity.verify_committed_chain(chain, tmp_path / "raw", sample=300)
    assert verdict["parity"]["status"] == "MISMATCH"
    assert any(
        e.get("reason") == "parent_key_unmasked" for e in verdict["parity"]["edges"].values()
    )


def test_parity_rejects_row_count_drift(chain: dict[str, Path], tmp_path: Path) -> None:
    short = tmp_path / "short"
    short.mkdir()
    for table, path in chain.items():
        pq.write_table(pq.read_table(path).slice(0, 10), short / f"{table}.parquet")
    assert not parity.verify_committed_chain(chain, short)["rowcount_match"]


def test_parity_is_order_independent(chain: dict[str, Path], tmp_path: Path) -> None:
    """A correct run whose rows are reordered (as the batch route may do) still passes:
    the check is referential, not positional."""
    out = tmp_path / "shuffled"
    out.mkdir()
    key_like = {"id", "patient", "encounter"}

    def mask(col: pa.ChunkedArray) -> pa.ChunkedArray:
        return pc.binary_join_element_wise(pa.scalar("m_"), col, pa.scalar(""))

    for table, path in chain.items():
        t = pq.read_table(path)
        for col in [c for c in key_like if c in t.column_names]:
            t = t.set_column(t.column_names.index(col), col, mask(t.column(col)))
        shuffled = t.take(pa.array(list(reversed(range(t.num_rows)))))
        pq.write_table(shuffled, out / f"{table}.parquet", row_group_size=700)
    assert parity.verify_committed_chain(chain, out, sample=300)["parity"]["status"] == "ok"


def test_read_window_wraps_past_the_end(chain: dict[str, Path]) -> None:
    got = layout.read_window(chain["patients"], "id", _COUNTS["patients"] - 2, 4)
    assert got == ["P998", "P999", "P0", "P1"]


# ----------------------------------------------------------------------- full worker


def _worker(data_dir: Path, work_root: Path, *extra: str) -> tuple[int, dict]:
    proc = subprocess.run(  # noqa: S603 -- fixed in-repo script, test-controlled args
        [sys.executable, str(_SCRIPTS / "bench_worker_synthea.py"), "--data-dir", str(data_dir),
         "--work-root", str(work_root), *extra],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("BENCH_JSON "))
    return proc.returncode, json.loads(line.removeprefix("BENCH_JSON "))


@pytest.fixture(scope="module")
def small_chain(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("worker_in")
    builder.build(out, _COUNTS, chunk_rows=1000)
    return out


def test_worker_emits_a_complete_characterization_record(small_chain: Path, tmp_path: Path) -> None:
    rc, rec = _worker(small_chain, tmp_path, "--rss-ceiling-mb", "8192")
    assert rc == 0 and rec["schema"] == "synthea-scale/v1"
    assert rec["completed"] is True and rec["failure"] is None
    assert rec["rows"] == _COUNTS and rec["total_rows"] == sum(_COUNTS.values())
    assert rec["table_count"] == 3 and rec["column_count"] == layout.total_columns()
    # Real engine path, native ON: the companion is present and most columns ran it.
    assert rec["native_companion"]["present"] is True
    disp = rec["disposition"]
    assert disp["columns_total"] > 0 and disp["native_columns"] > 0
    assert disp["native_columns"] + len(disp["non_native"]) == disp["columns_total"]
    # Size-to-time instrumentation, measured not estimated.
    assert rec["mask_s"] > 0 and rec["throughput_rows_per_s"] > 0
    assert rec["peak_rss_mb"] > 0 and rec["rss_method"] == "wait4_ru_maxrss"
    assert rec["rss_within_ceiling"] is True
    assert rec["route"]["execution_mode"] and rec["route"]["route_reason"]
    assert rec["machine"]["cpu_count"] and rec["run_date_utc"]
    # FK parity on every edge.
    assert rec["rowcount_match"] and rec["parity"]["status"] == "ok"


def test_worker_forced_oom_is_not_a_silent_pass(small_chain: Path, tmp_path: Path) -> None:
    # A tight RLIMIT_DATA cap forces a resource failure; a short timeout backstops the
    # mmap-allocator path. Either way: completed False with a recorded reason, rc != 0.
    rc, rec = _worker(small_chain, tmp_path, "--mem-cap-mb", "64", "--timeout-s", "60")
    assert rc != 0
    assert rec["completed"] is False and rec["failure"] is not None
    assert rec["requested"]["mem_cap_kind"] == "RLIMIT_DATA"
    assert rec["peak_rss_mb"] > 0  # external measurement survives the failure


def test_worker_timeout_still_emits_a_record(small_chain: Path, tmp_path: Path) -> None:
    rc, rec = _worker(small_chain, tmp_path, "--timeout-s", "0.4")
    assert rc != 0 and rec["completed"] is False and rec["failure"]["kind"] == "timeout"


def test_error_record_reports_frames_without_message_or_source() -> None:
    import bench_worker_synthea as worker

    secret = "SECRET-PHI-VALUE"

    def _thrower() -> None:
        local_value = secret  # noqa: F841
        raise ValueError(secret)

    try:
        _thrower()
    except ValueError as exc:
        record = worker._error_record(exc)
    assert record["type"] == "ValueError"
    assert any(f.endswith(":_thrower") for f in record["frames"])
    assert all(f.count(":") == 2 for f in record["frames"])
    assert secret not in json.dumps(record)


# -------------------------------------------------- A-2 remediation acceptance (F1/F2/F3)


def test_names_and_addresses_are_genuinely_transformed_on_ooc(
    chain: dict[str, Path], tmp_path: Path
) -> None:
    """F1: the direct-identifier name/address columns are GENUINELY transformed on the
    out-of-core route. They used to be `text_redact`, a free-text span pass that finds no
    spans in a single-token name cell and leaves it UNCHANGED; they are now a deterministic
    `categorical` remap onto a synthetic pool. The masked pools are disjoint from the source
    generation pools, so a correctly-masked column shares no value with its source."""
    import decoy_engine
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
    from decoy_engine.profile._readers import LazySource

    config = cfg.build_config(chain, tmp_path / "cfg-out")
    PipelineConfig.model_validate(config)
    committed = tmp_path / "committed"
    result = run_pipeline(
        config,
        {t: LazySource(path=p) for t, p in chain.items()},
        engine_version=decoy_engine.__version__,
        execution_mode="out_of_core",  # the route this harness characterizes
        sink=ParquetTransactionalSink(committed),
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    src = pq.read_table(chain["patients"])
    out = pq.read_table(committed / "patients.parquet")
    for col in ("first", "last", "address", "city"):
        src_vals = set(src.column(col).to_pylist())
        out_vals = set(out.column(col).to_pylist())
        # Transformed: no source value survives into the masked output.
        assert src_vals.isdisjoint(out_vals), f"{col!r} not transformed: {src_vals & out_vals}"
        # And the output is name-shaped (drawn from the column's synthetic pool), not blanked.
        assert out_vals and all(isinstance(v, str) and v for v in out_vals)


def test_config_does_not_claim_post_validation(chain: dict[str, Path], tmp_path: Path) -> None:
    """F2: the engine's `post_validation` scan is a run_pipeline runtime kwarg, not a
    `global_settings` field, and turning it on forces the job off the out-of-core route this
    harness measures. The config must not carry the inert/misleading `post_validation` flag,
    and the worker must not pass the kwarg; the harness's own `_synthea_parity` sample is the
    FK check."""
    config = cfg.build_config(chain, tmp_path / "out")
    assert "post_validation" not in config["global_settings"]
    worker_src = (_SCRIPTS / "bench_worker_synthea.py").read_text()
    assert "post_validation=True" not in worker_src


def test_sizing_guide_labels_100m_as_extrapolation() -> None:
    """F3: doc-lint. The reader-facing guide must present 1M/10M as measured and the 100M
    row as a clearly-labelled extrapolation, and must not carry a blanket "every number is
    measured" claim."""
    guide = (_ROOT / "docs" / "guides" / "sizing-and-hardware.md").read_text().lower()
    assert "extrapolat" in guide  # the 100M method is shown as an extrapolation
    # The 100M table row is labelled extrapolated, on the same line as the row.
    row_100m = next(ln for ln in guide.splitlines() if "100,000,000" in ln)
    assert "extrapolat" in row_100m
    # No blanket "measured, not estimated or extrapolated" claim remains.
    assert "not estimated or extrapolated" not in guide
    # prodsim is named as the source of real large-scale benchmarks.
    assert "prodsim" in guide
