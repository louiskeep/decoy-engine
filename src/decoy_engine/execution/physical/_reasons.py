"""Frozen DECISION-code catalog for the physical-plan compiler (Task 4.3, D3).

Design doc: docs/plans/2026-09-13-physical-plan-design.md section 10 (Appendix
A) names the target catalog; the plan (TASK-4.3-PLAN.md D3) draws the
boundary between families:

  * compile/admission DECISION codes -> the compiler EMITS these; D4 asserts
    bidirectional exact equality against the live gates (missing OR extra
    fails).
  * runtime execution/publication codes -> diagnostic OBLIGATIONS on the
    plan, never chosen-route reasons; excluded from equality (out of scope
    for 4.3, which never executes).
  * SUBMIT-BOUNDARY validation codes (`invalid_substrate`,
    `invalid_execution_knob`, raised by `_substrate.py` BEFORE compilation on
    the raw args) -> excluded: the compiler receives resolved+validated
    inputs and cannot emit them.
  * KNOB-RESOLUTION code `out_of_core_reorder_threshold_invalid`
    (`out_of_core/_route_policy.resolve_reorder_threshold_rows`, runs before
    profiling/compilation) -> same pre-compilation exclusion.
  * SNAPSHOT-ACQUISITION code `out_of_core_memory_detection_failed`
    (raised while building the resolved-OOC facts D1's snapshot captures,
    i.e. `out_of_core._budget.resolve_budget`) -> owned by the
    snapshot-acquisition boundary, not the compiler.
  * Layer-1 forced-mode failures raise a plain UNCODED `ConfigError` on
    main -> excluded from code-for-code catalog membership; D4 instead
    asserts exact normalized BRANCH IDENTITY (exception type + which forced
    branch), since several forced branches share the bare `ConfigError` type.

Some live reason strings are prose, not a bare stable code (`_planner.py`'s
`_polars_native_rejection` / `_chunked_rejection` build human-readable,
semicolon-joined sentences). Appendix A names the INTENDED target codes for
these; part of 4.3's job (design doc section 12 punch-list) is completing the
translation from live prose to those stable codes -- `translate_polars_
rejection` / `translate_chunked_rejection` below do that, by exact pattern
match against the literal f-string templates `_planner.py` emits today. A
part that matches no known template comes back tagged
`unclassified_<family>_rejection:<text>`, which the D4 harness asserts never
fires across the acceptance corpus -- a catalog-completeness defect to fix,
never a silent add (design doc section 10, closing line).
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Layer-1 route-reason tokens (`_pipeline_routing.py`), emitted verbatim by
# `decide_execution_route` / `_sequential_eligible` -- already stable single
# tokens in production, no translation needed.
# ---------------------------------------------------------------------------

ROUTE_NO_RELATIONSHIPS: Final = "no_relationships"
ROUTE_GENERATE_PLUS_MASK: Final = "generate_plus_mask"
ROUTE_VALIDATORS_PRESENT: Final = "validators_present"
ROUTE_FIDELITY_REPORT_REQUESTED: Final = "fidelity_report_requested"
ROUTE_VAULT_WRITER_REQUESTED: Final = "vault_writer_requested"
ROUTE_NON_PANDAS_SUBSTRATE_REQUESTED: Final = "non_pandas_substrate_requested"
ROUTE_PURE_MASK_FK: Final = "pure_mask_fk"
ROUTE_OVERRIDE_FULL_FRAME: Final = "override_full_frame"
ROUTE_OVERRIDE_OUT_OF_CORE: Final = "override_out_of_core"
ROUTE_BYTE_ESTIMATE_FULL_FRAME_FITS: Final = "byte_estimate_full_frame_fits"
ROUTE_PROBE_RECOVERED_FULL_FRAME: Final = "probe_recovered_full_frame"
ROUTE_BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE: Final = "byte_estimate_bounded_out_of_core"
ROUTE_OUT_OF_CORE_LARGE_FK: Final = "out_of_core_large_fk"
ROUTE_CROSS_TABLE_CYCLE: Final = "cross_table_cycle"

ROUTE_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        ROUTE_NO_RELATIONSHIPS,
        ROUTE_GENERATE_PLUS_MASK,
        ROUTE_VALIDATORS_PRESENT,
        ROUTE_FIDELITY_REPORT_REQUESTED,
        ROUTE_VAULT_WRITER_REQUESTED,
        ROUTE_NON_PANDAS_SUBSTRATE_REQUESTED,
        ROUTE_PURE_MASK_FK,
        ROUTE_OVERRIDE_FULL_FRAME,
        ROUTE_OVERRIDE_OUT_OF_CORE,
        ROUTE_BYTE_ESTIMATE_FULL_FRAME_FITS,
        ROUTE_PROBE_RECOVERED_FULL_FRAME,
        ROUTE_BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE,
        ROUTE_OUT_OF_CORE_LARGE_FK,
        ROUTE_CROSS_TABLE_CYCLE,
    }
)

# ExecutionError codes `decide_execution_route` raises for the fail-closed
# reject-before-read branch. Compile/admission codes: the compiler's
# `layer1_route` propagates these verbatim (it calls the live function
# directly), so D4 equality is definitional.
EXC_FK_FULL_FRAME_OOM_RISK_REJECTED: Final = "fk_full_frame_oom_risk_rejected"
EXC_FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED: Final = "fk_full_frame_oom_risk_rejected_estimated"

EXECUTION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {EXC_FK_FULL_FRAME_OOM_RISK_REJECTED, EXC_FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED}
)

# ---------------------------------------------------------------------------
# Forced-mode branch identities: NOT codes (production raises a bare, uncoded
# `ConfigError`). D4 asserts exact normalized branch identity instead of a
# code -- see this module's docstring.
# ---------------------------------------------------------------------------

FORCED_OUT_OF_CORE_NO_MASK_TABLE: Final = "forced_out_of_core_no_mask_table"
FORCED_OUT_OF_CORE_INELIGIBLE: Final = "forced_out_of_core_ineligible"
FORCED_OUT_OF_CORE_INCOMPATIBLE: Final = "forced_out_of_core_incompatible"
FORCED_SEQUENTIAL_INELIGIBLE: Final = "forced_sequential_ineligible"
FORCED_SEQUENTIAL_CYCLIC: Final = "forced_sequential_cyclic"
FORCED_SEQUENTIAL_NO_MASK_TABLE: Final = "forced_sequential_no_mask_table"

FORCED_MODE_BRANCH_IDENTITIES: Final[frozenset[str]] = frozenset(
    {
        FORCED_OUT_OF_CORE_NO_MASK_TABLE,
        FORCED_OUT_OF_CORE_INELIGIBLE,
        FORCED_OUT_OF_CORE_INCOMPATIBLE,
        FORCED_SEQUENTIAL_INELIGIBLE,
        FORCED_SEQUENTIAL_CYCLIC,
        FORCED_SEQUENTIAL_NO_MASK_TABLE,
    }
)

# ---------------------------------------------------------------------------
# Compiler-assigned driver-selection codes: D2's own narrowing decisions
# beyond Layer 1's already-coded tokens (native admission success has no
# live "reason" string -- `NativeBatchAdmission.reason`/`RouteAdmission.
# reason` are None on the admitted path -- so the compiler names its own
# positive-admission code; likewise "chunked was admitted" has no single
# stable code in `_planner.ExecutionPlan.reason`, which is prose).
# ---------------------------------------------------------------------------

DRIVER_REASON_NATIVE_ADMITTED: Final = "native_admission_admitted"
DRIVER_REASON_CHUNKED_ADMITTED: Final = "chunked_admitted"

DRIVER_SELECTION_CODES: Final[frozenset[str]] = frozenset(
    {DRIVER_REASON_NATIVE_ADMITTED, DRIVER_REASON_CHUNKED_ADMITTED}
)

# ---------------------------------------------------------------------------
# Pre-compilation / snapshot-acquisition EXCLUSIONS: documented, never
# emitted by the compiler (D3 boundary notes above).
# ---------------------------------------------------------------------------

PRECOMPILATION_EXCLUDED_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_substrate",
        "invalid_execution_knob",
        "out_of_core_reorder_threshold_invalid",
        "out_of_core_memory_detection_failed",
    }
)

# ---------------------------------------------------------------------------
# Native-lane admission codes (`_native_route.py` / `_native_route_preflight.
# py`). Parameterized with a `:<detail>` suffix; classified by the prefix
# before the first `:`. The static/scan split mirrors the two live call
# sites (`static_candidacy` vs. `classify_and_preflight`/`peek_and_admit`);
# `unsupported_projection` / `non_utf8_column` / `zero_row_source` are
# produced by BOTH `peek_and_admit` (utf8 lane) and `classify_and_preflight`
# (widened lane's own projection/type gate before the preflight scan), so
# they live in the shared "scan" family rather than being pinned to one.
# ---------------------------------------------------------------------------

NATIVE_STATIC_CODE_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "execution_mode_not_auto",
        "non_pandas_substrate",
        "generation_table_present",
        "multi_table_job",
        "fk_relationship_present",
        "source_loader_present",
        "non_lazy_source",
        "fidelity_report_requested",
        "validators_present",
        "quarantine_configured",
        "unsupported_sink",
        "no_columns_configured",
        "invalid_column_config",
        "vault_column",
        "unsupported_strategy",
        "native_route_disabled_or_no_mask_table",
    }
)

NATIVE_SCAN_CODE_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "zero_row_source",
        "unsupported_projection",
        "non_utf8_column",
        "native_preflight_schema_drift",
        "native_preflight_reroute",
        "native_preflight_strategy_unresolved",
        "native_source_snapshot_digest_mismatch",
        "native_chunk_schema_drift",
        "columns_changed",
        "type_changed",
    }
)


def native_reason_code_family(reason: str) -> str:
    """Classify a captured native-admission `reason` string into its D3
    catalog family by prefix (codes may carry a `:<detail>` suffix).

    Returns `"static"`, `"scan"`, or `"unknown"` -- the D4 harness asserts
    `"unknown"` never appears across the acceptance corpus: an unclassified
    reason is a catalog-completeness defect (design doc section 10), never a
    silently-accepted new code.
    """
    prefix = reason.split(":", 1)[0]
    if prefix in NATIVE_STATIC_CODE_PREFIXES:
        return "static"
    if prefix in NATIVE_SCAN_CODE_PREFIXES:
        return "scan"
    return "unknown"


# ---------------------------------------------------------------------------
# Planner-prose translators (`_planner.py`'s `_polars_native_rejection` /
# `_chunked_rejection`): live output is one sentence per applicable reason,
# `"; ".join()`-ed together -- but several of the INDIVIDUAL reason sentences
# ALSO contain their own internal `"; "` (e.g. `"chunked execution masks one
# table per run; job declares 2 mask tables (...)"` is ONE reason, not two),
# so splitting on `"; "` cannot reliably tell an inter-reason boundary from an
# intra-reason one. Scanning the whole joined string for each KNOWN reason
# template (by regex `search`, not a positional split) sidesteps that
# ambiguity: every literal template below is copied verbatim from `_planner.
# py`'s f-strings. A `snake_case_code: ` token immediately after `"; "` or at
# the string's start (`_CODE_PREFIX_RE`) generically captures every reason
# `_planner.py` already writes with its own leading code (re-raised
# `check_chunked_compatibility` codes included), without re-enumerating them
# by name -- so a new one added to `_chunked.py` is picked up for free rather
# than silently miscategorized.
#
# This is a best-effort EXTRACTION (every known reason present is found), not
# an exact segmentation of the joined string: `translate_*` returns
# `("unclassified_<family>_rejection:<prose>",)` only when NOTHING matched at
# all, which is the case that actually matters for catalog completeness (a
# wholly new, unrecognized reason kind) -- see this module's docstring.
# ---------------------------------------------------------------------------

_CODE_PREFIX_RE: Final = re.compile(r"(?:^|; )([a-z][a-z0-9_]+): ")

_POLARS_SUBSTRATE_RE: Final = re.compile(
    r"resolved substrate is '([^']*)'; the polars-native loop requires"
)
_POLARS_NONNATIVE_RE: Final = re.compile(r"non-polars-native work: ([^;]+)")

CODE_NO_MASK_WORK: Final = "no_mask_work"
CODE_FK_RESOLUTION: Final = "fk_resolution"

_POLARS_PATTERNS: Final[tuple[re.Pattern[str], str]] = (
    (
        re.compile(r"no mask-kind work; the polars-native loop masks existing data"),
        CODE_NO_MASK_WORK,
    ),
    (re.compile(r"fk_resolution: FK edges route through the pandas oracle"), CODE_FK_RESOLUTION),
)


def translate_polars_rejection(prose: str) -> tuple[str, ...]:
    """Translate `_polars_native_rejection`'s live prose into Appendix A's
    `polars_native` code family: `no_mask_work` / `substrate_is:<s>` /
    `fk_resolution` / `non_polars_native_work:<strategies>`."""
    codes: list[str] = []
    for pattern, code in _POLARS_PATTERNS:
        if pattern.search(prose):
            codes.append(code)
    match = _POLARS_SUBSTRATE_RE.search(prose)
    if match is not None:
        codes.append(f"substrate_is:{match.group(1)}")
    match = _POLARS_NONNATIVE_RE.search(prose)
    if match is not None:
        codes.append(f"non_polars_native_work:{match.group(1)}")
    if not codes:
        codes.append(f"unclassified_polars_rejection:{prose}")
    return tuple(codes)


_CHUNKED_SUBSTRATE_RE: Final = re.compile(
    r"resolved substrate is '([^']*)'; the chunked route constructs"
)

CODE_NO_MASK_TABLES: Final = "no_mask_tables"
CODE_GENERATE_TABLES_PRESENT: Final = "generate_tables_present"
CODE_MASKS_ONE_TABLE_PER_RUN: Final = "masks_one_table_per_run"
CODE_NON_SCALAR_COMPOSITE: Final = "non_scalar_composite"
CODE_FPE_JOIN_GROUP: Final = "fpe_join_group"
CODE_CHUNKED_EXTRA_SOURCE_FRAME: Final = "chunked_extra_source_frame"
CODE_CHUNKED_SOURCE_FRAME_MISSING: Final = "chunked_source_frame_missing"
CODE_CHUNKED_SOURCE_BELOW_THRESHOLD: Final = "chunked_source_below_threshold"
CODE_CHUNKED_SOURCE_DTYPE_UNSTABLE: Final = "chunked_source_dtype_unstable"
CODE_CHUNKED_LAZY_SOURCE_UNSUPPORTED: Final = "chunked_lazy_source_unsupported"

_CHUNKED_PATTERNS: Final[tuple[re.Pattern[str], str]] = (
    (re.compile(r"no mask-kind tables to stream"), CODE_NO_MASK_TABLES),
    (
        re.compile(
            r"generate-kind table\(s\) .*? present; chunked execution masks existing data "
            r"and has no generation mode"
        ),
        CODE_GENERATE_TABLES_PRESENT,
    ),
    (
        re.compile(r"chunked execution masks one table per run; job declares \d+ mask tables"),
        CODE_MASKS_ONE_TABLE_PER_RUN,
    ),
    (re.compile(r"non-scalar \(composite bundle\) work on table"), CODE_NON_SCALAR_COMPOSITE),
    (re.compile(r"fpe_join_group on column\(s\)"), CODE_FPE_JOIN_GROUP),
    (re.compile(r"extra loaded source frame\(s\)"), CODE_CHUNKED_EXTRA_SOURCE_FRAME),
    (re.compile(r"no loaded source frame for table"), CODE_CHUNKED_SOURCE_FRAME_MISSING),
    (
        re.compile(r"source holds \d+ rows, below the auto-chunk threshold"),
        CODE_CHUNKED_SOURCE_BELOW_THRESHOLD,
    ),
    (
        re.compile(r"column\(s\) with non-chunk-stable pandas round-trip dtypes"),
        CODE_CHUNKED_SOURCE_DTYPE_UNSTABLE,
    ),
    (
        re.compile(r"source for table .*? is a lazy \(LazySource\) handle"),
        CODE_CHUNKED_LAZY_SOURCE_UNSUPPORTED,
    ),
)


def translate_chunked_rejection(prose: str) -> tuple[str, ...]:
    """Translate `_chunked_rejection`'s live prose into stable codes (see the
    section docstring above for the scanning strategy)."""
    codes: list[str] = []
    for pattern, code in _CHUNKED_PATTERNS:
        if pattern.search(prose):
            codes.append(code)
    match = _CHUNKED_SUBSTRATE_RE.search(prose)
    if match is not None:
        codes.append(f"substrate_is:{match.group(1)}")
    codes.extend(m.group(1) for m in _CODE_PREFIX_RE.finditer(prose))
    if not codes:
        codes.append(f"unclassified_chunked_rejection:{prose}")
    return tuple(codes)


# ---------------------------------------------------------------------------
# Relationship deferral reasons (design doc section 12 punch-list: "name
# stable codes for... the relationship deferral reasons"). Imported directly
# from `_planner.py` for exact-string matching rather than duplicating the
# literal text (avoids drift if that module's wording changes).
# ---------------------------------------------------------------------------

CODE_RELATIONSHIP_ROUTE_DEFERRED: Final = "relationship_route_deferred"
CODE_NO_RELATIONSHIP_ROUTE: Final = "no_relationship_route"


def translate_relationship_mode_reason(prose: str) -> str:
    """Translate `_planner.py`'s `sequential_relationship` /
    `out_of_core_relationship` rejection text (either `RELATIONSHIP_ROUTE_
    DEFERRED` or the private `_NO_RELATIONSHIP_ROUTE` constant) into a
    stable code."""
    from decoy_engine.execution import _planner

    if prose == _planner.RELATIONSHIP_ROUTE_DEFERRED:
        return CODE_RELATIONSHIP_ROUTE_DEFERRED
    if prose == _planner._NO_RELATIONSHIP_ROUTE:
        return CODE_NO_RELATIONSHIP_ROUTE
    return f"unclassified_relationship_mode_reason:{prose}"


__all__ = [
    "CODE_CHUNKED_EXTRA_SOURCE_FRAME",
    "CODE_CHUNKED_SOURCE_BELOW_THRESHOLD",
    "CODE_CHUNKED_SOURCE_DTYPE_UNSTABLE",
    "CODE_CHUNKED_SOURCE_FRAME_MISSING",
    "CODE_FK_RESOLUTION",
    "CODE_FPE_JOIN_GROUP",
    "CODE_GENERATE_TABLES_PRESENT",
    "CODE_MASKS_ONE_TABLE_PER_RUN",
    "CODE_NON_SCALAR_COMPOSITE",
    "CODE_NO_MASK_TABLES",
    "CODE_NO_MASK_WORK",
    "CODE_NO_RELATIONSHIP_ROUTE",
    "CODE_RELATIONSHIP_ROUTE_DEFERRED",
    "DRIVER_REASON_CHUNKED_ADMITTED",
    "DRIVER_REASON_NATIVE_ADMITTED",
    "DRIVER_SELECTION_CODES",
    "EXC_FK_FULL_FRAME_OOM_RISK_REJECTED",
    "EXC_FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED",
    "EXECUTION_ERROR_CODES",
    "FORCED_MODE_BRANCH_IDENTITIES",
    "FORCED_OUT_OF_CORE_INCOMPATIBLE",
    "FORCED_OUT_OF_CORE_INELIGIBLE",
    "FORCED_OUT_OF_CORE_NO_MASK_TABLE",
    "FORCED_SEQUENTIAL_CYCLIC",
    "FORCED_SEQUENTIAL_INELIGIBLE",
    "FORCED_SEQUENTIAL_NO_MASK_TABLE",
    "NATIVE_SCAN_CODE_PREFIXES",
    "NATIVE_STATIC_CODE_PREFIXES",
    "PRECOMPILATION_EXCLUDED_CODES",
    "ROUTE_BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE",
    "ROUTE_BYTE_ESTIMATE_FULL_FRAME_FITS",
    "ROUTE_CROSS_TABLE_CYCLE",
    "ROUTE_FIDELITY_REPORT_REQUESTED",
    "ROUTE_GENERATE_PLUS_MASK",
    "ROUTE_NON_PANDAS_SUBSTRATE_REQUESTED",
    "ROUTE_NO_RELATIONSHIPS",
    "ROUTE_OUT_OF_CORE_LARGE_FK",
    "ROUTE_OVERRIDE_FULL_FRAME",
    "ROUTE_OVERRIDE_OUT_OF_CORE",
    "ROUTE_PROBE_RECOVERED_FULL_FRAME",
    "ROUTE_PURE_MASK_FK",
    "ROUTE_REASON_CODES",
    "ROUTE_VALIDATORS_PRESENT",
    "ROUTE_VAULT_WRITER_REQUESTED",
    "native_reason_code_family",
    "translate_chunked_rejection",
    "translate_polars_rejection",
    "translate_relationship_mode_reason",
]
