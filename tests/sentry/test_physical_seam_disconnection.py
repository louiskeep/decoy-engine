"""D4 disconnection proof, part 1/2: the physical-execution adapter seam
(Task 4.2, `decoy_engine.execution.physical`) is REACHED BY NOTHING in
production -- except the one sanctioned Task 4.5 connection point.

Mirrors `test_public_import_boundary.py`'s regex-sentry pattern for the
opposite direction: that file guards the public boundary from reaching INTO
`internal.*`; this one guards every current route/coordinator/executor/
generation module from reaching INTO the new `execution.physical` package.
Task 4.2 is additive and disconnected by design (docs/plans/2026-09-13-
physical-plan-design.md; production activation is Task 4.5+) -- this test is
what keeps that true as the seam grows.

Part 2/2 (the exact-diff gate: production execution modules carry zero
behavioral diff versus `origin/main`) lives in this same file below, since
both checks answer the same question -- "did this task touch anything it
was not supposed to."

Task 4.3 (the physical-plan compiler, `_compiler.py` / `_inputs.py` /
`_snapshot.py` / `_reasons.py` / `_plan.py`) extends this same disconnection
proof rather than adding a parallel one: `GUARDED_MODULES` already covers
every module the compiler's own snapshot builder (`capture_physical_plan_
inputs`) imports from (`_pipeline_routing`, `_planner`, ...), so the existing static regex sweep and the
exact-diff gate already re-verify 4.3's disconnection unmodified. The one
genuine addition below (`test_compile_physical_plan_is_unreachable_from_a_
fresh_import_of_run_pipeline`) is DYNAMIC rather than static: it proves, in
a fresh subprocess, that importing `decoy_engine.execution.run_pipeline`
never pulls `execution.physical` into `sys.modules` -- a complementary check
the regex sweep (which only reads source text) cannot make, since a hidden
dynamic/conditional import would not appear as a matchable `import`
statement at all.

Task 4.5 (engine production-readiness) is the connection this test's own
docstring always named as the point the seam stops being fully disconnected:
a single new module, `execution/_unified_slice.py`, is now the ONE place
outside `execution/physical/` allowed to import it -- behind a default-OFF
per-run flag it checks BEFORE ever reaching for the seam (see that module's
own docstring). `DELIBERATELY_CONNECTED_MODULES` below is that one-file
allowlist; every OTHER production module stays exactly as disconnected as
`GUARDED_MODULES` + the broad sweep already proved. `run_pipeline`
(`_pipeline.py`) itself is unchanged in this respect: it imports
`_unified_slice`, a sibling execution module, never `execution.physical`
directly, so it still passes the static regex sweep below with no
modification.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"
REPO_ROOT = ENGINE_ROOT.parents[1]

# Every current production entry/coordinator/router/executor module the
# design doc names as a driver's real delegate, plus the routing-decision
# layers that select between them. If a NEW module needs to route through
# physical/ someday, that is Task 4.5+ and this list (or the whole test)
# must be revisited deliberately -- not silently pass because a new file
# never got added here.
GUARDED_MODULES: tuple[str, ...] = (
    "execution/_pipeline.py",
    "execution/_pipeline_routing.py",
    "execution/_pipeline_routing_signals.py",
    "execution/_pipeline_chunk_route.py",
    "execution/_pipeline_route_exec.py",
    "execution/_pipeline_sources.py",
    "execution/_pipeline_finalize.py",
    "execution/_planner.py",
    "execution/_pandas_adapter.py",
    "execution/_sequential.py",
    "execution/_chunked.py",
    "execution/_adapter.py",
    "execution/_substrate.py",
    "execution/_runner.py",
    "execution/native/_dispatch.py",
    "execution/native/_plan.py",
    "execution/native/_requirements.py",
    "execution/native/_capabilities.py",
    "execution/out_of_core/_runner.py",
    "execution/out_of_core/_compat.py",
    "execution/out_of_core/_route_policy.py",
    "generation/_plan_entry.py",
    "generation/synthesize.py",
)


# Task 4.5's sanctioned connection points: the ONLY files outside
# `execution/physical/` itself permitted to import the seam. Widening this
# is a deliberate, reviewed decision (the next task after 4.5 to touch
# production routing), never an incidental add. `_unified_slice_admission.py`
# is the D3 admission-predicate module `_unified_slice.py` was split out of
# to hold the ~600-LOC orchestration cap; its `resident_contract_admission`
# reaches for `execution.physical._types.DriverId`, lazily, behind the same
# flag-checked-before-import discipline.
DELIBERATELY_CONNECTED_MODULES: tuple[str, ...] = (
    "execution/_unified_slice.py",
    "execution/_unified_slice_admission.py",
)

_PHYSICAL_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+decoy_engine\.execution\.physical(?:\.\S+)?\s+import\b|"
    r"import\s+decoy_engine\.execution\.physical(?:\.\S+)?\b)",
    re.MULTILINE,
)


def _imports_physical(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "execution.physical" not in text:
        return False
    return bool(_PHYSICAL_IMPORT_RE.search(text))


def test_guarded_modules_exist() -> None:
    missing = [rel for rel in GUARDED_MODULES if not (ENGINE_ROOT / rel).exists()]
    assert not missing, (
        "GUARDED_MODULES entries that do not exist on disk (update the list if "
        "one of these was intentionally removed/renamed):\n  - " + "\n  - ".join(missing)
    )


def test_no_production_module_imports_the_physical_seam() -> None:
    offenders = [rel for rel in GUARDED_MODULES if _imports_physical(ENGINE_ROOT / rel)]
    assert not offenders, (
        "Production execution/generation modules importing "
        "decoy_engine.execution.physical (the Task 4.2 seam is additive and "
        "must not be wired into any live route until Task 4.5+):\n  - " + "\n  - ".join(offenders)
    )


def test_no_module_anywhere_under_src_imports_the_physical_seam_except_itself() -> None:
    """Broader sweep than `GUARDED_MODULES`: NOTHING under `src/decoy_engine`
    outside the `execution/physical/` package itself -- or the one Task 4.5
    `DELIBERATELY_CONNECTED_MODULES` entry -- may import it. Catches a future
    module neither list has been updated to name yet."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        if rel.startswith("execution/physical/") or rel in DELIBERATELY_CONNECTED_MODULES:
            continue
        if _imports_physical(path):
            offenders.append(rel)
    assert not offenders, (
        "Modules outside execution/physical/ (and outside "
        "DELIBERATELY_CONNECTED_MODULES) importing decoy_engine.execution.physical:\n  - "
        + "\n  - ".join(offenders)
    )


def test_deliberately_connected_modules_exist_and_do_import_physical() -> None:
    """The flip side of the sweep above: every allowlisted module must both
    exist and actually import the seam, so the allowlist cannot silently
    become stale (a removed/renamed file that no longer needs the
    exemption) without a test failure pointing at it."""
    for rel in DELIBERATELY_CONNECTED_MODULES:
        path = ENGINE_ROOT / rel
        assert path.exists(), f"DELIBERATELY_CONNECTED_MODULES entry does not exist: {rel}"
        assert _imports_physical(path), (
            f"{rel} is listed in DELIBERATELY_CONNECTED_MODULES but does not import "
            "decoy_engine.execution.physical; remove it from the allowlist if that is intended"
        )


# ---------------------------------------------------------------------------
# Part 2/2: exact-diff gate -- production execution modules are byte-for-byte
# unchanged versus the branch point (origin/main).
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def _origin_main_available() -> bool:
    try:
        _git("rev-parse", "--verify", "origin/main")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return True


@pytest.mark.skipif(
    not _origin_main_available(), reason="origin/main not reachable in this checkout"
)
def test_production_execution_modules_are_byte_identical_to_origin_main() -> None:
    """Tasks 4.2-4.4 are additive: every file under `src/decoy_engine/execution`
    that existed before this program must be untouched, with three exceptions --
    the Task 4.5 `_unified_slice.py` connection module; `_pipeline.py` itself,
    whose permitted diffs are the Task 4.5 flag kwarg + the single guarded
    call site (`maybe_run_unified_slice`) and the Task 4.6 slice-5b-i Step-3
    extraction (its inline generate+mask output stitch now delegates to the
    shared `execution/_stitch.py::stitch_generate_mask_outputs`, a new
    parent-level module both it and the shadow mixed dispatch call so the
    "mask wins ties" precedence cannot drift); and (Task 4.6 slice 1)
    `execution/native/_chunk_masking.py`, where `_sample_faker_chunk` is
    promoted to the shared, importable `sample_faker_array` helper so the
    native chunked route and the new physical-plan shadow faker operator run
    the IDENTICAL selection code rather than two copies that could drift --
    a deliberate, reviewed touch, not incidental scope creep (its one caller,
    `_mask_chunk_native`, is updated to match; native output is unchanged,
    proven by the existing `tests/parity/native/test_c1_faker_parity.py` /
    `tests/native/test_dispatch_faker.py` staying green unmodified). Task 4.6
    slice 5b-ii adds one more of the same kind: the FK orphan-policy helpers
    move verbatim from `_strategies/_orphan.py` into the new parent-level
    `_fk_resolve.py`, so the pandas oracle and the shadow mixed-FK dispatch
    share one owner (the `_pandas_adapter.py` / `_orphan.py` diffs are the
    import rewire plus the moved bodies; FK production output is unchanged,
    proven by the FK/RI/orphan/lossless suites staying green). Every other
    file's diff is still restricted to `execution/physical/`."""
    # Diff against the MERGE-BASE, not origin/main's tip: if origin/main advances
    # with unrelated execution/ changes before this branch merges, a raw
    # origin/main..HEAD diff would raise a false positive. The merge-base is the
    # branch point, which is the correct baseline for "what THIS branch changed".
    base = _git("merge-base", "origin/main", "HEAD").strip()
    diff_names = _git(
        "diff", "--name-only", base, "HEAD", "--", "src/decoy_engine/execution"
    ).splitlines()
    permitted_non_physical = {
        f"src/decoy_engine/{rel}" for rel in DELIBERATELY_CONNECTED_MODULES
    } | {
        "src/decoy_engine/execution/_pipeline.py",
        "src/decoy_engine/execution/native/_chunk_masking.py",
        # Task 4.6 slice 5b-i: the shared generate+mask output-stitch helper
        # both `_pipeline.py` and `execution/physical/_shadow_mixed.py` call,
        # so the "mask wins ties" precedence cannot drift between the two.
        # Lives at the PARENT `execution` level specifically so `_pipeline.py`
        # never has to import the physical seam to reach it -- it imports
        # NOTHING from `execution.physical` itself (confirmed by the sweeps
        # above), it is simply a new file outside that package.
        "src/decoy_engine/execution/_stitch.py",
        # Task 4.6 slice 5b-ii: the FK orphan-policy helpers (resolve_fk_keys,
        # gather_errored_parent_keys, cascade_row_errors) are extracted from
        # `_strategies/_orphan.py` into the new parent-level `_fk_resolve.py`
        # so the pandas oracle AND the shadow mixed-FK dispatch share one owner
        # of the map-hit/orphan/REMAP precedence and cannot drift. Same seam
        # discipline as `_stitch.py`: `_fk_resolve.py` imports NOTHING from
        # `execution.physical`; `_pandas_adapter.py` and `_strategies/_orphan.py`
        # import only that parent-level module (their diffs are the import
        # rewire + `_orphan.py`'s function bodies moving out verbatim). The four
        # import-direction seam sweeps above stay green.
        "src/decoy_engine/execution/_fk_resolve.py",
        "src/decoy_engine/execution/_pandas_adapter.py",
        "src/decoy_engine/execution/_strategies/_orphan.py",
        # Task 4.7: the superseded standalone native routing lane was deleted
        # (it had zero production ownership -- `native_route_enabled` always
        # defaulted False). `_adapter.py`'s diff is the removed
        # `ExecutionResult.native_route` field; the four `_native_route*.py`
        # files are the deleted lane itself. None is under execution/physical/,
        # so they are permitted deletions/edits here.
        "src/decoy_engine/execution/_adapter.py",
        "src/decoy_engine/execution/_native_route.py",
        "src/decoy_engine/execution/_native_route_exec.py",
        "src/decoy_engine/execution/_native_route_preflight.py",
        "src/decoy_engine/execution/_native_route_digest.py",
        # Polars masking removal (2026-09-21): the dormant polars masking adapter
        # was deleted (pre-GA hard delete; that substrate was value-parity with
        # pandas and never selected by default). These modules are edited to strip
        # the deleted adapter's references -- its planner classification mode + the
        # matching rejection helper, the when-gate variant, the reason-code
        # translation, the collapsed chunked-adapter gate, docstrings,
        # and the determinism-mirror sites for the deleted polars strategy files.
        # The pandas execution path is unchanged. None is under execution/physical/.
        # The deleted execution/polars/ tree itself is carved out of the filter below.
        "src/decoy_engine/execution/__init__.py",
        "src/decoy_engine/execution/_chunked.py",
        "src/decoy_engine/execution/_chunked_adapter_gate.py",
        "src/decoy_engine/execution/_planner.py",
        "src/decoy_engine/execution/_strategies/_top_code.py",
        "src/decoy_engine/execution/_substrate.py",
        "src/decoy_engine/execution/_when_gate.py",
        "src/decoy_engine/execution/native/_determinism_protocol.py",
        # Comment-only edits stripping polars-masking wording from the retained
        # non-pandas substrate guards (the guards themselves stay as fail-closed
        # defence; the pandas path is unchanged).
        "src/decoy_engine/execution/_pipeline_routing.py",
        "src/decoy_engine/execution/_unified_slice_admission.py",
    }
    unexpected = [
        name
        for name in diff_names
        if "/execution/physical/" not in name
        # Polars masking removal (2026-09-21): the whole execution/polars/ adapter
        # tree was deleted; permit those deletions wholesale rather than enumerate.
        and "/execution/polars/" not in name
        and name not in permitted_non_physical
    ]
    assert not unexpected, (
        "Files under src/decoy_engine/execution changed versus origin/main outside "
        "execution/physical/ and outside the Task 4.5 permitted exceptions "
        f"({sorted(permitted_non_physical)}):\n  - " + "\n  - ".join(unexpected)
    )


# ---------------------------------------------------------------------------
# Task 4.3 addition: a DYNAMIC check, in a fresh subprocess, that importing
# `run_pipeline` never pulls `execution.physical` into `sys.modules`.
# ---------------------------------------------------------------------------

_FRESH_IMPORT_PROBE = (
    "import sys\n"
    "from decoy_engine.execution import run_pipeline\n"
    "assert callable(run_pipeline)\n"
    "hits = [name for name in sys.modules if name.startswith('decoy_engine.execution.physical')]\n"
    "print(','.join(sorted(hits)))\n"
)


def test_compile_physical_plan_is_unreachable_from_a_fresh_import_of_run_pipeline() -> None:
    """A fresh interpreter that imports only `decoy_engine.execution.
    run_pipeline` (production's real entry point) must never end up with any
    `decoy_engine.execution.physical*` module in `sys.modules` -- proof by
    dynamic observation, not source-text pattern matching, that nothing on
    the real import path reaches the compiler package."""
    result = subprocess.run(
        [sys.executable, "-c", _FRESH_IMPORT_PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    hits = [name for name in result.stdout.strip().split(",") if name]
    assert not hits, (
        "Importing decoy_engine.execution.run_pipeline in a fresh interpreter "
        "pulled in execution.physical module(s), which must stay unreachable "
        "from production until Task 4.5+:\n  - " + "\n  - ".join(hits)
    )
