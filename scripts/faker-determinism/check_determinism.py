"""Faker pool determinism driver (plan_faker_determinism_harness_v2.md, C3/C4).

For each `(faker_type, locale, kwargs)` candidate in the matrix, spawns K
fresh `sys.executable` subprocesses of `pool_determinism_worker.py` with an
explicit, controlled environment -- `PYTHONPATH`, distinct `PYTHONHASHSEED`
values (including 0), and pinned `TZ=UTC`/`LC_ALL=C`/`LANG=C` -- and checks
whether all K report the same `(pool_digest, output_digest)`, a consistently
true `exact_name_available`, a consistently false `custom_override_present`,
and the requested locale equalling the resolved one (never an accidental
en_US fallback masquerading as a certified pair).

A candidate that never becomes eligible (`exact_name_available` false, or a
custom override, or a fallback-resolved locale) is reported as such, not as
a failure of the harness -- "not certified" is an expected, common outcome
for a locale that simply lacks that Faker method. Only two things make the
whole run fail (nonzero exit): a worker crashing/timing out/emitting
unparseable output (an infrastructure fault, not a determinism finding), or
a CERTIFIED digest disagreeing with a previously recorded `golden_digests.json`
entry for the same candidate (a real cross-time regression).

Usage:
    check_determinism.py --write-golden   # explicit, reviewed baseline op
    check_determinism.py                  # default: assert against golden

See this directory's README.md for the full candidate matrix and how to
read `certified_pairs.json` / `determinism_report.json`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from decoy_engine.generation import _faker_pool

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent.parent
DEFAULT_WORKER = HERE / "pool_determinism_worker.py"
DEFAULT_GOLDEN_PATH = HERE / "golden_digests.json"
DEFAULT_CERTIFIED_OUT = HERE / "certified_pairs.json"
DEFAULT_REPORT_OUT = HERE / "determinism_report.json"

# The 10 types already in production, as a POSITIVE control (must certify
# wherever the method exists) -- imported, never restated, so this matrix
# cannot silently drift from the real allowlist.
POSITIVE_CONTROL: tuple[str, ...] = tuple(sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES))

# Tier 1 (faker_widening_research.md): gendered/variant siblings of the
# already-pooled 10 -- same reuse-safety class, lowest-risk widening
# candidates.
TIER_1: tuple[str, ...] = (
    "first_name_female",
    "first_name_male",
    "first_name_nonbinary",
    "last_name_female",
    "last_name_male",
    "last_name_nonbinary",
    "name_female",
    "name_male",
    "name_nonbinary",
    "prefix_female",
    "prefix_male",
    "prefix_nonbinary",
    "suffix_female",
    "suffix_male",
    "suffix_nonbinary",
    "job_female",
    "job_male",
    "company_suffix",
    "catch_phrase",
    "bs",
)

# Tier 2: geo scalars, PII-relevant. `building_number` is deliberately
# excluded per the research doc's own caution (numeric, higher-cardinality,
# "vet carefully or exclude").
TIER_2: tuple[str, ...] = (
    "street_name",
    "street_suffix",
    "city_prefix",
    "city_suffix",
    "state_abbr",
    "country_code",
    "administrative_unit",
    "military_state",
    "current_country",
)

DEFAULT_TYPES: tuple[str, ...] = tuple(sorted(set(POSITIVE_CONTROL) | set(TIER_1) | set(TIER_2)))

# The locale set Decoy customers actually use (plan's own framing) -- a
# real, extensible starting set, not every Faker locale.
DEFAULT_LOCALES: tuple[str, ...] = ("en_US", "en_GB", "fr_FR", "de_DE", "es_ES")

# K=8, fixed and including 0 (plan C3): the verdict must be reproducible
# run-to-run, so this is not drawn from anything random.
DEFAULT_HASH_SEEDS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7)

_MARKER_RE = re.compile(r"^POOL_DETERMINISM_JSON (.+)$")


class WorkerFailureError(RuntimeError):
    """A worker process crashed, timed out, or emitted unparseable output --
    an infrastructure fault distinct from a candidate simply failing to
    certify (which is reported as a normal per-candidate status)."""


@dataclass(frozen=True)
class Candidate:
    faker_type: str
    locale: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.faker_type}|{self.locale}|{json.dumps(self.kwargs, sort_keys=True)}"


@dataclass
class CandidateResult:
    candidate: Candidate
    status: str  # certified | not_available | custom_override | locale_fallback | divergent | inconsistent
    pool_digest: str | None
    output_digest: str | None
    k: int
    detail: str = ""


def build_matrix(
    types: Sequence[str], locales: Sequence[str], kwargs: dict[str, Any]
) -> list[Candidate]:
    return [Candidate(t, loc, dict(kwargs)) for t in types for loc in locales]


def _parse_marker_line(stdout: str) -> dict[str, Any]:
    marker_lines = [
        ln
        for ln in stdout.splitlines()
        if ln == "POOL_DETERMINISM_JSON" or ln.startswith("POOL_DETERMINISM_JSON ")
    ]
    if len(marker_lines) != 1:
        raise WorkerFailureError(
            f"expected exactly one POOL_DETERMINISM_JSON line, found {len(marker_lines)}: "
            f"{stdout[-2000:]!r}"
        )
    match = _MARKER_RE.match(marker_lines[0])
    assert match is not None  # noqa: S101 -- guaranteed by the prefix check above
    try:
        record = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise WorkerFailureError(f"worker JSON line failed to parse: {exc}") from exc
    if not isinstance(record, dict):
        raise WorkerFailureError(f"worker JSON line did not parse to an object: {record!r}")
    return record


def run_worker_once(
    candidate: Candidate,
    *,
    worker_path: Path,
    hash_seed: int,
    timeout_s: float,
    python_exe: str,
) -> dict[str, Any]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "PYTHONPATH": str(ENGINE_ROOT / "src"),
        "PYTHONHASHSEED": str(hash_seed),
        "TZ": "UTC",
        "LC_ALL": "C",
        "LANG": "C",
    }
    locale_arg = candidate.locale if candidate.locale is not None else "-"
    cmd = [
        python_exe,
        str(worker_path),
        candidate.faker_type,
        locale_arg,
        json.dumps(candidate.kwargs, sort_keys=True),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed worker script + validated candidate fields
            cmd,
            env=env,
            cwd=str(ENGINE_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkerFailureError(
            f"{candidate.key} hash_seed={hash_seed}: worker timed out after {timeout_s}s"
        ) from exc
    if proc.returncode != 0:
        raise WorkerFailureError(
            f"{candidate.key} hash_seed={hash_seed}: worker exited {proc.returncode}: "
            f"{proc.stderr[-2000:]}"
        )
    return _parse_marker_line(proc.stdout)


def verdict_from_records(candidate: Candidate, records: list[dict[str, Any]]) -> CandidateResult:
    """PASS criteria per plan C3: all K (pool_digest, output_digest) agree,
    AND exact_name_available, AND not custom_override_present, AND
    requested locale == resolved locale. The four "should never vary across
    K identical-input runs" fields are checked for INTERNAL agreement
    first -- any disagreement there is a stronger signal (`inconsistent`)
    than a normal non-certification outcome, since it means the resolver
    itself is not process-stable."""
    exact_avail_vals = {r["exact_name_available"] for r in records}
    custom_override_vals = {r["custom_override_present"] for r in records}
    resolved_locale_vals = {r["meta"]["resolved_locale"] for r in records}
    pool_digest_vals = {r["pool_digest"] for r in records}
    output_digest_vals = {r["output_digest"] for r in records}

    if len(exact_avail_vals) > 1 or len(custom_override_vals) > 1 or len(resolved_locale_vals) > 1:
        return CandidateResult(
            candidate,
            status="inconsistent",
            pool_digest=None,
            output_digest=None,
            k=len(records),
            detail=(
                f"exact_name_available={sorted(exact_avail_vals, key=str)} "
                f"custom_override_present={sorted(custom_override_vals, key=str)} "
                f"resolved_locale={sorted(resolved_locale_vals, key=str)}"
            ),
        )

    # Read cardinality BEFORE picking a representative value out of each
    # set: `set.pop()` mutates, so checking `len(...)` after popping would
    # always see the post-pop (usually empty) set -- a real bug this
    # ordering exists to avoid reintroducing.
    pool_digest_agrees = len(pool_digest_vals) == 1
    output_digest_agrees = len(output_digest_vals) == 1
    distinct_pool_digests = len(pool_digest_vals)

    exact_available = exact_avail_vals.pop()
    custom_override = custom_override_vals.pop()
    resolved_locale = resolved_locale_vals.pop()
    pool_digest = next(iter(pool_digest_vals)) if pool_digest_agrees else None
    output_digest = next(iter(output_digest_vals)) if output_digest_agrees else None

    if not exact_available:
        status = "not_available"
    elif custom_override:
        status = "custom_override"
    elif candidate.locale != resolved_locale:
        status = "locale_fallback"
    elif not (pool_digest_agrees and output_digest_agrees):
        status = "divergent"
    else:
        status = "certified"

    if status in ("certified", "not_available"):
        detail = ""
    elif status == "divergent":
        detail = f"{distinct_pool_digests} distinct pool_digest(s) across K={len(records)}"
    else:
        detail = f"resolved_locale={resolved_locale}"
    return CandidateResult(
        candidate,
        status=status,
        pool_digest=pool_digest,
        output_digest=output_digest,
        k=len(records),
        detail=detail,
    )


def run_candidate(
    candidate: Candidate,
    *,
    worker_path: Path,
    hash_seeds: Sequence[int],
    timeout_s: float,
    python_exe: str,
) -> CandidateResult:
    records = [
        run_worker_once(
            candidate,
            worker_path=worker_path,
            hash_seed=seed,
            timeout_s=timeout_s,
            python_exe=python_exe,
        )
        for seed in hash_seeds
    ]
    return verdict_from_records(candidate, records)


def run_matrix(
    matrix: Sequence[Candidate],
    *,
    worker_path: Path,
    hash_seeds: Sequence[int],
    timeout_s: float,
    python_exe: str,
    jobs: int,
) -> list[CandidateResult]:
    if jobs <= 1:
        return [
            run_candidate(
                c,
                worker_path=worker_path,
                hash_seeds=hash_seeds,
                timeout_s=timeout_s,
                python_exe=python_exe,
            )
            for c in matrix
        ]
    results: dict[str, CandidateResult] = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(
                run_candidate,
                c,
                worker_path=worker_path,
                hash_seeds=hash_seeds,
                timeout_s=timeout_s,
                python_exe=python_exe,
            ): c
            for c in matrix
        }
        for future in as_completed(futures):
            candidate = futures[future]
            results[candidate.key] = future.result()
    return [results[c.key] for c in matrix]


def load_golden(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_golden(path: Path, golden: dict[str, dict[str, Any]]) -> None:
    ordered = dict(sorted(golden.items()))
    path.write_text(json.dumps(ordered, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class RunReport:
    run_ok: bool
    write_golden: bool
    k: int
    hash_seeds: list[int]
    counts: dict[str, int]
    regressions: list[dict[str, Any]]
    results: list[dict[str, Any]]


def build_report(
    results: list[CandidateResult],
    *,
    write_golden_mode: bool,
    k: int,
    hash_seeds: Sequence[int],
    regressions: list[dict[str, Any]],
) -> RunReport:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return RunReport(
        run_ok=not regressions,
        write_golden=write_golden_mode,
        k=k,
        hash_seeds=list(hash_seeds),
        counts=counts,
        regressions=regressions,
        results=[
            {
                "faker_type": r.candidate.faker_type,
                "locale": r.candidate.locale,
                "kwargs": r.candidate.kwargs,
                "status": r.status,
                "pool_digest": r.pool_digest,
                "output_digest": r.output_digest,
                "k": r.k,
                "detail": r.detail,
            }
            for r in results
        ],
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Faker pool determinism harness driver")
    p.add_argument("--types", default=",".join(DEFAULT_TYPES))
    p.add_argument("--locales", default=",".join(DEFAULT_LOCALES))
    p.add_argument("--kwargs-json", default="{}")
    p.add_argument("--k", type=int, default=len(DEFAULT_HASH_SEEDS))
    p.add_argument("--hash-seeds", default=",".join(str(s) for s in DEFAULT_HASH_SEEDS))
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--worker", default=str(DEFAULT_WORKER))
    p.add_argument("--write-golden", action="store_true")
    p.add_argument("--golden-path", default=str(DEFAULT_GOLDEN_PATH))
    p.add_argument("--certified-out", default=str(DEFAULT_CERTIFIED_OUT))
    p.add_argument("--report-out", default=str(DEFAULT_REPORT_OUT))
    p.add_argument("--python", default=sys.executable)
    return p


def parse_and_validate_args(
    argv: Sequence[str] | None, parser: argparse.ArgumentParser | None = None
) -> argparse.Namespace:
    parser = parser or build_arg_parser()
    args = parser.parse_args(argv)

    args.types = [t.strip() for t in args.types.split(",") if t.strip()]
    if not args.types:
        parser.error("--types must be non-empty")
    args.locales = [loc.strip() for loc in args.locales.split(",") if loc.strip()]
    if not args.locales:
        parser.error("--locales must be non-empty")
    try:
        kwargs = json.loads(args.kwargs_json)
    except json.JSONDecodeError as exc:
        parser.error(f"--kwargs-json is not valid JSON: {exc}")
        kwargs = {}  # unreachable
    if not isinstance(kwargs, dict):
        parser.error("--kwargs-json must decode to a JSON object")
    args.kwargs = kwargs

    try:
        hash_seeds = [int(s.strip()) for s in args.hash_seeds.split(",") if s.strip() != ""]
    except ValueError:
        parser.error(
            f"--hash-seeds must be a comma-separated list of ints, got {args.hash_seeds!r}"
        )
        hash_seeds = []  # unreachable
    if len(hash_seeds) != len(set(hash_seeds)):
        parser.error("--hash-seeds must not contain duplicates")
    if args.k < 1:
        parser.error("--k must be >= 1")
    if args.k > len(hash_seeds):
        parser.error(f"--k ({args.k}) exceeds the number of --hash-seeds given ({len(hash_seeds)})")
    args.hash_seeds = hash_seeds[: args.k]

    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    args.worker_path = Path(args.worker).resolve()
    if not args.worker_path.exists():
        parser.error(f"--worker path does not exist: {args.worker_path}")
    args.golden_path = Path(args.golden_path).resolve()
    args.certified_out_path = Path(args.certified_out).resolve()
    args.report_out_path = Path(args.report_out).resolve()

    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_and_validate_args(argv)
    matrix = build_matrix(args.types, args.locales, args.kwargs)

    try:
        results = run_matrix(
            matrix,
            worker_path=args.worker_path,
            hash_seeds=args.hash_seeds,
            timeout_s=args.timeout,
            python_exe=args.python,
            jobs=args.jobs,
        )
    except WorkerFailureError as exc:
        error_report = {"run_ok": False, "error": str(exc)}
        args.report_out_path.write_text(json.dumps(error_report, indent=2) + "\n", encoding="utf-8")
        sys.stderr.write(f"FAKER DETERMINISM HARNESS FAILED (infrastructure fault): {exc}\n")
        return 1

    golden = {} if args.write_golden else load_golden(args.golden_path)
    regressions: list[dict[str, Any]] = []
    certified_pairs: list[dict[str, Any]] = []
    new_golden: dict[str, dict[str, Any]] = dict(golden)

    for r in results:
        key = r.candidate.key
        if r.status == "certified":
            certified_pairs.append(
                {
                    "faker_type": r.candidate.faker_type,
                    "locale": r.candidate.locale,
                    "kwargs": r.candidate.kwargs,
                    "pool_digest": r.pool_digest,
                    "output_digest": r.output_digest,
                }
            )
        if args.write_golden:
            if r.status == "certified":
                new_golden[key] = certified_pairs[-1]
        else:
            existing = golden.get(key)
            if existing is not None:
                mismatch = (
                    r.status != "certified"
                    or r.pool_digest != existing["pool_digest"]
                    or r.output_digest != existing["output_digest"]
                )
                if mismatch:
                    regressions.append(
                        {
                            "candidate": key,
                            "golden": existing,
                            "current_status": r.status,
                            "current_pool_digest": r.pool_digest,
                            "current_output_digest": r.output_digest,
                        }
                    )

    if args.write_golden:
        write_golden(args.golden_path, new_golden)

    report = build_report(
        results,
        write_golden_mode=args.write_golden,
        k=len(args.hash_seeds),
        hash_seeds=args.hash_seeds,
        regressions=regressions,
    )
    args.certified_out_path.write_text(
        json.dumps(certified_pairs, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    args.report_out_path.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")

    sys.stderr.write(f"counts: {report.counts}\n")
    if regressions:
        sys.stderr.write(f"REGRESSIONS ({len(regressions)}):\n")
        for reg in regressions:
            sys.stderr.write(f"  {reg}\n")
    banner = "DETERMINISM CHECK OK" if report.run_ok else "DETERMINISM CHECK FAILED"
    sys.stderr.write(f"\n{banner}\n")
    return 0 if report.run_ok else 1


if __name__ == "__main__":
    sys.exit(main())
