"""GP1 generation-throughput probe: the tiers x types sweep/compare driver.

Runs the frozen `bench_worker_gen.py` (this directory) as a fresh
subprocess per arm per rep, over every (faker_type, n_rows) cell in the
sweep, and reports the pooled-vs-per-row crossover: the smallest row
count at which pooling actually wins, confirmed against the next tier so
a single noisy rep can't manufacture a false crossover. See this
directory's README.md for how to read the output and what the sweep is
for; see `bench_worker_gen.py`'s module docstring for the threshold-
override mechanism this driver relies on.

Reuses the D9 harness's (`scripts/bench-unified-slice/bench_compare.py`)
statistical spine: paired per-rep arm alternation by rep parity, warmups
discarded, a seeded bootstrap CI on the paired-ratio median, and the
race-free `os.wait4`-based child lifecycle for peak-RSS. That module is
not imported here (it is frozen to its own D9 workload and this driver
has an independent CLI contract), so the shared logic is restated,
adapted for GP1's per-(type, n) cells in place of D9's per-n tiers.

Building this driver is not the same as RUNNING the real offline sweep:
this module's own tests, and any small custom invocation, are `run_ok`
at most and print `SMOKE COMPLETE`. Only the exact default sweep shape
(every default tier, every allowlisted type, cert-minimum reps/warmup/
bootstrap) is eligible to print a crossover recommendation, and that
sweep is a deliberate offline invocation on a quiet host -- never a CI
step (README.md).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from decoy_engine.generation import _faker_pool

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent.parent
WORKER_PATH = HERE / "bench_worker_gen.py"
# Self-locating, same interpreter that runs this driver -- a worktree
# checkout has no fixed venv path, so hardcoding one would break the
# first worker spawn on a fresh host.
VENV_PY = Path(sys.executable)
_WORKER_ENV_BASE = {**os.environ, "PYTHONPATH": str(ENGINE / "src")}

HARNESS_VERSION = "1.0.0"
# Fixed default seed: the bootstrap CI must be reproducible run-to-run,
# never drawn from unseeded randomness.
_DEFAULT_SEED = 20260917
_DEFAULT_TIERS: tuple[int, ...] = (
    1_000,
    5_000,
    10_000,
    25_000,
    50_000,
    100_000,
    250_000,
    1_000_000,
)
# The closed allowlist IS `_faker_pool.POOL_ELIGIBLE_FAKER_TYPES` (not a
# restated copy): a non-allowlisted type is never pool-eligible in
# production, so there is no second, harness-local notion of "allowed"
# to drift from the real one.
_ALL_TYPES: tuple[str, ...] = tuple(sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES))
# The sweep forces the pooled arm to pool at every tier (module docstring
# of bench_worker_gen.py); both arms get this set so a workload fingerprint
# comparison between arms is meaningful (the off arm reports it too, even
# though its own `pooled: false` opt-out ignores the value).
_SWEEP_THRESHOLD_OVERRIDE = 1

_CERT_MIN_REPS = 20
_CERT_MIN_WARMUP = 3
_CERT_MIN_BOOTSTRAP = 10_000
_DEFAULT_MAX_RSS_RATIO = 1.25
_DEFAULT_MAX_RSS_DELTA_KB = 51_200  # 50 MB


def is_full_sweep_shape(
    tiers: Sequence[int], types: Sequence[str], *, reps: int, warmup: int, bootstrap: int
) -> bool:
    """The only shape eligible to print a crossover recommendation: every
    default tier, every allowlisted type, at cert-minimum sample sizes.
    A tiny or custom invocation can still be `run_ok`, just never eligible
    for a recommendation -- mirrors the D9 harness's cert-shape gate."""
    return (
        tuple(tiers) == _DEFAULT_TIERS
        and tuple(sorted(types)) == _ALL_TYPES
        and reps >= _CERT_MIN_REPS
        and warmup >= _CERT_MIN_WARMUP
        and bootstrap >= _CERT_MIN_BOOTSTRAP
    )


class FailClosedError(RuntimeError):
    """Any data-validity failure the plan's fail-closed list requires.
    Raised via real `if: raise` throughout this module, never `assert`
    (stripped by `python -O`). Catching this at the top level is what
    flips `run_ok` to False and the exit code non-zero."""


# ---------------------------------------------------------------------------
# Pure statistics -- no subprocess, no I/O, fully unit-testable.
# ---------------------------------------------------------------------------


def _inclusive_percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation inclusive percentile; degrades gracefully at
    n=1, unlike `statistics.quantiles`'s n>=2 requirement -- `--bootstrap 1`
    is a valid (if statistically thin) input per the CLI floor."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def inclusive_p95(values: Sequence[float]) -> float:
    """The inclusive-method 95th percentile. Requires len(values) >= 2,
    guaranteed by the `--reps >= 2` CLI floor."""
    ordered = sorted(values)
    return statistics.quantiles(ordered, n=100, method="inclusive")[94]


def bootstrap_ci(ratios: Sequence[float], *, seed: int, n_bootstrap: int) -> tuple[float, float]:
    """Seeded bootstrap CI on the paired-ratio median: resample the rep
    INDEX set with replacement `n_bootstrap` times, recompute the median
    ratio per resample, take the inclusive [2.5, 97.5] percentile
    interval. A LOCAL `random.Random(seed)`, never the shared/global
    `random` module, so this is reproducible and never interferes with
    other seeded state."""
    rng = random.Random(seed)
    n = len(ratios)
    medians = []
    for _ in range(n_bootstrap):
        sample = [ratios[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    return _inclusive_percentile(medians, 2.5), _inclusive_percentile(medians, 97.5)


@dataclass
class CrossoverResult:
    """`tier` is the smallest CONFIRMED crossover row count (None if the
    sweep never confirms one). `boundary_fallback` is True when only the
    LAST measured tier shows pooling winning, with no further tier to
    confirm it -- the plan's "crossover at or beyond sweep boundary" case,
    reported distinctly from "pooling never won in this sweep" (both
    `tier is None`, distinguished by this flag)."""

    tier: int | None
    boundary_fallback: bool


def find_crossover_tier(tiers_sorted: Sequence[int], ci_highs: Sequence[float]) -> CrossoverResult:
    """The smallest tier whose paired-ratio CI upper bound < 1.0 AND stays
    < 1.0 for the next measured tier (guards against a single noisy win).
    `tiers_sorted` and `ci_highs` are parallel, ascending by row count.

    A tier whose own CI upper bound wins but whose successor does not is
    an unconfirmed single win: the scan continues looking for a later,
    confirmed tier rather than stopping there. If the sweep reaches its
    LAST tier with the win still unconfirmed (no successor exists to
    confirm it), that is reported as `boundary_fallback=True` rather than
    a confirmed crossover.
    """
    n = len(tiers_sorted)
    for i in range(n):
        if ci_highs[i] >= 1.0:
            continue
        if i < n - 1:
            if ci_highs[i + 1] < 1.0:
                return CrossoverResult(tier=tiers_sorted[i], boundary_fallback=False)
            continue  # unconfirmed single-tier win; keep scanning
        return CrossoverResult(tier=None, boundary_fallback=True)
    return CrossoverResult(tier=None, boundary_fallback=False)


def _round_conservative(raw_tier: int) -> int:
    """A round, conservative number at or above `raw_tier`: 10% headroom,
    rounded up to the nearest 1,000 rows. This is a reporting convenience
    for a human picking the next `N_THRESHOLD` value, not a statistical
    claim -- the raw max is reported alongside it precisely so this
    rounding is never the only number on record."""
    padded = raw_tier * 1.1
    return math.ceil(padded / 1_000.0) * 1_000


@dataclass
class RssGateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def apply_rss_gate(
    cells: Sequence[dict[str, Any]],
    *,
    recommended_tier: int,
    max_rss_ratio: float,
    max_rss_delta_kb: int,
) -> RssGateResult:
    """A recommendation is only safe to emit if, at and above the
    recommended tier, pooling's peak-RSS cost stays within budget on
    EVERY measured (type, n) cell in that range -- not just the type that
    produced the max crossover. `cells` are per-(type, n_rows) records
    carrying `n_rows`, `rss_ratio`, and `rss_delta_kb` (as `_run_cell`
    produces below)."""
    reasons: list[str] = []
    for cell in cells:
        if cell["n_rows"] < recommended_tier:
            continue
        if cell["rss_ratio"] > max_rss_ratio:
            reasons.append(
                f"{cell['faker_type']}@{cell['n_rows']}: rss_ratio {cell['rss_ratio']:.3f} "
                f"> max_rss_ratio {max_rss_ratio}"
            )
        if cell["rss_delta_kb"] > max_rss_delta_kb:
            reasons.append(
                f"{cell['faker_type']}@{cell['n_rows']}: rss_delta_kb {cell['rss_delta_kb']} "
                f"> max_rss_delta_kb {max_rss_delta_kb}"
            )
    return RssGateResult(ok=not reasons, reasons=reasons)


def build_recommendation(
    per_type_crossover: dict[str, CrossoverResult],
    cells: Sequence[dict[str, Any]],
    *,
    max_rss_ratio: float,
    max_rss_delta_kb: int,
) -> dict[str, Any]:
    """Aggregate recommendation = MAX of every type's CONFIRMED crossover
    tier. Withheld (with a reason) if any measured type never confirmed a
    crossover within the sweep, or if the RSS gate fails at and above the
    resulting tier -- either way the reason is on record, never a silent
    omission."""
    unconfirmed = sorted(t for t, c in per_type_crossover.items() if c.tier is None)
    if unconfirmed:
        return {
            "recommended_threshold_raw": None,
            "recommended_threshold_rounded": None,
            "withheld_reason": f"no confirmed crossover for type(s): {', '.join(unconfirmed)}",
        }
    raw_max = max(c.tier for c in per_type_crossover.values() if c.tier is not None)
    rss = apply_rss_gate(
        cells,
        recommended_tier=raw_max,
        max_rss_ratio=max_rss_ratio,
        max_rss_delta_kb=max_rss_delta_kb,
    )
    if not rss.ok:
        return {
            "recommended_threshold_raw": raw_max,
            "recommended_threshold_rounded": None,
            "withheld_reason": f"RSS gate failed at/above {raw_max}: {'; '.join(rss.reasons)}",
        }
    return {
        "recommended_threshold_raw": raw_max,
        "recommended_threshold_rounded": _round_conservative(raw_max),
        "withheld_reason": None,
    }


# ---------------------------------------------------------------------------
# Strict worker-record validation -- pure, no subprocess.
# ---------------------------------------------------------------------------

_BENCH_JSON_RE = re.compile(r"^BENCH_JSON (.+)$")
_NUMERIC_FIELDS = ("wall_s", "rows_per_s")


def _reject_nonfinite_constant(name: str) -> NoReturn:
    raise FailClosedError(f"worker record JSON contains disallowed constant {name!r}")


def validate_worker_record(
    stdout: str,
    *,
    expected_n_rows: int,
    expected_faker_type: str,
    expected_flag_on: bool,
) -> dict[str, Any]:
    """Strict typed validation of one worker invocation's FULL stdout
    buffer (never a partial one -- see `run_child_process`'s drain-
    completeness contract). Raises `FailClosedError` on the first
    violation; every check is a real `if: raise`, never `assert`."""
    marker_lines = [
        ln for ln in stdout.splitlines() if ln == "BENCH_JSON" or ln.startswith("BENCH_JSON ")
    ]
    if len(marker_lines) != 1:
        raise FailClosedError(f"expected exactly one BENCH_JSON line, found {len(marker_lines)}")
    payload_match = _BENCH_JSON_RE.match(marker_lines[0])
    if payload_match is None:
        raise FailClosedError("the BENCH_JSON line carries no payload")

    try:
        record = json.loads(payload_match.group(1), parse_constant=_reject_nonfinite_constant)
    except json.JSONDecodeError as exc:
        raise FailClosedError(f"BENCH_JSON line is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise FailClosedError(f"BENCH_JSON line did not parse to an object: {record!r}")

    for field_name in _NUMERIC_FIELDS:
        if field_name not in record:
            raise FailClosedError(f"worker record missing numeric field {field_name!r}")
        value = record[field_name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FailClosedError(f"worker record field {field_name!r} is not numeric: {value!r}")
        if not math.isfinite(value):
            raise FailClosedError(f"worker record field {field_name!r} is not finite: {value!r}")
    if record["wall_s"] <= 0:
        raise FailClosedError(f"worker record wall_s must be > 0, got {record['wall_s']!r}")

    for int_field in ("n_rows", "out_rows"):
        if int_field not in record:
            raise FailClosedError(f"worker record missing field {int_field!r}")
        value = record[int_field]
        # `type(x) is int` (not `isinstance`): isinstance(True, int) is True
        # in Python, which would silently admit a bool as a row count.
        if type(value) is not int:
            raise FailClosedError(f"worker record field {int_field!r} must be int, got {value!r}")
        if value != expected_n_rows:
            raise FailClosedError(
                f"worker record field {int_field!r}={value!r} != requested n_rows={expected_n_rows!r}"
            )

    if record.get("faker_type") != expected_faker_type:
        raise FailClosedError(
            f"worker record faker_type={record.get('faker_type')!r} != "
            f"expected {expected_faker_type!r}"
        )

    expected_arm = "pooled" if expected_flag_on else "per_row"
    if record.get("arm") != expected_arm:
        raise FailClosedError(
            f"worker record arm={record.get('arm')!r} != expected {expected_arm!r}"
        )

    activated = record.get("pooled_activated")
    if expected_flag_on:
        if activated is not True:
            raise FailClosedError(
                f"on-arm pooled_activated is not True (identity check): {activated!r}"
            )
    else:
        if activated is not False:
            raise FailClosedError(
                f"off-arm pooled_activated is not False (identity check): {activated!r}"
            )

    if record.get("bench_threshold_override") != _SWEEP_THRESHOLD_OVERRIDE:
        raise FailClosedError(
            f"worker record bench_threshold_override={record.get('bench_threshold_override')!r} "
            f"!= sweep override {_SWEEP_THRESHOLD_OVERRIDE!r}"
        )

    if "workload_fingerprint" not in record:
        raise FailClosedError("worker record missing workload_fingerprint")
    fingerprint = record["workload_fingerprint"]
    if not isinstance(fingerprint, dict):
        raise FailClosedError(
            f"worker record workload_fingerprint is not an object: {fingerprint!r}"
        )
    fp_n_rows = fingerprint.get("n_rows")
    if type(fp_n_rows) is not int or fp_n_rows != expected_n_rows:
        raise FailClosedError(
            f"worker record workload_fingerprint n_rows={fp_n_rows!r} != {expected_n_rows!r}"
        )
    if fingerprint.get("faker_type") != expected_faker_type:
        raise FailClosedError(
            f"worker record workload_fingerprint faker_type={fingerprint.get('faker_type')!r} "
            f"!= {expected_faker_type!r}"
        )

    return record


# ---------------------------------------------------------------------------
# Race-free child lifecycle (mirrors the D9 harness's own algorithm; see
# scripts/bench-unified-slice/bench_compare.py for the hazard-by-hazard
# rationale this restates for GP1's independent CLI contract).
# ---------------------------------------------------------------------------


@dataclass
class RawArmResult:
    stdout: str
    stderr: str
    returncode: int
    drained_ok: bool
    timed_out: bool
    ru_maxrss_kb: int | None


ArmRunner = Callable[[str, int, str, float], RawArmResult]


def _drain_stream(stream: Any, chunks: list[str], errors: list[BaseException]) -> None:
    try:
        chunks.append(stream.read())
    except BaseException as exc:  # must capture every shape; the thread has no other reporting path
        errors.append(exc)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_child_process(
    cmd: list[str], *, cwd: str, env: dict[str, str], timeout_s: float
) -> RawArmResult:
    """Spawn as a session/group leader so the SAVED pgid (== pid, by
    `start_new_session`) can group-kill any descendant that inherited a
    pipe; drain both pipes on daemon threads concurrently with an
    `os.wait4(WNOHANG)` poll (never `Popen.wait`/`poll`/`communicate`/
    `kill` before the reap -- each would steal the child or lose its
    rusage/status); assign `returncode` from the reaped status before
    touching `proc` any other way; then require BOTH readers to finish
    cleanly before the buffer is considered parseable."""
    proc = subprocess.Popen(  # noqa: S603 fixed benchmark worker invocation, no untrusted input
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    pgid = proc.pid

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    out_errors: list[BaseException] = []
    err_errors: list[BaseException] = []
    t_out = threading.Thread(
        target=_drain_stream, args=(proc.stdout, out_chunks, out_errors), daemon=True
    )
    t_err = threading.Thread(
        target=_drain_stream, args=(proc.stderr, err_chunks, err_errors), daemon=True
    )
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout_s
    timed_out = False
    status = 0
    rusage = None
    while True:
        reaped_pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
        if reaped_pid != 0:
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)

    if timed_out:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            # Benign: the child exited between the last WNOHANG poll and
            # this kill; the blocking wait4 below still reaps it cleanly.
            pass
        _, status, rusage = os.wait4(proc.pid, 0)

    proc.returncode = os.waitstatus_to_exitcode(status)

    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)
    drained_ok = not t_out.is_alive() and not t_err.is_alive() and not out_errors and not err_errors
    if not drained_ok:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

    return RawArmResult(
        stdout="".join(out_chunks),
        stderr="".join(err_chunks),
        returncode=proc.returncode,
        drained_ok=drained_ok,
        timed_out=timed_out,
        ru_maxrss_kb=rusage.ru_maxrss if rusage is not None else None,
    )


def _spawn_worker_arm(arm: str, n_rows: int, faker_type: str, timeout_s: float) -> RawArmResult:
    """The real `ArmRunner`: invokes the frozen `bench_worker_gen.py` with
    `GEN_POOL_BENCH_FLAG` set explicitly (never relying on the worker's
    own "on" default) and `GEN_POOL_BENCH_THRESHOLD` forced to the sweep
    override so the pooled arm pools at every tier (module docstring)."""
    env = {
        **_WORKER_ENV_BASE,
        "GEN_POOL_BENCH_FLAG": arm,
        "GEN_POOL_BENCH_THRESHOLD": str(_SWEEP_THRESHOLD_OVERRIDE),
    }
    cmd = [str(VENV_PY), str(WORKER_PATH), str(n_rows), faker_type]
    return run_child_process(cmd, cwd=str(ENGINE), env=env, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Orchestration: per-rep pairing/alternation, per-cell stats, sweep state.
# ---------------------------------------------------------------------------


def _arm_order_for_rep(rep_index: int) -> tuple[str, str]:
    """Alternates by rep parity: even rep off-then-on, odd rep on-then-off,
    so a short host load spike cannot land entirely inside one arm across
    a whole cell."""
    return ("off", "on") if rep_index % 2 == 0 else ("on", "off")


def _run_one_rep(
    faker_type: str,
    n_rows: int,
    rep_index: int,
    phase: str,
    arm_runner: ArmRunner,
    timeout_s: float,
    raw_reps: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    """Runs both arms of one rep in alternated order; returns (off_record,
    on_record, off_ru_maxrss_kb, on_ru_maxrss_kb)."""
    order = _arm_order_for_rep(rep_index)
    per_arm: dict[str, tuple[dict[str, Any], int]] = {}
    for arm in order:
        raw = arm_runner(arm, n_rows, faker_type, timeout_s)
        if raw.timed_out:
            raise FailClosedError(f"{faker_type}@{n_rows}: arm {arm!r} rep {rep_index} timed out")
        if not raw.drained_ok:
            raise FailClosedError(
                f"{faker_type}@{n_rows}: arm {arm!r} rep {rep_index} pipe drain did not complete cleanly"
            )
        if raw.returncode != 0:
            raise FailClosedError(
                f"{faker_type}@{n_rows}: arm {arm!r} rep {rep_index} exited {raw.returncode}: "
                f"{raw.stderr[-500:]}"
            )
        if not raw.ru_maxrss_kb or raw.ru_maxrss_kb <= 0:
            raise FailClosedError(
                f"{faker_type}@{n_rows}: arm {arm!r} rep {rep_index} missing/zero ru_maxrss evidence"
            )
        record = validate_worker_record(
            raw.stdout,
            expected_n_rows=n_rows,
            expected_faker_type=faker_type,
            expected_flag_on=(arm == "on"),
        )
        per_arm[arm] = (record, raw.ru_maxrss_kb)
        raw_reps.append(
            {
                "phase": phase,
                "pair_index": rep_index,
                "arm_order": list(order),
                "arm": arm,
                "wall_s": record["wall_s"],
                "rows_per_s": record["rows_per_s"],
                "ru_maxrss_kb": raw.ru_maxrss_kb,
            }
        )

    off_rec, off_rss = per_arm["off"]
    on_rec, on_rss = per_arm["on"]
    if off_rec["workload_fingerprint"] != on_rec["workload_fingerprint"]:
        raise FailClosedError(
            f"{faker_type}@{n_rows} rep {rep_index}: workload_fingerprint differs between off/on arms"
        )
    sys.stderr.write(
        f"  [{faker_type}@{n_rows}] {phase} rep {rep_index}: "
        f"off={off_rec['wall_s']:.3f}s(rss={off_rss}kb) on={on_rec['wall_s']:.3f}s(rss={on_rss}kb)\n"
    )
    sys.stderr.flush()
    return off_rec, on_rec, off_rss, on_rss


def _run_cell(
    faker_type: str,
    n_rows: int,
    *,
    warmup: int,
    reps: int,
    bootstrap: int,
    seed: int,
    timeout_s: float,
    arm_runner: ArmRunner,
) -> dict[str, Any]:
    """One (faker_type, n_rows) cell: paired ratio pooled_wall/per_row_wall,
    its bootstrap CI, both arms' median rows/s, and peak-RSS ratio/delta.
    No gate is applied here -- gating (RSS budget, crossover confirmation)
    happens once across the whole sweep, in `build_recommendation`."""
    raw_reps: list[dict[str, Any]] = []
    off_walls: list[float] = []
    on_walls: list[float] = []
    off_rps: list[float] = []
    on_rps: list[float] = []
    off_rss_max = 0
    on_rss_max = 0

    for w in range(warmup):
        _run_one_rep(faker_type, n_rows, w, "warmup", arm_runner, timeout_s, raw_reps)

    for r in range(reps):
        off_rec, on_rec, off_rss, on_rss = _run_one_rep(
            faker_type, n_rows, r, "timed", arm_runner, timeout_s, raw_reps
        )
        off_walls.append(off_rec["wall_s"])
        on_walls.append(on_rec["wall_s"])
        off_rps.append(off_rec["rows_per_s"])
        on_rps.append(on_rec["rows_per_s"])
        off_rss_max = max(off_rss_max, off_rss)
        on_rss_max = max(on_rss_max, on_rss)

    if len(off_walls) != reps or len(on_walls) != reps:
        raise FailClosedError(
            f"{faker_type}@{n_rows}: fewer than the requested {reps} reps completed"
        )
    if not off_rss_max or not on_rss_max:
        raise FailClosedError(f"{faker_type}@{n_rows}: missing peak-RSS aggregate evidence")

    ratios = [on_w / off_w for on_w, off_w in zip(on_walls, off_walls, strict=True)]
    ratio_median = statistics.median(ratios)
    ratio_p95 = inclusive_p95(ratios)
    ci_low, ci_high = bootstrap_ci(ratios, seed=seed, n_bootstrap=bootstrap)

    return {
        "faker_type": faker_type,
        "n_rows": n_rows,
        "warmups": warmup,
        "reps": reps,
        "off_wall_median": statistics.median(off_walls),
        "on_wall_median": statistics.median(on_walls),
        "off_rows_per_s_median": statistics.median(off_rps),
        "on_rows_per_s_median": statistics.median(on_rps),
        "ratio_median": ratio_median,
        "ratio_p95": ratio_p95,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "off_rss_max_kb": off_rss_max,
        "on_rss_max_kb": on_rss_max,
        "rss_ratio": on_rss_max / off_rss_max,
        "rss_delta_kb": on_rss_max - off_rss_max,
        "raw_reps": raw_reps,
    }


@dataclass
class RunConfig:
    tiers: list[int]
    types: list[str]
    reps: int
    warmup: int
    bootstrap: int
    seed: int
    timeout_s: float
    max_rss_ratio: float
    max_rss_delta_kb: int


def run_bench_compare(config: RunConfig, arm_runner: ArmRunner) -> dict[str, Any]:
    """Orchestrates every (type, tier) cell. Stops at the first fail-closed
    condition: a later cell's evidence cannot rescue an already-broken
    run, and running further multi-minute cells once the run is doomed
    wastes time without changing the outcome."""
    cells: dict[str, dict[str, Any]] = {}
    error: str | None = None
    outer_break = False
    for faker_type in config.types:
        if outer_break:
            break
        for n_rows in config.tiers:
            try:
                cell = _run_cell(
                    faker_type,
                    n_rows,
                    warmup=config.warmup,
                    reps=config.reps,
                    bootstrap=config.bootstrap,
                    seed=config.seed,
                    timeout_s=config.timeout_s,
                    arm_runner=arm_runner,
                )
            except FailClosedError as exc:
                error = str(exc)
                outer_break = True
                break
            cells[f"{faker_type}@{n_rows}"] = cell

    run_ok = error is None
    full_sweep = is_full_sweep_shape(
        config.tiers,
        config.types,
        reps=config.reps,
        warmup=config.warmup,
        bootstrap=config.bootstrap,
    )

    recommendation: dict[str, Any] | None = None
    if run_ok and full_sweep:
        tiers_sorted = sorted(config.tiers)
        per_type_crossover: dict[str, CrossoverResult] = {}
        for faker_type in config.types:
            ci_highs = [cells[f"{faker_type}@{n}"]["ci_high"] for n in tiers_sorted]
            per_type_crossover[faker_type] = find_crossover_tier(tiers_sorted, ci_highs)
        recommendation = build_recommendation(
            per_type_crossover,
            list(cells.values()),
            max_rss_ratio=config.max_rss_ratio,
            max_rss_delta_kb=config.max_rss_delta_kb,
        )
        recommendation["per_type_crossover"] = {
            t: {"tier": c.tier, "boundary_fallback": c.boundary_fallback}
            for t, c in per_type_crossover.items()
        }

    result: dict[str, Any] = {
        "run_ok": run_ok,
        "full_sweep": full_sweep,
        "harness_version": HARNESS_VERSION,
        "seed": config.seed,
        "max_rss_ratio": config.max_rss_ratio,
        "max_rss_delta_kb": config.max_rss_delta_kb,
        "cells": cells,
        "recommendation": recommendation,
    }
    if error is not None:
        result["error"] = error
    return result


def banner_for(result: dict[str, Any]) -> str:
    if not result["run_ok"]:
        return "GP1 FAILED"
    if result["full_sweep"]:
        return "GP1 SWEEP COMPLETE"
    return "SMOKE COMPLETE"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="GP1 generation-throughput probe: tiers x types sweep")
    p.add_argument(
        "--tiers",
        default=",".join(str(t) for t in _DEFAULT_TIERS),
        help="comma-separated row counts",
    )
    p.add_argument(
        "--types", default=",".join(_ALL_TYPES), help="comma-separated allowlisted faker types"
    )
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--out", default="results.json")
    p.add_argument("--bootstrap", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    p.add_argument("--timeout", type=float, default=600.0, help="per arm-run, seconds")
    p.add_argument("--max-rss-ratio", type=float, default=_DEFAULT_MAX_RSS_RATIO)
    p.add_argument("--max-rss-delta-kb", type=int, default=_DEFAULT_MAX_RSS_DELTA_KB)
    return p


def parse_and_validate_args(
    argv: Sequence[str] | None, parser: argparse.ArgumentParser | None = None
) -> argparse.Namespace:
    """CLI validation, run BEFORE any measurement or artifact write. Every
    violation goes through `parser.error` (stderr message, exit 2) so a
    bad invocation never reaches measurement or writes a stale artifact."""
    parser = parser or build_arg_parser()
    args = parser.parse_args(argv)

    try:
        tiers = [int(x) for x in args.tiers.split(",")]
    except ValueError:
        parser.error(f"--tiers must be a comma-separated list of ints, got {args.tiers!r}")
        tiers = []  # unreachable: parser.error always exits
    if not tiers:
        parser.error("--tiers must be non-empty")
    if any(t <= 0 for t in tiers):
        parser.error("--tiers values must all be positive")
    if len(tiers) != len(set(tiers)):
        parser.error("--tiers must not contain duplicates")
    args.tiers = tiers

    types = [t.strip() for t in args.types.split(",") if t.strip()]
    if not types:
        parser.error("--types must be non-empty")
    unknown = sorted(set(types) - _faker_pool.POOL_ELIGIBLE_FAKER_TYPES)
    if unknown:
        parser.error(
            f"--types contains non-allowlisted value(s) {unknown}; allowed: {sorted(_ALL_TYPES)}"
        )
    if len(types) != len(set(types)):
        parser.error("--types must not contain duplicates")
    args.types = types

    if args.reps < 2:
        parser.error("--reps must be >= 2 (p95 and the bootstrap CI need at least two paired reps)")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    if args.bootstrap < 1:
        parser.error("--bootstrap must be >= 1")
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        parser.error("--timeout must be a finite number > 0")
    if not (math.isfinite(args.max_rss_ratio) and args.max_rss_ratio > 0):
        parser.error("--max-rss-ratio must be a finite number > 0")
    if args.max_rss_delta_kb < 0:
        parser.error("--max-rss-delta-kb must be >= 0")

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_and_validate_args(argv)

    out_path = Path(args.out)
    # Stale-artifact guard: written BEFORE any measurement so a killed/
    # crashed run can never leave a stale artifact on disk.
    out_path.write_text(json.dumps({"run_ok": False, "status": "in_progress"}))

    config = RunConfig(
        tiers=args.tiers,
        types=args.types,
        reps=args.reps,
        warmup=args.warmup,
        bootstrap=args.bootstrap,
        seed=args.seed,
        timeout_s=args.timeout,
        max_rss_ratio=args.max_rss_ratio,
        max_rss_delta_kb=args.max_rss_delta_kb,
    )
    result = run_bench_compare(config, arm_runner=_spawn_worker_arm)
    result["status"] = "complete"

    out_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

    banner = banner_for(result)
    for key, cell in result["cells"].items():
        sys.stderr.write(
            f"  {key}: off_rps={cell['off_rows_per_s_median']:.0f} "
            f"on_rps={cell['on_rows_per_s_median']:.0f} ratio_median={cell['ratio_median']:.3f} "
            f"ci=[{cell['ci_low']:.3f},{cell['ci_high']:.3f}] rss_ratio={cell['rss_ratio']:.3f}\n"
        )
    if result["recommendation"] is not None:
        sys.stderr.write(f"  recommendation: {result['recommendation']}\n")
    if not result["run_ok"] and "error" in result:
        sys.stderr.write(f"  error: {result['error']}\n")
    sys.stderr.write(f"\n{banner}\n")

    return 0 if result["run_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
