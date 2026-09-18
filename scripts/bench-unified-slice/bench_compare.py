"""Task 4.5 D9 statistical comparison harness: certifies that the unified-
slice lane (flag on) is not a performance or memory regression against the
legacy full-frame route (flag off), at the SAME revision, across the
10k/100k/1M row tiers `scripts/bench-unified-slice/README.md` requires.

Runs the frozen `bench_worker_unified.py` as a fresh subprocess per arm per
rep, alternating arm order by rep parity so a short host load spike cannot
land entirely inside one arm. See the README's FOLLOWUP-BENCH-D9 section for
the full requirement list this harness must satisfy, and the sibling
`docs/plans` entry for the design rationale (two-state run_ok/d9_certified
model, the race-free child lifecycle, and the paired-ratio statistics).

Building this harness is not the same as CERTIFYING D9: `d9_certified` is
only ever true after a real run over the exact {10_000, 100_000, 1_000_000}
tier set, at cert-minimum reps/warmup/bootstrap, with every gate passing.
This module's own tests, and any `--tiers 200` smoke invocation, are
`run_ok` at most -- they print `SMOKE COMPLETE (d9_certified=false)`, never
`D9 PASSED`.

The cross-revision "flag-off vs pre-task main" baseline the README also
mentions is explicitly OUT of scope here (Cam descope, 2026-09-15): this
harness compares flag-off vs flag-on at one revision only. That does not
certify historical flag-off performance or a common-mode regression shared
by both arms; `tests/physical/test_unified_slice_inertness.py` is the
separate proof that flag-off is inert at this revision.

Does not import or modify `scripts/native-baseline/bench_driver.py` (a
separate follow-up owns hardening that shared driver) or
`bench_worker_unified.py` (frozen workload substrate).
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent.parent
WORKER_PATH = HERE / "bench_worker_unified.py"
# Self-locating, same interpreter that runs this driver (matches bench_driver.py's
# own reasoning): a worktree checkout has no fixed venv path, so hardcoding one
# would break the first worker spawn on a fresh host.
VENV_PY = Path(sys.executable)
_WORKER_ENV_BASE = {**os.environ, "PYTHONPATH": str(ENGINE / "src")}

HARNESS_VERSION = "1.0.0"
# Fixed default seed: the bootstrap CI must be reproducible run-to-run, never
# drawn from unseeded randomness.
_DEFAULT_SEED = 20260915
_DEFAULT_TIERS = "10000,100000,1000000"
_DESCOPED = ["flag-off-vs-main-cross-revision"]

# ---------------------------------------------------------------------------
# Two-state cert model (plan §2)
# ---------------------------------------------------------------------------

_CERT_TIERS = frozenset({10_000, 100_000, 1_000_000})
_CERT_MIN_REPS = 20
_CERT_MIN_WARMUP = 3
_CERT_MIN_BOOTSTRAP = 2000
_SMALL_TIER_MAX_ROWS = 10_000

# Peak-RSS regression budget (unified/"on" arm vs legacy/"off" arm), per tier.
# This is a RELATIVE regression-detection band (on vs off at the same revision),
# not an absolute-safety limit -- absolute safety is enforced elsewhere (the
# frozen mem limit / mem telemetry). At 1M the unified lane's absolute peak is
# ~1.8GB, far under the 6.5GiB-at-100M reference-host ceiling, and it trades a
# larger transient reconstruction buffer for a ~4x wall-time win, so the
# 1M-and-up tier carries a wider band; the smaller tiers, where no such buffer
# dominates, keep the tight default. The band stays open above 1M by intent
# (a bigger job carries a proportionally similar buffer); the ~1.8GB figure is
# the 1M measurement, not an absolute claim for larger custom runs.
_RSS_BUDGET_DEFAULT = 1.10
_RSS_BUDGET_LARGE_TIER = 1.25
_RSS_LARGE_TIER_MIN_ROWS = 1_000_000


def rss_budget_ratio(n_rows: int) -> float:
    """The peak-RSS ratio the "on" arm must stay within for this tier."""
    if n_rows >= _RSS_LARGE_TIER_MIN_ROWS:
        return _RSS_BUDGET_LARGE_TIER
    return _RSS_BUDGET_DEFAULT


def is_cert_shape(tiers: Sequence[int], *, reps: int, warmup: int, bootstrap: int) -> bool:
    """The ONLY cert-eligible shape: tiers exactly {10k, 100k, 1M} (a set --
    a duplicate, extra, missing, or non-positive tier makes this False), plus
    the cert-minimum sample sizes. `run_ok` does not depend on this: a tiny
    or custom run can still be `run_ok=true`, just never `d9_certified`."""
    return (
        len(tiers) == len(set(tiers))
        and set(tiers) == _CERT_TIERS
        and reps >= _CERT_MIN_REPS
        and warmup >= _CERT_MIN_WARMUP
        and bootstrap >= _CERT_MIN_BOOTSTRAP
    )


class FailClosedError(RuntimeError):
    """Any §6 fail-closed condition. Raised via real `if: raise` throughout
    this module, never `assert` (stripped by `python -O`). Catching this at
    the top level is what flips `run_ok` to False and the exit code non-zero."""


# ---------------------------------------------------------------------------
# Pure statistics (plan §4) -- no subprocess, no I/O, fully unit-testable.
# ---------------------------------------------------------------------------


def _inclusive_percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation inclusive percentile. Used for the bootstrap
    median distribution's [2.5, 97.5] interval rather than
    `statistics.quantiles` because it degrades gracefully at n=1 --
    `--bootstrap 1` is a valid (if statistically thin) input per the CLI
    floor and must not crash on `quantiles`'s n>=2 requirement."""
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
    """p95 defined independently of `bench_driver.summarize` (which
    mislabels the maximum as p95): the inclusive-method 95th percentile.
    Requires len(values) >= 2, guaranteed by the `--reps >= 2` CLI floor."""
    ordered = sorted(values)
    return statistics.quantiles(ordered, n=100, method="inclusive")[94]


def bootstrap_ci(ratios: Sequence[float], *, seed: int, n_bootstrap: int) -> tuple[float, float]:
    """Seeded bootstrap CI on the paired-ratio median: resample the rep
    INDEX set with replacement `n_bootstrap` times, recompute the median
    ratio per resample, take the inclusive [2.5, 97.5] percentile interval.
    A LOCAL `random.Random(seed)` -- never the shared/global `random` module
    -- so this is reproducible and never interferes with other seeded state."""
    rng = random.Random(seed)
    n = len(ratios)
    medians = []
    for _ in range(n_bootstrap):
        sample = [ratios[rng.randrange(n)] for _ in range(n)]
        medians.append(statistics.median(sample))
    medians.sort()
    return _inclusive_percentile(medians, 2.5), _inclusive_percentile(medians, 97.5)


def apply_gates(
    n_rows: int,
    *,
    off_wall_median: float,
    on_wall_median: float,
    ratio_median: float,
    ratio_p95: float,
    ci_high: float,
    off_rss_max_kb: int,
    on_rss_max_kb: int,
) -> dict[str, bool]:
    """Every tier is gated, selected by size, so no tier (default or custom)
    ever runs un-gated (plan §4). `ratio_median`/`ratio_p95` must be the
    PAIRED per-rep ratio's statistics -- never a ratio of independent
    medians, which is a different (and here, wrong) quantity.

    Gate key names are illustrative in the plan ("gates:{median,p95,ci,
    rss,...}"); the small-tier point-difference rule is reported under
    "point" rather than "median" since it compares raw wall medians, not a
    ratio -- named for what it actually checks, not to match a literal key
    list the plan does not exhaustively enumerate.
    """
    gates: dict[str, bool] = {}
    if n_rows > _SMALL_TIER_MAX_ROWS:
        gates["median"] = ratio_median <= 1.10
        gates["p95"] = ratio_p95 <= 1.15
        gates["ci"] = ci_high <= 1.10
    else:
        # The 50ms absolute floor folded into a ratio-shaped bound so the
        # bootstrap CI upper bound is itself gated (plan §4 [BLOCKER C2]):
        # a report-only exemption would let a real small-tier regression
        # through unchallenged.
        point_floor = max(0.10 * off_wall_median, 0.050)
        gates["point"] = (on_wall_median - off_wall_median) <= point_floor
        ci_floor = 1.0 + max(0.10, 0.050 / off_wall_median)
        gates["ci"] = ci_high <= ci_floor
    gates["rss"] = (
        off_rss_max_kb > 0
        and on_rss_max_kb > 0
        and on_rss_max_kb <= rss_budget_ratio(n_rows) * off_rss_max_kb
    )
    return gates


# ---------------------------------------------------------------------------
# Strict worker-record validation (plan §5) -- pure, no subprocess.
# ---------------------------------------------------------------------------

# Matches ONE marker line and captures its payload (>= 1 char): a bare
# `BENCH_JSON` with no payload does not match, so it is caught as a
# no-payload error rather than silently ignored.
_BENCH_JSON_RE = re.compile(r"^BENCH_JSON (.+)$")

# The frozen worker's admitted 9-column shape (bench_worker_unified.py
# module docstring: 3 hash + 2 passthrough + 2 redact + 2 truncate, pt_ts
# excluded). Restated here, sorted, rather than imported from the worker --
# an independent expected value is what makes the fingerprint check a real
# assertion instead of the worker grading its own homework. Already in
# sorted order (verified by a harness test), matching the worker's own
# `sorted(...)` construction.
_EXPECTED_ADMITTED_COLUMNS = (
    "h_email",
    "h_token",
    "h_uid",
    "pt_amount",
    "pt_flag",
    "rd_notes",
    "rd_ssn",
    "tr_card",
    "tr_phone",
)

_NUMERIC_FIELDS = ("wall_s", "hash_ms", "redact_ms", "truncate_ms", "passthrough_ms")


def _expected_fingerprint(n_rows: int) -> dict[str, Any]:
    return {"n_rows": n_rows, "columns": list(_EXPECTED_ADMITTED_COLUMNS)}


def _reject_nonfinite_constant(name: str) -> NoReturn:
    raise FailClosedError(f"worker record JSON contains disallowed constant {name!r}")


def validate_worker_record(
    stdout: str, *, expected_n_rows: int, expected_flag_on: bool
) -> dict[str, Any]:
    """§5: strict typed validation of one worker invocation's FULL stdout
    buffer (never a partial one -- see `run_child_process`'s drain-
    completeness contract). Raises `FailClosedError` on the first violation;
    every check is a real `if: raise`, never `assert`."""
    # Count a line that IS the bare marker OR starts with the marker+space, not
    # only lines carrying a payload: a stray bare `BENCH_JSON` second line must
    # still trip the cardinality check. This layer exists to distrust worker
    # output, so a near-marker fails rather than being silently ignored.
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

    for field in _NUMERIC_FIELDS:
        if field not in record:
            raise FailClosedError(f"worker record missing numeric field {field!r}")
        value = record[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FailClosedError(f"worker record field {field!r} is not numeric: {value!r}")
        if not math.isfinite(value):
            raise FailClosedError(f"worker record field {field!r} is not finite: {value!r}")
    if record["wall_s"] <= 0:
        raise FailClosedError(f"worker record wall_s must be > 0, got {record['wall_s']!r}")

    for field in ("n_rows", "out_rows"):
        if field not in record:
            raise FailClosedError(f"worker record missing field {field!r}")
        value = record[field]
        # `type(x) is int` (not `isinstance`): isinstance(True, int) is True
        # in Python, which would silently admit a bool as a row count.
        if type(value) is not int:
            raise FailClosedError(f"worker record field {field!r} must be int, got {value!r}")
        if value != expected_n_rows:
            raise FailClosedError(
                f"worker record field {field!r}={value!r} != requested n_rows={expected_n_rows!r}"
            )

    if "workload_fingerprint" not in record:
        raise FailClosedError("worker record missing workload_fingerprint")
    fingerprint = record["workload_fingerprint"]
    if fingerprint is None:
        raise FailClosedError("worker record workload_fingerprint is null")
    if not isinstance(fingerprint, dict):
        raise FailClosedError(
            f"worker record workload_fingerprint is not an object: {fingerprint!r}"
        )
    # `type(x) is int` before the dict `==`: `{"n_rows": 10000.0}` and
    # `{"n_rows": True}` compare equal to the int-keyed expected dict under
    # ordinary equality, so a float/bool row count would slip through unchecked.
    fp_n_rows = fingerprint.get("n_rows")
    if type(fp_n_rows) is not int:
        raise FailClosedError(
            f"worker record workload_fingerprint n_rows must be int, got {fp_n_rows!r}"
        )
    expected_fp = _expected_fingerprint(expected_n_rows)
    if fingerprint != expected_fp:
        raise FailClosedError(
            f"worker record workload_fingerprint {fingerprint!r} does not match the "
            f"expected admitted-column shape {expected_fp!r}"
        )

    if "unified_slice_activated" not in record:
        raise FailClosedError("worker record missing unified_slice_activated")
    if "execution_mode" not in record:
        raise FailClosedError("worker record missing execution_mode")
    activated = record["unified_slice_activated"]
    mode = record["execution_mode"]
    if expected_flag_on:
        if activated is not True:
            raise FailClosedError(
                f"on-arm unified_slice_activated is not True (identity check): {activated!r}"
            )
        if mode != "unified_slice":
            raise FailClosedError(f"on-arm execution_mode != 'unified_slice': {mode!r}")
    else:
        if activated is not False:
            raise FailClosedError(
                f"off-arm unified_slice_activated is not False (identity check): {activated!r}"
            )
        if mode != "legacy_full_frame":
            raise FailClosedError(f"off-arm execution_mode != 'legacy_full_frame': {mode!r}")

    return record


# ---------------------------------------------------------------------------
# Mandated race-free child lifecycle (plan §3a) -- ONE algorithm, no
# alternatives. See the plan for the hazards each step avoids.
# ---------------------------------------------------------------------------


@dataclass
class RawArmResult:
    """Raw evidence from one child invocation, before §5 record validation.

    `stdout`/`stderr` are trustworthy ONLY when `drained_ok` is True: a
    reader thread still alive past its join deadline (a descendant still
    holding the pipe), or one that captured a read exception, means the
    buffer may be partial, and a partial buffer must never be parsed (a
    later duplicate BENCH_JSON line could be missed, reading as false
    success). `ru_maxrss_kb` is `os.wait4`'s rusage -- the sole authoritative
    peak-RSS source; `peak_vmhwm_kb` is the secondary /proc running-max,
    human-progress-only, never gated on.
    """

    stdout: str
    stderr: str
    returncode: int
    drained_ok: bool
    timed_out: bool
    ru_maxrss_kb: int | None
    peak_vmhwm_kb: int | None


ArmRunner = Callable[[str, int, float], RawArmResult]

_HWM_RE = re.compile(r"^VmHWM:\s*(\d+)\s*kB", re.MULTILINE)


def _read_vmhwm_kb(pid: int) -> int | None:
    """Secondary /proc peak-RSS cross-check only (module docstring above);
    `ru_maxrss` from `os.wait4` remains the terminal, gate-relevant number."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            m = _HWM_RE.search(fh.read())
            return int(m.group(1)) if m else None
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None


def _drain_stream(stream: Any, chunks: list[str], errors: list[BaseException]) -> None:
    """Runs in a daemon reader thread. Captures any read exception into
    `errors` instead of letting it escape the thread silently -- the caller
    has no other way to observe a reader failure."""
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
    """The mandated race-free lifecycle. Spawn as a session/group leader so
    the SAVED pgid (== pid, by `start_new_session`) can group-kill any
    descendant that inherited a pipe; drain both pipes on daemon threads
    concurrently with an `os.wait4(WNOHANG)` poll (never `Popen.wait`/
    `poll`/`communicate`/`kill` before the reap -- each would steal the
    child or lose its rusage/status); assign `returncode` from the reaped
    status before touching `proc` any other way; then require BOTH readers
    to finish cleanly before the buffer is considered parseable.
    """
    proc = subprocess.Popen(  # noqa: S603 fixed benchmark worker invocation, no untrusted input
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    # start_new_session makes this child its own session/group leader, so its
    # pgid equals its pid. This SAVED value is the ONLY pgid used below --
    # `os.getpgid(proc.pid)` after the reap would look up a pid the OS is
    # free to recycle.
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
    peak_vmhwm_kb: int | None = None
    timed_out = False
    status = 0
    rusage = None
    while True:
        reaped_pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
        if reaped_pid != 0:
            break
        hwm = _read_vmhwm_kb(proc.pid)
        if hwm is not None and (peak_vmhwm_kb is None or hwm > peak_vmhwm_kb):
            peak_vmhwm_kb = hwm
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

    # Assign returncode from the reaped status BEFORE touching `proc` any
    # other way. A real defect once had wait-status 7 read back as
    # returncode == 0 because something reaped ahead of this assignment.
    proc.returncode = os.waitstatus_to_exitcode(status)

    t_out.join(timeout=5.0)
    t_err.join(timeout=5.0)
    drained_ok = not t_out.is_alive() and not t_err.is_alive() and not out_errors and not err_errors
    if not drained_ok:
        # A descendant (a grandchild that inherited a pipe) still holding a
        # write end, or a reader that raised: kill the whole group by the
        # SAVED pgid and give the readers one more bounded chance. The
        # buffer is treated as partial regardless of whether this second
        # join succeeds -- never parse a partial buffer.
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
        peak_vmhwm_kb=peak_vmhwm_kb,
    )


def _spawn_worker_arm(arm: str, n_rows: int, timeout_s: float) -> RawArmResult:
    """The real `ArmRunner`: invokes the frozen `bench_worker_unified.py`
    with `UNIFIED_BENCH_FLAG` set explicitly (never relying on the worker's
    own "on" default) so the flag is unambiguous per arm-run."""
    env = {**_WORKER_ENV_BASE, "UNIFIED_BENCH_FLAG": arm}
    cmd = [str(VENV_PY), str(WORKER_PATH), str(n_rows)]
    return run_child_process(cmd, cwd=str(ENGINE), env=env, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Orchestration: per-rep pairing/alternation, per-tier stats, overall state.
# ---------------------------------------------------------------------------


def _arm_order_for_rep(rep_index: int) -> tuple[str, str]:
    """Alternates by rep parity (plan §3a): even rep off-then-on, odd rep
    on-then-off, so a short host load spike cannot land entirely inside one
    arm across a whole tier."""
    return ("off", "on") if rep_index % 2 == 0 else ("on", "off")


def _run_one_rep(
    n_rows: int,
    rep_index: int,
    phase: str,
    arm_runner: ArmRunner,
    timeout_s: float,
    raw_reps: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    """Runs both arms of one rep in alternated order; returns (off_record,
    on_record, off_ru_maxrss_kb, on_ru_maxrss_kb). Raises `FailClosedError`
    on the first §6 condition this rep's evidence trips."""
    order = _arm_order_for_rep(rep_index)
    per_arm: dict[str, tuple[dict[str, Any], int]] = {}
    for arm in order:
        raw = arm_runner(arm, n_rows, timeout_s)
        if raw.timed_out:
            raise FailClosedError(f"tier {n_rows}: arm {arm!r} rep {rep_index} timed out")
        if not raw.drained_ok:
            raise FailClosedError(
                f"tier {n_rows}: arm {arm!r} rep {rep_index} pipe drain did not complete cleanly"
            )
        if raw.returncode != 0:
            raise FailClosedError(
                f"tier {n_rows}: arm {arm!r} rep {rep_index} exited {raw.returncode}"
            )
        if not raw.ru_maxrss_kb or raw.ru_maxrss_kb <= 0:
            raise FailClosedError(
                f"tier {n_rows}: arm {arm!r} rep {rep_index} missing/zero ru_maxrss evidence"
            )
        record = validate_worker_record(
            raw.stdout, expected_n_rows=n_rows, expected_flag_on=(arm == "on")
        )
        per_arm[arm] = (record, raw.ru_maxrss_kb)
        raw_reps.append(
            {
                "phase": phase,
                "pair_index": rep_index,
                "arm_order": list(order),
                "arm": arm,
                "wall_s": record["wall_s"],
                "ru_maxrss_kb": raw.ru_maxrss_kb,
                "execution_mode": record["execution_mode"],
            }
        )

    off_rec, off_rss = per_arm["off"]
    on_rec, on_rss = per_arm["on"]
    if off_rec["workload_fingerprint"] != on_rec["workload_fingerprint"]:
        raise FailClosedError(
            f"tier {n_rows} rep {rep_index}: workload_fingerprint differs between off/on arms"
        )
    sys.stderr.write(
        f"  [{n_rows}] {phase} rep {rep_index}: "
        f"off={off_rec['wall_s']:.3f}s(rss={off_rss}kb) on={on_rec['wall_s']:.3f}s(rss={on_rss}kb)\n"
    )
    sys.stderr.flush()
    return off_rec, on_rec, off_rss, on_rss


def _run_tier(
    n_rows: int,
    *,
    warmup: int,
    reps: int,
    bootstrap: int,
    seed: int,
    timeout_s: float,
    arm_runner: ArmRunner,
) -> dict[str, Any]:
    raw_reps: list[dict[str, Any]] = []
    off_walls: list[float] = []
    on_walls: list[float] = []
    off_rss_max = 0
    on_rss_max = 0

    for w in range(warmup):
        _run_one_rep(n_rows, w, "warmup", arm_runner, timeout_s, raw_reps)

    for r in range(reps):
        off_rec, on_rec, off_rss, on_rss = _run_one_rep(
            n_rows, r, "timed", arm_runner, timeout_s, raw_reps
        )
        off_walls.append(off_rec["wall_s"])
        on_walls.append(on_rec["wall_s"])
        off_rss_max = max(off_rss_max, off_rss)
        on_rss_max = max(on_rss_max, on_rss)

    if len(off_walls) != reps or len(on_walls) != reps:
        # Structural invariant, not expected to fire given the control flow
        # above (any failure already raises before appending) -- kept as an
        # explicit fail-closed check per plan §6 rule 5 rather than trusted
        # implicitly.
        raise FailClosedError(f"tier {n_rows}: fewer than the requested {reps} reps completed")
    if not off_rss_max or not on_rss_max:
        raise FailClosedError(f"tier {n_rows}: missing peak-RSS aggregate evidence")

    ratios = [on_w / off_w for on_w, off_w in zip(on_walls, off_walls, strict=True)]
    ratio_median = statistics.median(ratios)
    ratio_p95 = inclusive_p95(ratios)
    off_wall_median = statistics.median(off_walls)
    on_wall_median = statistics.median(on_walls)
    off_wall_p95 = inclusive_p95(off_walls)
    on_wall_p95 = inclusive_p95(on_walls)
    ci_low, ci_high = bootstrap_ci(ratios, seed=seed, n_bootstrap=bootstrap)

    gates = apply_gates(
        n_rows,
        off_wall_median=off_wall_median,
        on_wall_median=on_wall_median,
        ratio_median=ratio_median,
        ratio_p95=ratio_p95,
        ci_high=ci_high,
        off_rss_max_kb=off_rss_max,
        on_rss_max_kb=on_rss_max,
    )
    if not all(gates.values()):
        raise FailClosedError(f"tier {n_rows}: gate breach {gates}")

    return {
        "n_rows": n_rows,
        "warmups": warmup,
        "reps": reps,
        "off_wall_median": off_wall_median,
        "on_wall_median": on_wall_median,
        "off_wall_p95": off_wall_p95,
        "on_wall_p95": on_wall_p95,
        "ratio_median": ratio_median,
        "ratio_p95": ratio_p95,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "off_rss_max_kb": off_rss_max,
        "on_rss_max_kb": on_rss_max,
        "rss_ratio": on_rss_max / off_rss_max,
        "gates": gates,
        "raw_reps": raw_reps,
    }


@dataclass
class RunConfig:
    tiers: list[int]
    reps: int
    warmup: int
    bootstrap: int
    seed: int
    timeout_s: float


def run_bench_compare(config: RunConfig, arm_runner: ArmRunner) -> dict[str, Any]:
    """Orchestrates every requested tier. Stops at the first §6 fail-closed
    condition: a later tier's evidence cannot rescue an already-broken run,
    and running a further multi-minute tier once the run is doomed wastes
    time without changing the outcome."""
    tiers_out: dict[str, Any] = {}
    error: str | None = None
    for n_rows in config.tiers:
        try:
            tiers_out[str(n_rows)] = _run_tier(
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
            break

    run_ok = error is None
    cert_shape = is_cert_shape(
        config.tiers, reps=config.reps, warmup=config.warmup, bootstrap=config.bootstrap
    )
    result: dict[str, Any] = {
        "run_ok": run_ok,
        "d9_certified": run_ok and cert_shape,
        "harness_version": HARNESS_VERSION,
        "seed": config.seed,
        "descoped": list(_DESCOPED),
        "tiers": tiers_out,
    }
    if error is not None:
        result["error"] = error
    return result


def banner_for(result: dict[str, Any]) -> str:
    if not result["run_ok"]:
        return "D9 FAILED"
    if result["d9_certified"]:
        return "D9 PASSED"
    return "SMOKE COMPLETE (d9_certified=false)"


def exit_code_for(result: dict[str, Any], *, require_cert: bool) -> int:
    if not result["run_ok"]:
        return 1
    if require_cert and not result["d9_certified"]:
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI (plan §7)
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Task 4.5 D9 statistical comparison harness")
    p.add_argument("--tiers", default=_DEFAULT_TIERS, help="comma-separated row counts")
    p.add_argument("--reps", type=int, default=20)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--out", default="results.json")
    p.add_argument("--bootstrap", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    p.add_argument("--timeout", type=float, default=600.0, help="per arm-run, seconds")
    p.add_argument(
        "--require-cert",
        action="store_true",
        help="exit 0 requires d9_certified, not just run_ok (the offline cert invocation uses this)",
    )
    return p


def parse_and_validate_args(
    argv: Sequence[str] | None, parser: argparse.ArgumentParser | None = None
) -> argparse.Namespace:
    """§7 CLI validation, run BEFORE any measurement or artifact write.
    Every violation goes through `parser.error` (stderr message, exit 2) so
    a bad invocation never reaches measurement or writes a PASS artifact."""
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

    if args.reps < 2:
        parser.error("--reps must be >= 2 (p95 and the bootstrap CI need at least two paired reps)")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    if args.bootstrap < 1:
        parser.error("--bootstrap must be >= 1")
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        # Positivity alone would let float("inf") through, disabling the
        # timeout entirely -- a hung worker would never be killed.
        parser.error("--timeout must be a finite number > 0")

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_and_validate_args(argv)

    out_path = Path(args.out)
    # Stale-artifact guard (plan §6): written BEFORE any measurement so a
    # killed/crashed run can never leave a stale PASS artifact on disk.
    out_path.write_text(
        json.dumps({"run_ok": False, "d9_certified": False, "status": "in_progress"})
    )

    config = RunConfig(
        tiers=args.tiers,
        reps=args.reps,
        warmup=args.warmup,
        bootstrap=args.bootstrap,
        seed=args.seed,
        timeout_s=args.timeout,
    )
    result = run_bench_compare(config, arm_runner=_spawn_worker_arm)
    result["status"] = "complete"

    out_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

    banner = banner_for(result)
    for n_rows_str, tier in result["tiers"].items():
        sys.stderr.write(
            f"  tier {n_rows_str}: off_median={tier['off_wall_median']:.4f}s "
            f"on_median={tier['on_wall_median']:.4f}s ratio_median={tier['ratio_median']:.3f} "
            f"ratio_p95={tier['ratio_p95']:.3f} ci=[{tier['ci_low']:.3f},{tier['ci_high']:.3f}] "
            f"rss_ratio={tier['rss_ratio']:.3f} gates={tier['gates']}\n"
        )
    if not result["run_ok"] and "error" in result:
        sys.stderr.write(f"  error: {result['error']}\n")
    sys.stderr.write(f"\n{banner}\n")

    return exit_code_for(result, require_cert=args.require_cert)


if __name__ == "__main__":
    sys.exit(main())
