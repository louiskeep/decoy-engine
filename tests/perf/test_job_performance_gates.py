"""P0 (job-performance sprints, docs/job-performance-sprints.md sec. 3):
job-level performance gates + baselines at the public `run_pipeline`
entrypoint.

Scope: this module benchmarks the NON-FK job shapes buildable on
`feat/engine-efficiencies` -- `main` does not carry the FK stack
(`_sequential.py`, `_transactional_sink.py`, `out_of_core/`), and the
job-performance sprint plan keeps that stack on its own branches under
the ordered merge plan. FK parent/child masking, the sequential
relationship route, and the out-of-core relationship route are
deliberately OUT of this module; their benchmarks live with the FK
stack on its own branch, not here. Covered instead:

- scalar no-FK mask job (hash / truncate / redact / passthrough /
  bucketize mix);
- faker-heavy wide table (many deterministic faker columns);
- FPE-heavy table (many fpe columns);
- text-redaction-heavy table (PII-bearing free text at scale);
- chunked large single-table masking via `run_mask_pipeline_chunked`;
- P3 auto-chunk routing memory gate (auto-chunked `run_pipeline` vs the
  same job forced full-frame; relative peak assertion + byte parity).

Every cell benchmarks at the `run_pipeline` public entrypoint (not the
strategy-handler layer PV-2 P5 already covers in
`test_throughput_budgets.py`), so each one exercises the P1
adapter-selection routing (`run_pipeline` -> `select_execution_adapter`
-> a concrete `ExecutionAdapter`) plus the full profile -> compile ->
adapter.run -> boundary-conversion stack.

"No performance number counts unless parity passes": every timing
assertion in this module sits beside a determinism check (same
config + seed => byte-identical output across two independent
`run_pipeline`/`run_mask_pipeline_chunked` calls) and a masking-sanity
check (masked columns differ from source, unmasked/passthrough columns
do not, row count is preserved). A parity failure fails the test
regardless of what the timing assertion would have said.

Two-phase measurement (see `_measure`): tracemalloc instrumentation
inflates wall-clock roughly 3-4x on these job-level runs, far more
than the PV-2 P5 handler-level cells (bare strategy calls on 10k rows)
tolerate, because a full `run_pipeline` call does far more small
Python-level allocation (lazy imports on first call, pandas/pyarrow
object columns, plan compile, relationship-graph build). Mixing the
traced run into the timed run would report an inflated, non-
representative wall-clock, so each cell runs the job three times: once
unmeasured (warmup, primes `run_pipeline`'s first-call lazy imports),
once for a clean `time.perf_counter()` reading, and once under
`tracemalloc` for the peak-RSS reading. The determinism check reuses
the warmup and timed runs' outputs rather than a fourth call.

Phase split: `ExecutionResult.timings` (one `StrategyTimingRecord` per
masked column, wired since PERF.BASE.1 -- see
`decoy_engine.instrumentation.timing`) already gives a real split
between "the execution adapter's strategy passes" (sum of
`elapsed_ms`) and "everything else" (profile_source, compile_plan,
relationship-graph build, adapter setup, output stitching), and
`ExecutionResult.boundary_conversion_ms` is a separately reported
field. This module reads both; it adds NO timing plumbing to src.
`run_pipeline` does not expose a profile/compile/materialize split any
finer than that -- that gap is noted here, not closed, per the P0
acceptance criteria ("no production behavior changes").

Calibration target (2026-07-02): devbox LXC, Linux 6.17, 4 vCPU,
8 GiB RAM, Python 3.11.2 (see `~/dev-rules` homelab notes for the box).
Budgets are ~2x the observed wall-clock and ~2x the observed
tracemalloc peak on that box; each cell's docstring documents the
observed number, the chosen budget, and the headroom factor, per the
PV-2 P5 discipline in `test_throughput_budgets.py`.
"""

from __future__ import annotations

import time
import tracemalloc
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionResult, run_pipeline
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

_ENGINE_VERSION = "p0-perf-gate"


def _validated_dump(cfg: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(cfg).model_dump()


@pytest.fixture
def adapter_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the concrete `ExecutionAdapter` P1 routing selects.

    `run_pipeline`'s default knobs never stamp `quality_metrics
    ["execution_adapter"]` (that block is opt-in, non-default-knob
    only -- see `_pipeline.py`), so a benchmark that wants to record
    "which adapter actually ran" for the default route has to observe
    the call the same way `test_run_pipeline_substrate.py`'s
    `select_spy` fixture does: wrap `select_execution_adapter` itself.
    """
    from decoy_engine.execution import _substrate

    calls: list[dict[str, Any]] = []
    real = _substrate.select_execution_adapter

    def spy(**kwargs: Any) -> Any:
        adapter = real(**kwargs)
        calls.append({**kwargs, "adapter": adapter})
        return adapter

    monkeypatch.setattr(_substrate, "select_execution_adapter", spy)
    return calls


def _measure(
    run_fn: Any,
) -> tuple[ExecutionResult, ExecutionResult, float, int]:
    """Run `run_fn` three times: warmup (uncounted), timed (clean wall
    clock), traced (tracemalloc peak). Returns (warmup_result,
    timed_result, elapsed_s, peak_bytes) so callers get a determinism
    pair (warmup vs timed output) for free alongside the timing.
    """
    warmup_result = run_fn()
    t0 = time.perf_counter()
    timed_result = run_fn()
    elapsed_s = time.perf_counter() - t0
    tracemalloc.start()
    run_fn()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return warmup_result, timed_result, elapsed_s, peak_bytes


def _phase_split(result: ExecutionResult, elapsed_s: float) -> tuple[float, float, float]:
    """(strategy_execution_s, boundary_conversion_s, other_s) from the
    fields `run_pipeline` already exposes; see module docstring."""
    strategy_s = sum(t.elapsed_ms for t in result.timings) / 1000.0
    conversion_s = result.boundary_conversion_ms / 1000.0
    return strategy_s, conversion_s, elapsed_s - strategy_s - conversion_s


def _write_parquet_source(tmp_path: Any, df: pd.DataFrame, filename: str) -> str:
    path = tmp_path / filename
    df.to_parquet(path)
    return str(path)


def _medium_tier() -> pd.DataFrame:
    from tests.perf_fixtures.loaders import load_tier

    try:
        return load_tier("medium")
    except FileNotFoundError:
        pytest.skip("perf_fixtures medium tier not on disk; see tests/perf_fixtures/README.md")


# --------------------------------------------------------------------------
# Scalar no-FK mask job
# --------------------------------------------------------------------------


def _scalar_config(source_path: str) -> dict[str, Any]:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "ssn", "strategy": "hash", "namespace": "ssn_ns"},
                        {
                            "name": "zip",
                            "strategy": "truncate",
                            "provider_config": {"length": 3},
                        },
                        {"name": "status", "strategy": "redact"},
                        {"name": "customer_id", "strategy": "passthrough"},
                        {
                            "name": "score",
                            "strategy": "bucketize",
                            "provider_config": {"width": 50},
                        },
                    ],
                },
            ],
            "targets": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
        }
    )


@pytest.mark.benchmark
def test_scalar_no_fk_mask_job_gate(tmp_path: Any, adapter_spy: list[dict[str, Any]]) -> None:
    """P0: scalar no-FK mask job at 100k rows (hash/truncate/redact/
    passthrough/bucketize mix), through `run_pipeline`.

    P3 note: this cell is the FULL-FRAME baseline, and this shape at
    100k rows is exactly what P3's auto-chunk routing now takes, so the
    kill switch (`auto_chunk=False`) pins the measured path; the
    auto-chunked counterpart is measured by `test_auto_chunk_memory_gate`
    below.

    Devbox calibration (2026-07-02): observed wall-clock 1.28s, peak
    tracemalloc 20.3 MiB (of which strategy execution ~1.25s, boundary
    conversion ~0.011s, everything else -- profile/compile/graph-build
    -- ~0.02s: the strategy passes dominate, as expected for
    cheap-but-100k-row string/hash work). Budget below is ~2x
    observed on both axes (2.6s / 48 MiB), matching the PV-2 P5
    headroom convention.
    """
    full = _medium_tier()
    df = full[["ssn", "zip", "status", "customer_id", "score"]].copy()
    source_path = _write_parquet_source(tmp_path, df, "scalar_src.parquet")
    cfg = _scalar_config(source_path)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def run() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False)

    warmup, timed, elapsed_s, peak_bytes = _measure(run)

    # Parity gate: determinism first, timing second.
    out_a = warmup.outputs["accounts"]
    out_b = timed.outputs["accounts"]
    assert out_a.equals(out_b), "scalar mask job is not deterministic across two calls"
    assert len(out_b) == len(df)
    assert out_b.column("ssn").to_pylist() != df["ssn"].tolist()
    assert out_b.column("zip").to_pylist() != df["zip"].tolist()
    assert set(out_b.column("status").to_pylist()) == {"REDACTED"}
    assert out_b.column("customer_id").to_pylist() == df["customer_id"].tolist()  # passthrough

    assert isinstance(adapter_spy[-1]["adapter"], PandasExecutionAdapter), (
        "P1 routing should select the pandas adapter on run_pipeline's default route"
    )

    strategy_s, conversion_s, other_s = _phase_split(timed, elapsed_s)
    assert elapsed_s < 2.6, (
        f"scalar mask job (100k rows) took {elapsed_s:.2f}s; budget 2.6s "
        f"(strategy={strategy_s:.2f}s conversion={conversion_s:.2f}s other={other_s:.2f}s)"
    )
    assert peak_bytes < 48 * 1024 * 1024, (
        f"scalar mask job peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 48 MiB."
    )


# --------------------------------------------------------------------------
# Faker-heavy wide table
# --------------------------------------------------------------------------

_FAKER_COLUMNS: tuple[tuple[str, str, str, int], ...] = (
    ("full_name", "person_full_name", "fh_full_name_ns", 2000),
    ("email", "person_email", "fh_email_ns", 2000),
    ("phone", "person_phone", "fh_phone_ns", 2000),
    ("street_address", "address_street", "fh_addr_ns", 2000),
    ("city", "address_city", "fh_city_ns", 1000),
    ("state", "address_state", "fh_state_ns", 50),
    ("zip", "address_zip", "fh_zip_ns", 2000),
    ("claim_id", "synthetic_member_id", "fh_claim_ns", 2000),
)


def _faker_wide_config(source_path: str) -> dict[str, Any]:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "name": name,
                            "strategy": "faker",
                            "provider": provider,
                            "deterministic": True,
                            "namespace": namespace,
                            "cardinality_mode": "reuse",
                            "provider_config": {"pool_size": pool_size},
                        }
                        for name, provider, namespace, pool_size in _FAKER_COLUMNS
                    ],
                },
            ],
            "targets": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
        }
    )


@pytest.mark.benchmark
def test_faker_heavy_wide_table_gate(tmp_path: Any, adapter_spy: list[dict[str, Any]]) -> None:
    """P0: deterministic faker across 8 columns, 50k rows, through
    `run_pipeline`. All 8 columns use the deterministic value-keyed
    path (`deterministic: true` + `namespace` + explicit
    `provider_config.pool_size`, `cardinality_mode: reuse`) -- the same
    admission rule `check_chunked_compatibility` requires, so this
    shape's config would also be chunk-eligible (not exercised here;
    see the chunked cell below for a different column mix).

    Devbox calibration (2026-07-02): observed wall-clock 5.17s, peak
    tracemalloc 7.9 MiB (strategy execution dominates at ~5.15s; pool
    build + per-row `derive_index` across 8 columns is the cost, not
    the boundary conversion or plan compile). Budget ~2x observed:
    11.0s / 20 MiB.
    """
    full = _medium_tier()
    cols = [c[0] for c in _FAKER_COLUMNS]
    df = full[cols].head(50_000).copy()
    source_path = _write_parquet_source(tmp_path, df, "faker_src.parquet")
    cfg = _faker_wide_config(source_path)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def run() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)

    warmup, timed, elapsed_s, peak_bytes = _measure(run)

    out_a = warmup.outputs["accounts"]
    out_b = timed.outputs["accounts"]
    assert out_a.equals(out_b), "faker-heavy job is not deterministic across two calls"
    assert len(out_b) == len(df)
    for name in cols:
        assert out_b.column(name).to_pylist() != df[name].tolist(), (
            f"faker column {name!r} did not change"
        )

    assert isinstance(adapter_spy[-1]["adapter"], PandasExecutionAdapter)

    strategy_s, conversion_s, other_s = _phase_split(timed, elapsed_s)
    assert elapsed_s < 11.0, (
        f"faker-heavy wide job (50k rows x 8 cols) took {elapsed_s:.2f}s; budget 11.0s "
        f"(strategy={strategy_s:.2f}s conversion={conversion_s:.2f}s other={other_s:.2f}s)"
    )
    assert peak_bytes < 20 * 1024 * 1024, (
        f"faker-heavy job peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 20 MiB."
    )


# --------------------------------------------------------------------------
# FPE-heavy table
# --------------------------------------------------------------------------

_FPE_COLUMNS: tuple[str, ...] = ("ssn", "zip", "phone", "claim_id", "invoice_id")


def _fpe_heavy_config(source_path: str) -> dict[str, Any]:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {
                            "name": name,
                            "strategy": "fpe",
                            "namespace": f"fpe_{name}_ns",
                            "provider_config": {"charset": "digits"},
                        }
                        for name in _FPE_COLUMNS
                    ],
                },
            ],
            "targets": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
        }
    )


@pytest.mark.benchmark
def test_fpe_heavy_table_gate(tmp_path: Any, adapter_spy: list[dict[str, Any]]) -> None:
    """P0: fpe across 5 digit-charset columns, 20k rows, through
    `run_pipeline`. `claim_id`/`invoice_id` are cast to string first
    (fpe operates on the digit charset regardless of source dtype, but
    the fixture stores them as int64; casting keeps the config's
    `charset: "digits"` honest about what it is permuting).

    Devbox calibration (2026-07-02): observed wall-clock 3.52s, peak
    tracemalloc 4.3 MiB (strategy execution ~3.50s -- the Feistel+HMAC
    permutation per value is the documented Python-hot-path cost per
    the sprint plan's "GIL-bound loops" limitation). Budget ~2x
    observed: 7.5s / 12 MiB.
    """
    full = _medium_tier()
    df = full[list(_FPE_COLUMNS)].head(20_000).copy()
    df["claim_id"] = df["claim_id"].astype(str)
    df["invoice_id"] = df["invoice_id"].astype(str)
    source_path = _write_parquet_source(tmp_path, df, "fpe_src.parquet")
    cfg = _fpe_heavy_config(source_path)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def run() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)

    warmup, timed, elapsed_s, peak_bytes = _measure(run)

    out_a = warmup.outputs["accounts"]
    out_b = timed.outputs["accounts"]
    assert out_a.equals(out_b), "fpe-heavy job is not deterministic across two calls"
    assert len(out_b) == len(df)
    for name in _FPE_COLUMNS:
        assert out_b.column(name).to_pylist() != df[name].tolist(), (
            f"fpe column {name!r} did not change"
        )

    assert isinstance(adapter_spy[-1]["adapter"], PandasExecutionAdapter)

    strategy_s, conversion_s, other_s = _phase_split(timed, elapsed_s)
    assert elapsed_s < 7.5, (
        f"fpe-heavy job (20k rows x 5 cols) took {elapsed_s:.2f}s; budget 7.5s "
        f"(strategy={strategy_s:.2f}s conversion={conversion_s:.2f}s other={other_s:.2f}s)"
    )
    assert peak_bytes < 12 * 1024 * 1024, (
        f"fpe-heavy job peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 12 MiB."
    )


# --------------------------------------------------------------------------
# Text-redaction-heavy table
# --------------------------------------------------------------------------

# PII-bearing sentences (reused pattern from PV-2 P5's text_redact cell in
# test_throughput_budgets.py): the medium perf-fixture's `notes` column is
# lorem-style Faker text with no embedded PII, which would make the
# masking-sanity check ("output differs from input") vacuous for
# text_redact's span-detector strategy. These samples carry a detectable
# span in 3 of 4 rows so the redaction is real and checkable.
_TEXT_SAMPLES: tuple[str, ...] = (
    "Contact alice@example.com about appointment SSN 123-45-6789.",
    "Patient phone (212) 555-1234. PAN 4111 1111 1111 1111 valid.",
    "Patient ICD-10 J45.40 + NPI 1234567893. No further notes.",
    "Lorem ipsum dolor sit amet, no PII in this row at all.",
)


def _text_redact_config(source_path: str) -> dict[str, Any]:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [{"name": "notes", "strategy": "text_redact"}],
                },
            ],
            "targets": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
        }
    )


@pytest.mark.benchmark
def test_text_redaction_heavy_table_gate(tmp_path: Any, adapter_spy: list[dict[str, Any]]) -> None:
    """P0: text_redact over 50k rows of PII-bearing free text, through
    `run_pipeline`.

    Devbox calibration (2026-07-02): observed wall-clock 1.41s, peak
    tracemalloc 10.7 MiB. Budget ~2x observed: 3.0s / 24 MiB.
    """
    n = 50_000
    df = pd.DataFrame({"notes": [_TEXT_SAMPLES[i % len(_TEXT_SAMPLES)] for i in range(n)]})
    source_path = _write_parquet_source(tmp_path, df, "text_src.parquet")
    cfg = _text_redact_config(source_path)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def run() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)

    warmup, timed, elapsed_s, peak_bytes = _measure(run)

    out_a = warmup.outputs["accounts"]
    out_b = timed.outputs["accounts"]
    assert out_a.equals(out_b), "text-redact-heavy job is not deterministic across two calls"
    notes_out = out_b.column("notes").to_pylist()
    assert len(notes_out) == n
    assert notes_out != df["notes"].tolist()
    # The email/SSN spans are the detectable PII in this sample set; a real
    # redaction removes them from every row that had one, not just "some".
    assert not any("@example.com" in v for v in notes_out)
    assert not any("123-45-6789" in v for v in notes_out)

    assert isinstance(adapter_spy[-1]["adapter"], PandasExecutionAdapter)

    strategy_s, conversion_s, other_s = _phase_split(timed, elapsed_s)
    assert elapsed_s < 3.0, (
        f"text-redact-heavy job (50k rows) took {elapsed_s:.2f}s; budget 3.0s "
        f"(strategy={strategy_s:.2f}s conversion={conversion_s:.2f}s other={other_s:.2f}s)"
    )
    assert peak_bytes < 24 * 1024 * 1024, (
        f"text-redact-heavy job peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 24 MiB."
    )


# --------------------------------------------------------------------------
# Chunked large single-table masking
# --------------------------------------------------------------------------


@pytest.mark.benchmark
def test_chunked_large_single_table_gate(tmp_path: Any) -> None:
    """P0: `run_mask_pipeline_chunked` over the same 100k-row / 5-column
    scalar-mix config as the scalar cell above, streamed in 10k-row
    chunks (10 chunks total).

    Parity is two-layered here, both non-negotiable ahead of the
    timing assertion: (1) the chunked contract itself -- concatenated
    chunked output must equal a full-frame `run_pipeline` call on the
    identical config + sources byte-for-byte (the module docstring's
    "value-keyed" guarantee `check_chunked_compatibility` enforces at
    compile time); (2) determinism -- two independent chunked runs
    must also be byte-identical.

    Adapter note: `run_mask_pipeline_chunked` does NOT go through P1's
    `select_execution_adapter` -- it defaults its own `adapter`
    parameter straight to `PandasExecutionAdapter()`
    (`execution/_chunked.py:292-293`), a separate code path from
    `run_pipeline`'s routing. The full-frame baseline this cell diffs
    against DOES route through P1 (same as the scalar cell above); the
    chunked path itself stays exactly as it shipped in WS4, which this
    sprint does not change.

    Phase-split note: `run_mask_pipeline_chunked` yields bare
    `pa.Table` chunks; per-chunk `ExecutionResult`s (timings, boundary
    conversion figures) are available only via its opt-in
    `chunk_result_sink` parameter (added by the P3 remediation for the
    routed path). This cell keeps recording only total elapsed + peak
    RSS, per the P0 scope note.

    Devbox calibration (2026-07-02): observed wall-clock 1.27s, peak
    tracemalloc 2.1 MiB -- lower peak than the equivalent full-frame
    scalar cell (20.3 MiB), consistent with chunked execution's
    bounded-batch memory story. Budget ~2x observed: 2.6s / 8 MiB.
    """
    full = _medium_tier()
    df = full[["ssn", "zip", "status", "customer_id", "score"]].copy()
    source_path = _write_parquet_source(tmp_path, df, "chunk_src.parquet")
    cfg = _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
            "tables": [
                {
                    "name": "accounts",
                    "columns": [
                        {"name": "ssn", "strategy": "hash", "namespace": "ck_ssn_ns"},
                        {
                            "name": "zip",
                            "strategy": "truncate",
                            "provider_config": {"length": 3},
                        },
                        {"name": "status", "strategy": "redact"},
                        {"name": "customer_id", "strategy": "passthrough"},
                        {
                            "name": "score",
                            "strategy": "bucketize",
                            "provider_config": {"width": 50},
                        },
                    ],
                },
            ],
            "targets": {
                "accounts": {"type": "file", "format": "parquet", "path": source_path},
            },
        }
    )
    table = pa.Table.from_pandas(df, preserve_index=False)

    def _chunks(chunk_size: int = 10_000):
        for i in range(0, table.num_rows, chunk_size):
            yield table.slice(i, chunk_size)

    def run_chunked() -> pa.Table:
        return pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )

    # auto_chunk=False (P3): this call is the FULL-FRAME oracle; without
    # the kill switch the P3 auto-routing would chunk this 100k-row shape
    # too and the diff would compare chunked against chunked.
    full_frame = run_pipeline(
        cfg, sources={"accounts": table}, engine_version=_ENGINE_VERSION, auto_chunk=False
    ).outputs["accounts"]

    run_chunked()  # warmup: primes lazy imports, uncounted
    t0 = time.perf_counter()
    chunked_a = run_chunked()
    elapsed_s = time.perf_counter() - t0
    tracemalloc.start()
    chunked_b = run_chunked()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert chunked_a.to_pylist() == full_frame.to_pylist(), (
        "chunked output diverged from the full-frame run_pipeline oracle"
    )
    assert chunked_a.to_pylist() == chunked_b.to_pylist(), (
        "chunked run is not deterministic across two independent calls"
    )
    assert len(chunked_a) == len(df)
    assert chunked_a.column("ssn").to_pylist() != df["ssn"].tolist()
    assert chunked_a.column("customer_id").to_pylist() == df["customer_id"].tolist()

    assert elapsed_s < 2.6, (
        f"chunked mask job (100k rows, 10k-row chunks) took {elapsed_s:.2f}s; budget 2.6s."
    )
    assert peak_bytes < 8 * 1024 * 1024, (
        f"chunked mask job peak RSS {peak_bytes / (1024 * 1024):.1f} MiB; budget 8 MiB."
    )


# --------------------------------------------------------------------------
# P3: auto-chunk routing memory gate
# --------------------------------------------------------------------------


@pytest.mark.benchmark
def test_auto_chunk_memory_gate(tmp_path: Any) -> None:
    """P3: the AUTO-CHUNKED `run_pipeline` route peaks lower than the same
    job forced full-frame, with byte-identical output.

    The job is the P0 scalar 100k-row / 5-column shape under ALL-DEFAULT
    knobs, which sits exactly at the default auto-chunk threshold
    (100k rows) and therefore routes chunked (2 chunks at the default
    50k-row chunk size); `auto_chunk=False` is the full-frame baseline.
    Parity comes first, per the P0 discipline: the two routes must be
    byte-identical (values and schema) before any memory number counts.

    Both measurements run through the same `run_pipeline` entrypoint, so
    the shared stages (profile sampling, plan compile) appear in both
    peaks and the delta isolates the mask-execution working set; the
    assertion is therefore RELATIVE (auto < 75% of full-frame) rather
    than an absolute budget. Devbox calibration (2026-07-02): full-frame
    20.35 MiB vs auto-chunked 10.20 MiB traced peak (ratio 0.50 -- the
    expected ~2x for the 2-chunk split at this exact-threshold size;
    larger jobs chunk more and win more). The 75% bound keeps headroom
    above the observed ratio while still failing if the route stops
    bounding the working set.
    """
    full = _medium_tier()
    df = full[["ssn", "zip", "status", "customer_id", "score"]].copy()
    source_path = _write_parquet_source(tmp_path, df, "auto_chunk_src.parquet")
    cfg = _scalar_config(source_path)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}

    def run_auto() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)

    def run_full() -> ExecutionResult:
        return run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION, auto_chunk=False)

    def traced_peak(run_fn: Any) -> tuple[ExecutionResult, int]:
        tracemalloc.start()
        result = run_fn()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return result, peak

    run_auto()  # warmup: primes lazy imports for both routes, uncounted
    auto_result, auto_peak = traced_peak(run_auto)
    full_result, full_peak = traced_peak(run_full)

    # Parity gate before any memory number counts.
    assert auto_result.quality_metrics.get("auto_chunk", {}).get("mode") == "chunked", (
        "the default run did not auto-chunk; this gate must measure the chunked route"
    )
    out_auto = auto_result.outputs["accounts"]
    out_full = full_result.outputs["accounts"]
    assert [str(f.type) for f in out_auto.schema] == [str(f.type) for f in out_full.schema]
    assert out_auto.equals(out_full), "auto-chunked output diverged from full-frame"
    assert len(out_auto) == len(df)
    assert out_auto.column("ssn").to_pylist() != df["ssn"].tolist()
    assert out_auto.column("customer_id").to_pylist() == df["customer_id"].tolist()

    assert auto_peak < full_peak * 0.75, (
        f"auto-chunked peak {auto_peak / (1024 * 1024):.1f} MiB is not below 75% of "
        f"full-frame peak {full_peak / (1024 * 1024):.1f} MiB; the memory win is the "
        "whole point of the auto-chunk route"
    )
