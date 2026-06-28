"""Invariant assertion library for the test-flight suite.

Each public function corresponds to one invariant family described in the
acceptance-testflight plan (sections 6.1-6.10). Phase 1 implements
check_distribution_mask and check_distribution_generate via the
_distribution sub-module; remaining families carry Phase 2+ stubs.

Naming convention: check_<family>(...) raises AssertionError on failure with a
message naming job/table/column/strategy so triage localises to one strategy.

Module split (LOW-4): distribution logic lives in testflight/_distribution.py.
This module re-exports the distribution entry points and FIXED_TS so existing
callers that import from testflight._invariants continue to work unchanged.
"""

from __future__ import annotations

from typing import Any

# Re-export distribution-fidelity functions and FIXED_TS so the teeth test
# module and any runner code can import from here without change.
from ._distribution import (
    FIXED_TS as FIXED_TS,
)
from ._distribution import (
    check_distribution_generate as check_distribution_generate,
)
from ._distribution import (
    check_distribution_mask as check_distribution_mask,
)
from ._spec import (
    ChecksumSpec,
    ComputedColumnSpec,
    FKIntegritySpec,
    QuarantineSpec,
    SafeHarborSpec,
    SentinelSpec,
)

__all__ = [
    "FIXED_TS",
    "check_checksums",
    "check_computed_columns",
    "check_determinism",
    "check_distribution_generate",
    "check_distribution_mask",
    "check_fk_integrity",
    "check_quarantine",
    "check_safe_harbor",
    "check_sentinels",
    "check_strategy_coverage",
]


# ---------------------------------------------------------------------------
# 6.1 Determinism
# ---------------------------------------------------------------------------


def check_determinism(
    job_name: str,
    result_a: Any,
    result_b: Any,
) -> None:
    """Assert that two pipeline runs produce byte-identical outputs.

    For every table in result_a.outputs, assert:
      result_a.outputs[t].to_pydict() == result_b.outputs[t].to_pydict()

    Also asserts that the in-pipeline quality_metrics["fidelity_reports"]
    blocks are byte-equal across both runs (the golden determinism idiom from
    tests/integration/golden/test_fidelity_report_golden.py).

    Args:
        job_name: Job name for error messages.
        result_a: First ExecutionResult.
        result_b: Second ExecutionResult.

    Raises:
        AssertionError: If any table output differs across runs.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_determinism")


# ---------------------------------------------------------------------------
# 6.4 FK integrity
# ---------------------------------------------------------------------------


def check_fk_integrity(
    job_name: str,
    spec: list[FKIntegritySpec],
    result: Any,
    sources: dict[str, Any],
) -> None:
    """Assert FK integrity for all declared relationships.

    For each RelationshipSpec: assert every non-null child key in the masked
    output exists in the parent's masked key set (no orphans except planted
    ones). Checks both the built-in engine validators (fk_intact,
    no_orphan_children via quality_metrics) AND a direct set-membership
    assertion (belt-and-suspenders). Composite tuples are matched as a unit.

    Args:
        job_name: Job name for error messages.
        spec: List of FKIntegritySpec from the manifest invariants.
        result: ExecutionResult (carries outputs and quality_metrics).
        sources: dict[table_name, pa.Table] of source frames.

    Raises:
        AssertionError: If orphan count does not match expected or FK is broken.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_fk_integrity")


# ---------------------------------------------------------------------------
# 6.5 Checksums
# ---------------------------------------------------------------------------


def check_checksums(
    job_name: str,
    spec: list[ChecksumSpec],
    result: Any,
) -> None:
    """Assert every output value in checksum columns satisfies validate(scheme, v).

    Calls decoy_engine.checksums.validate(scheme, value) per row in each
    declared (table, column) pair. A single failing value raises AssertionError
    naming job/table/column/scheme/value.

    Args:
        job_name: Job name for error messages.
        spec: List of ChecksumSpec from the manifest invariants.
        result: ExecutionResult carrying masked output tables.

    Raises:
        AssertionError: If any output value fails checksum validation.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_checksums")


# ---------------------------------------------------------------------------
# 6.6 Safe Harbor suppression
# ---------------------------------------------------------------------------


def check_safe_harbor(
    job_name: str,
    spec: list[SafeHarborSpec],
    result: Any,
) -> None:
    """Assert Safe Harbor suppression counts and absence of restricted ZIP3.

    For each SafeHarborSpec:
    - Assert no restricted ZIP3 prefix survives at zip5 resolution in the
      output (geo_generalize must have generalized or suppressed it).
    - Assert suppressed/generalized row count == planted_restricted_zip3_count.
    - Assert the geo_generalize_cascade QualityWarning is present with the
      expected aggregate count.

    Args:
        job_name: Job name for error messages.
        spec: List of SafeHarborSpec from the manifest invariants.
        result: ExecutionResult carrying masked outputs and quality_metrics warnings.

    Raises:
        AssertionError: If suppression counts mismatch or a restricted ZIP3 leaks.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_safe_harbor")


# ---------------------------------------------------------------------------
# 6.7 Quarantine counts
# ---------------------------------------------------------------------------


def check_quarantine(
    job_name: str,
    spec: QuarantineSpec,
    result: Any,
) -> None:
    """Assert quarantine row count and file presence.

    Asserts:
    - result.quality_metrics["quarantine"].total_quarantined == expected.
    - Main output row count reduced by exactly expected_total_quarantined.
    - JSONL quarantine file exists with matching line count.
    - quality_metrics["validation"]["validators"] reports expected_validator failing.

    Args:
        job_name: Job name for error messages.
        spec: QuarantineSpec from the manifest invariants.
        result: ExecutionResult carrying quality_metrics and output tables.

    Raises:
        AssertionError: If quarantine count mismatches or file is absent.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_quarantine")


# ---------------------------------------------------------------------------
# 6.8 Sentinel no-leakage
# ---------------------------------------------------------------------------


def check_sentinels(
    job_name: str,
    spec: list[SentinelSpec],
    result: Any,
) -> None:
    """Assert planted raw-PII sentinel strings are absent from all output.

    Scans EVERY column of EVERY output table (not just the planted column) for
    each sentinel value as an exact string and as a substring. A hit means a
    column was accidentally left on passthrough or masking did not apply.

    Args:
        job_name: Job name for error messages.
        spec: List of SentinelSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Raises:
        AssertionError: If any sentinel string appears in any output column.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_sentinels")


# ---------------------------------------------------------------------------
# 6.9 Computed-column correctness
# ---------------------------------------------------------------------------


def check_computed_columns(
    job_name: str,
    spec: list[ComputedColumnSpec],
    result: Any,
) -> None:
    """Assert derived / case_when / derived_aggregate columns are correct.

    For each ComputedColumnSpec, recomputes the expected value in pure Python
    from the output's input columns and asserts equality. For case_when columns
    with branch_count > 0, also asserts that every branch is exercised by at
    least one output row (branch-coverage guard: an unused branch could hide a
    bug). For derived_aggregate, asserts the single scalar equals the Python
    aggregate of the sibling column broadcast to all rows.

    Args:
        job_name: Job name for error messages.
        spec: List of ComputedColumnSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Raises:
        AssertionError: If a computed value is wrong or a branch is unexercised.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_computed_columns")


# ---------------------------------------------------------------------------
# 6.10 Strategy coverage guard
# ---------------------------------------------------------------------------


def check_strategy_coverage(
    job_name: str,
    declared: list[str],
) -> None:
    """Assert all declared strategies are present in the live SCALAR_HANDLERS registry.

    At Phase 4, the suite-level coverage guard (run once across all jobs) will
    assert that the union of declared strategies covers SCALAR_HANDLERS minus a
    documented allowlist. This per-job function asserts that each strategy name
    declared in strategy_coverage exists in SCALAR_HANDLERS so a typo in the
    manifest fails loudly.

    Args:
        job_name: Job name for error messages.
        declared: List of strategy names from InvariantSpec.strategy_coverage.

    Raises:
        AssertionError: If a declared strategy is not in SCALAR_HANDLERS.
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: check_strategy_coverage")
