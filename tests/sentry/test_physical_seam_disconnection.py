"""D4 disconnection proof, part 1/2: the physical-execution adapter seam
(Task 4.2, `decoy_engine.execution.physical`) is REACHED BY NOTHING in
production.

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
inputs`) imports from (`_pipeline_routing`, `_planner`, `_native_route`,
`_native_route_preflight`, ...), so the existing static regex sweep and the
exact-diff gate already re-verify 4.3's disconnection unmodified. The one
genuine addition below (`test_compile_physical_plan_is_unreachable_from_a_
fresh_import_of_run_pipeline`) is DYNAMIC rather than static: it proves, in
a fresh subprocess, that importing `decoy_engine.execution.run_pipeline`
never pulls `execution.physical` into `sys.modules` -- a complementary check
the regex sweep (which only reads source text) cannot make, since a hidden
dynamic/conditional import would not appear as a matchable `import`
statement at all.
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
    "execution/_native_route.py",
    "execution/_native_route_exec.py",
    "execution/_native_route_preflight.py",
    "execution/_native_route_digest.py",
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
    "execution/polars/_polars_adapter.py",
    "generation/_plan_entry.py",
    "generation/synthesize.py",
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
    outside the `execution/physical/` package itself may import it. Catches a
    future module this list has not been updated to name yet."""
    offenders: list[str] = []
    for path in ENGINE_ROOT.rglob("*.py"):
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        if rel.startswith("execution/physical/"):
            continue
        if _imports_physical(path):
            offenders.append(rel)
    assert not offenders, (
        "Modules outside execution/physical/ importing "
        "decoy_engine.execution.physical:\n  - " + "\n  - ".join(offenders)
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
    """Task 4.2 is additive: every file under `src/decoy_engine/execution`
    that existed before this task must be untouched. The only permitted diff
    is new files under `execution/physical/` (this task's package)."""
    # Diff against the MERGE-BASE, not origin/main's tip: if origin/main advances
    # with unrelated execution/ changes before this branch merges, a raw
    # origin/main..HEAD diff would raise a false positive. The merge-base is the
    # branch point, which is the correct baseline for "what THIS branch changed".
    base = _git("merge-base", "origin/main", "HEAD").strip()
    diff_names = _git(
        "diff", "--name-only", base, "HEAD", "--", "src/decoy_engine/execution"
    ).splitlines()
    unexpected = [name for name in diff_names if "/execution/physical/" not in name]
    assert not unexpected, (
        "Files under src/decoy_engine/execution changed versus origin/main outside "
        "the new execution/physical/ package (Task 4.2 must not touch production "
        "execution behavior):\n  - " + "\n  - ".join(unexpected)
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
