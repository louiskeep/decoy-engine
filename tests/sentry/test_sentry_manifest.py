"""AT-CI1 and AT-CI2: the local gate's sentry step collects every sentry in ``SENTRIES``.

``scripts/dev/gates.sh`` runs ``SENTRY_PATHS`` with its own ``-m`` filter. A sentry outside
those paths, or one a marker filter deselects, never runs at merge time, so this test reads
the same two settings from gates.sh and asks pytest what it would collect.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts" / "dev" / "gates.sh"

SENTRIES = (
    "tests/sentry/test_baseline_registry.py",
    "tests/sentry/test_bash32_compat.py",
    "tests/sentry/test_capability_matrix.py",
    "tests/sentry/test_cdf_threshold_ratchet.py",
    "tests/sentry/test_ci_config.py",
    "tests/sentry/test_ci_lock_installs.py",
    "tests/sentry/test_deprecation_expiry.py",
    "tests/sentry/test_dp_lock_row.py",
    "tests/sentry/test_engine_string_targets.py",
    "tests/sentry/test_csv_scan_single_copy.py",
    "tests/sentry/test_exception_hierarchy.py",
    "tests/sentry/test_expression_safety.py",
    "tests/sentry/test_f06_declared_casting.py",
    "tests/sentry/test_f06_route_matrix.py",
    "tests/sentry/test_file_access_boundary.py",
    "tests/sentry/test_file_access_entry_points.py",
    "tests/sentry/test_ga_stub_exports.py",
    "tests/sentry/test_license_policy.py",
    "tests/sentry/test_locator_hygiene.py",
    "tests/sentry/test_log_interpolation.py",
    "tests/sentry/test_methodology_citations.py",
    "tests/sentry/test_module_size.py",
    "tests/sentry/test_mypy_override_targets.py",
    "tests/sentry/test_native_release_workflow.py",
    "tests/sentry/test_no_residual_removed_surface.py",
    "tests/sentry/test_operator_registry_sentry.py",
    "tests/sentry/test_outbound_clients.py",
    "tests/sentry/test_package_map_paths.py",
    "tests/sentry/test_pandas_eval_sites.py",
    "tests/sentry/test_perf_claim_provenance.py",
    "tests/sentry/test_phase_containment.py",
    "tests/sentry/test_physical_seam_disconnection.py",
    "tests/sentry/test_private_files.py",
    "tests/sentry/test_proof_manifest.py",
    "tests/sentry/test_public_import_boundary.py",
    "tests/sentry/test_ratchet_check.py",
    "tests/sentry/test_reason_baseline_frozen.py",
    "tests/sentry/test_reason_minor_fixtures.py",
    "tests/sentry/test_reason_policy_single_source.py",
    "tests/sentry/test_reason_registry.py",
    "tests/sentry/test_run_result_constructor.py",
    "tests/sentry/test_script_call_kwargs.py",
    "tests/sentry/test_sdv_import_sentry.py",
    "tests/sentry/test_secret_material_census.py",
    "tests/sentry/test_secret_provider_semgrep.py",
    "tests/sentry/test_sentry_manifest.py",
    "tests/sentry/test_source_hygiene.py",
    "tests/sentry/test_unified_backend_map.py",
    "tests/sentry/test_validate_no_mutation.py",
    "tests/sentry/test_wave_b_no_registry_version_literal.py",
    "tests/sentry/defects/test_anchored_regex.py",
    "tests/sentry/defects/test_exception_context.py",
    "tests/sentry/defects/test_generate_option_reads.py",
    "tests/sentry/defects/test_validation_sites.py",
    "tests/sentry/parallel/test_serial_resources.py",
    "tests/sentry/parallel/test_stable_ids.py",
    "tests/sentry/profiling/test_profile_value_registry.py",
    "tests/sentry/profiling/test_snapshot_value_registry.py",
    "tests/sentry/source_config/test_source_dialect_choke_point.py",
    # Named sentries that live outside tests/sentry/.
    "tests/secret_providers/test_leak_sentry.py",
)


def gates_sentry_paths(text: str | None = None) -> list[str]:
    text = GATES.read_text(encoding="utf-8") if text is None else text
    m = re.search(r"^SENTRY_PATHS=\(([^)]*)\)", text, re.MULTILINE)
    assert m, "gates.sh has no SENTRY_PATHS array"
    return m.group(1).split()


def gates_marker_expr() -> str:
    m = re.search(r"^PYTEST_ARGS=\(-m '([^']*)'", GATES.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, "gates.sh PYTEST_ARGS has no -m expression"
    return m.group(1)


def uncovered(sentries: tuple[str, ...], paths: list[str]) -> list[str]:
    return [
        s for s in sentries if not any(s == p or s.startswith(p.rstrip("/") + "/") for p in paths)
    ]


def test_manifest_lists_every_sentry_file_and_no_ghost() -> None:
    on_disk = {
        p.relative_to(ROOT).as_posix() for p in (ROOT / "tests" / "sentry").glob("test_*.py")
    }
    listed = {s for s in SENTRIES if s.startswith("tests/sentry/")}
    assert on_disk - listed == set(), "sentry files missing from SENTRIES"
    assert [s for s in SENTRIES if not (ROOT / s).is_file()] == []
    assert len(set(SENTRIES)) == len(SENTRIES)


def test_gates_sentry_paths_cover_every_sentry() -> None:
    assert uncovered(SENTRIES, gates_sentry_paths()) == []


def test_removing_a_sentry_path_is_detected() -> None:
    """AT-CI2: dropping a path from SENTRY_PATHS makes the coverage check fail."""
    paths = gates_sentry_paths()
    assert paths, "SENTRY_PATHS is empty"
    for dropped in paths:
        rest = [p for p in paths if p != dropped]
        assert uncovered(SENTRIES, rest), f"dropping {dropped} went unnoticed"
    seeded = (*SENTRIES, "tests/policy/test_seeded.py")
    assert uncovered(seeded, paths) == ["tests/policy/test_seeded.py"]


@pytest.fixture(scope="module")
def collected() -> set[str]:
    cmd = [
        sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
        "-m", gates_marker_expr(), *gates_sentry_paths(),
    ]  # fmt: skip
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)  # noqa: S603 - fixed argv, no shell
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
    return {line.split("::")[0] for line in res.stdout.splitlines() if "::" in line}


def test_gates_step_collects_every_sentry(collected: set[str]) -> None:
    assert [s for s in SENTRIES if s not in collected] == []
