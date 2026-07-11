"""Manifest data model for the test-flight acceptance suite.

Extends the golden-fixture manifest conventions in
tests/fixtures/golden/_manifest_schema.py with the richer JobSpec and
InvariantSpec needed by the multi-job acceptance harness.

All Pydantic models use extra="forbid" so an unknown YAML key raises
ValidationError immediately (fail-closed on schema drift). Call
`load_manifest(path)` to get a validated `FlightManifest`; it raises
`ValueError` with a full validation trace on any schema violation.

Source pattern: same as tests/fixtures/golden/_manifest_schema.py (dbt
manifest.json schema convention, Pydantic extra="forbid", Literal for closed
string enums).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ---------------------------------------------------------------------------
# Closed-string literals used across multiple models
# ---------------------------------------------------------------------------

OrphanPolicyLiteral = Literal["preserve", "remap", "warn", "fail"]

TableKindLiteral = Literal["mask", "generate"]

TopologyLiteral = Literal[
    "one_to_many_multilevel",
    "many_to_many_junction",
    "self_referential",
    "composite_key",
    "mixed",
]

DistributionClassLiteral = Literal["preserve", "coarsen", "synthetic", "derived"]

ChecksumSchemeLiteral = Literal["luhn", "npi", "iban", "vin", "isbn13", "ean13", "gtin"]


# ---------------------------------------------------------------------------
# TableSpec -- one table in the job
# ---------------------------------------------------------------------------


class TableSpec(BaseModel):
    """Declaration of one table participating in the test-flight job.

    `source_builder` is a dotted reference into the job's fixture.py module
    (e.g. "fixture.build_members") that the runner resolves at Phase 2. The
    value "csv:<relative-path>" loads a committed CSV instead.

    Generate tables (kind="generate") have no source and omit source_builder.
    The engine builds their rows from the generate_columns spec in the config.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: TableKindLiteral
    row_count: int = Field(ge=1)
    # Required for mask tables; None for generate tables (no source to build).
    source_builder: str | None = Field(
        default=None,
        description="Dotted fixture function ref or 'csv:<path>'. None for generate tables.",
    )

    @model_validator(mode="after")
    def _source_builder_required_for_mask(self) -> TableSpec:
        if self.kind == "mask" and not self.source_builder:
            raise ValueError(
                f"TableSpec {self.name!r}: kind='mask' requires a source_builder. "
                "Generate tables (kind='generate') may omit source_builder."
            )
        return self


# ---------------------------------------------------------------------------
# RelationshipEndSpec / RelationshipSpec -- mirrors engine RelationshipConfig
# ---------------------------------------------------------------------------


class RelationshipEndSpec(BaseModel):
    """One side of a FK relationship (parent or child).

    Mirrors engine RelationshipEnd / RelationshipEdge. `columns` is a list so
    composite-key FKs (length > 1) and single-column FKs (length 1) share the
    same model.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    columns: list[str] = Field(min_length=1)


class RelationshipSpec(BaseModel):
    """One FK relationship: parent table + one or more children.

    `orphan_policy` is required (mirrors the engine S2 resolution that every
    declared relationship must name its policy explicitly). `namespace` groups
    columns for coherent masking across the relationship.
    """

    model_config = ConfigDict(extra="forbid")

    parent: RelationshipEndSpec
    children: list[RelationshipEndSpec] = Field(min_length=1)
    orphan_policy: OrphanPolicyLiteral
    namespace: str | None = None


# ---------------------------------------------------------------------------
# InvariantSpec sub-models
# ---------------------------------------------------------------------------


class FKIntegritySpec(BaseModel):
    """Per-relationship FK integrity expectation.

    `relationship_name` matches the `namespace` of the RelationshipSpec (used
    as the human-readable key in the evidence report). `expected_orphans` is
    the exact count of orphaned child rows the fixture plants; the runner
    asserts the output count matches exactly (so a bug that creates extra
    orphans fails).
    """

    model_config = ConfigDict(extra="forbid")

    relationship_name: str
    expected_orphans: int = Field(ge=0, default=0)
    policy: OrphanPolicyLiteral


class ColumnDistributionSpec(BaseModel):
    """Per-column distribution-fidelity expectation.

    `distribution_class` drives which quality checks apply:
    - preserve: cardinality-preserving (fpe, hash); high value-identity expected.
    - coarsen: intentional cardinality drop (bucketize, geo_generalize,
      bucket_perturb); assert out_nunique < src_nunique (real-coarsening guard).
    - synthetic: no source dependency (faker, categorical); value-identity
      irrelevant, shape checked.
    - derived: computed from other output columns (derived, case_when,
      derived_aggregate); checked via computed_columns invariant, not quality score.

    `joint_columns` is a list of (col_a, col_b) pairs to pass to
    compute_quality_report's joint_columns argument (pairwise correlation check).
    Declaring pairs is mandatory for multi-column preserve tables; undeclared pairs
    are explicitly out of scope. Set `joints_waived=True` with a reason on any
    column entry to explicitly opt out when correlation is not required for the table.

    `corr_tol` applies only when this column appears in a joint pair; it is the
    minimum TVD-based similarity the pair must achieve (default 0.90, floor 0.50).
    Values below 0.50 are degenerate: they would not catch gross decorrelation.

    `strategy` is the raw strategy name (fpe, hash, shuffle, bucketize, etc.)
    used to build the strategy_map for apply_quality_policy and to distinguish
    cardinality-bijective strategies (fpe/hash) from marginal-preserving ones
    (shuffle) in the constant-collapse guard. Optional; columns without a declared
    strategy are excluded from the policy's per-strategy floor check but still
    checked by the explicit teeth (cardinality, null-rate, coarsening).

    `null_pp` is the per-column null-rate drift tolerance in percentage points.
    The explicit null-rate tooth asserts abs(null_rate_out - null_rate_in) <= null_pp.
    Default 10.0 pp (matches compute_quality_report's null_drift_threshold_pp default).
    Values above 25.0 are degenerate: they would not catch gross null-rate inflation.

    `joints_waived` opts this table out of the multi-column joint-pair requirement
    (MEDIUM-2). Must be set on at least one column entry for the table. Requires
    `joints_waived_reason` to be non-empty so the opt-out is reviewable.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    distribution_class: DistributionClassLiteral
    strategy: str | None = None
    tolerance: float = Field(ge=0.0, le=1.0, default=0.05)
    null_pp: float = Field(ge=0.0, le=25.0, default=10.0)
    joint_columns: list[list[str]] = Field(default_factory=list)
    corr_tol: float = Field(ge=0.5, le=1.0, default=0.90)
    expected_coarsening: bool = False
    joints_waived: bool = False
    joints_waived_reason: str | None = None

    @model_validator(mode="after")
    def _require_waiver_reason(self) -> ColumnDistributionSpec:
        # The waiver silences the correlation tooth; it must carry a reviewable
        # reason. Enforce what the docstring promises (a reason-less waiver is a
        # silent opt-out from a teeth check).
        if self.joints_waived and not (self.joints_waived_reason or "").strip():
            raise ValueError(
                "joints_waived=True requires a non-empty joints_waived_reason "
                "so the correlation opt-out is reviewable"
            )
        return self


class ChecksumSpec(BaseModel):
    """Per-column checksum-validity expectation.

    Every output row in `table.column` must satisfy
    `decoy_engine.checksums.validate(scheme, value) == True`. This covers
    fpe-checksum columns (luhn, npi, iban, vin, isbn13, ean13, gtin).
    NDC and ICD codes have no check-digit scheme; use `sentinels` or
    `computed_columns` to validate their structural form instead.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    scheme: ChecksumSchemeLiteral


class SafeHarborSpec(BaseModel):
    """Safe Harbor (45 CFR 164.514(b)(2)) suppression expectation.

    `planted_restricted_zip3_count` is the exact number of source rows with a
    ZIP3 prefix that falls below the HIPAA_K_THRESHOLD = 20,000 threshold.
    `expected_suppressions` is the count the runner asserts are suppressed or
    generalized past ZIP-5 in the output. The runner also asserts the
    geo_generalize_cascade QualityWarning is present with a matching count.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    planted_restricted_zip3_count: int = Field(ge=0)
    expected_suppressions: int = Field(ge=0)


class QuarantineSpec(BaseModel):
    """Per-job quarantine expectation.

    `planted_bad_row_count` is the number of fixture rows that will fail the
    declared validator, matching `expected_total_quarantined` in the quarantine
    summary. The runner asserts the main output row count is reduced by exactly
    `expected_total_quarantined` and the JSONL quarantine file has the same
    line count.
    """

    model_config = ConfigDict(extra="forbid")

    planted_bad_row_count: int = Field(ge=0)
    expected_total_quarantined: int = Field(ge=0)
    expected_validator: str = Field(description="Validator name, e.g. 'luhn'.")


class SentinelSpec(BaseModel):
    """A planted raw-PII string that must NOT appear anywhere in the output.

    After the pipeline runs, the runner scans EVERY column of EVERY output
    table for this exact string (and as a substring). Any match is a failure,
    indicating a column was accidentally left on passthrough or a masking
    strategy did not apply.
    """

    model_config = ConfigDict(extra="forbid")

    table: str = Field(description="Source table where the sentinel is planted.")
    column: str = Field(description="Source column where the sentinel is planted.")
    value: str = Field(description="The raw PII string that must be absent from output.")


class ChapterPreserveSpec(BaseModel):
    """Assertion that a code_set column with chapter_preserve:true keeps its chapter.

    After masking, the first character of every output code must equal the first
    character of the corresponding source code. This verifies the SP-09/SP-11
    chapter_preserve feature end-to-end through run_pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    table: str = Field(description="Table containing the code_set column.")
    column: str = Field(description="The code_set column (e.g. 'diagnosis').")


class ComputedColumnSpec(BaseModel):
    """Correctness expectation for a derived / case_when / derived_aggregate column.

    `formula` is a human-readable description of the computation for the report
    (not evaluated programmatically; the runner re-implements it in pure Python
    per invariant-family 6.9). `branch_count` is the number of case_when branches
    expected to be exercised by at least one output row; 0 means no branch-
    coverage check (used for derived_aggregate where there are no branches).
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    formula: str = Field(description="Human-readable expected formula (for the report).")
    branch_count: int = Field(ge=0, default=0)


class JointMaskConsistencySpec(BaseModel):
    """SP-08 consistency assertion for a joint_mask output column group.

    Each output row's tuple of values for ``columns`` must be an actual row
    in the reference table (not a Frankenstein combination of values from
    different rows). Consistency is guaranteed by the joint_mask transform
    because it writes ALL columns from a single selected reference-table row.

    Fields:
        table: Output table that contains the joint_mask columns.
        columns: The column names written by the joint_mask group (e.g.
            ``["city", "state"]``).
        reference: The shipped reference table name (e.g.
            ``"us_zip5_city_state"``).
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    columns: list[str] = Field(min_length=2)
    reference: str


class MaskedCorrelationSpec(BaseModel):
    """Relabel-invariant association check for a value-changing masked column pair.

    Declares two columns that are both masked by a value-changing strategy
    (fpe, code_set, hash, etc.). The harness computes Cramers V on BOTH the
    source pair and the masked output pair and asserts::

        abs(v_out - v_src) <= tol

    Cramers V depends on contingency COUNTS not value labels, so an FPE
    bijection on both columns yields v_out == v_src exactly (the bijection
    preserves pair counts). The old engine crosstab-TVD metric scores such pairs
    at ~0.0 because the value labels are disjoint after relabeling, making it
    unable to distinguish a faithfully-masked pair from a decorrelated one.

    Phase 3c (2026-06-29): this closes the masked-correlation carry-forward
    documented in docs/what-we-cannot-prove.md for categorical / low-cardinality
    column pairs masked by bijective strategies. High-cardinality continuous
    pairs remain out of scope (Cramers V requires binning for continuous data).

    Fields:
        table: Table name containing both columns.
        col_a: First column in the correlated pair.
        col_b: Second column in the correlated pair.
        strategy_a: Masking strategy applied to col_a (for error messages).
        strategy_b: Masking strategy applied to col_b (for error messages).
        tol: Maximum allowed absolute difference |v_out - v_src|. Default 0.10.
            For bijective strategies (fpe) the theoretical diff is 0.0; tol
            provides a small budget for floating-point precision.
        min_assoc: Floor on v_src (source Cramers V). Non-zero values prove the
            source columns are meaningfully associated (the check is non-vacuous).
            A near-zero min_assoc means any output passes regardless of whether
            the association was destroyed.
    """

    model_config = ConfigDict(extra="forbid")

    table: str
    col_a: str
    col_b: str
    strategy_a: str | None = None
    strategy_b: str | None = None
    tol: float = Field(ge=0.0, le=1.0, default=0.10)
    min_assoc: float = Field(ge=0.0, le=1.0, default=0.0)


# ---------------------------------------------------------------------------
# InvariantSpec -- all expected invariants for one job
# ---------------------------------------------------------------------------


class InvariantSpec(BaseModel):
    """Full invariant expectation set for one test-flight job.

    Each field maps to one invariant family in testflight/_invariants.py
    (implemented in Phase 1+). Fields default to empty / no-check so a
    skeleton manifest can be valid without listing every family.
    """

    model_config = ConfigDict(extra="forbid")

    # 6.1 Determinism: require value-equal output across two pipeline reruns
    # (schema + to_pydict() + quality_metrics minus timing keys; not a byte
    # comparison -- TH-2.2 doc correction).
    determinism: bool = True

    # 6.4 FK integrity: per-relationship orphan count + policy assertions.
    fk_integrity: list[FKIntegritySpec] = Field(default_factory=list)

    # 6.2 / 6.3 Distribution fidelity: per-column quality expectations.
    distribution: list[ColumnDistributionSpec] = Field(default_factory=list)

    # policy_config passed to apply_quality_policy (mode key defaults to "fail"
    # inside check_distribution_mask if not set here). Tolerances in this dict
    # override the per-strategy defaults from the quality module. Keeping
    # tolerances in the manifest (not hardcoded in the invariant) satisfies the
    # anti-vacuity rule: a reviewer can tighten or relax with a recorded reason.
    policy: dict[str, Any] = Field(default_factory=dict)

    # Whether to enforce grade A/B for preserve-dominant mask tables.
    # Disabled automatically when any preserve column uses a value-changing
    # strategy (fpe/hash) because the value-identity metric will score low by
    # design; the cardinality guard is the correct tooth for those columns.
    grade_floor_enabled: bool = True

    # 6.5 Checksum validity: fpe-checksum columns must satisfy validate(scheme, v).
    checksums: list[ChecksumSpec] = Field(default_factory=list)

    # 6.6 Safe Harbor suppression: planted ZIP3 rows vs expected suppressions.
    safe_harbor: list[SafeHarborSpec] = Field(default_factory=list)

    # 6.7 Quarantine: planted bad rows vs expected total_quarantined.
    quarantine: QuarantineSpec | None = None

    # 6.8 Sentinel no-leakage: planted raw PII strings absent from all output.
    sentinels: list[SentinelSpec] = Field(default_factory=list)

    # 6.9 Computed-column correctness: derived / case_when / derived_aggregate.
    computed_columns: list[ComputedColumnSpec] = Field(default_factory=list)

    # 6.10 Strategy coverage: strategies this job claims to exercise.
    # The live-registry coverage guard (Phase 4) reads SCALAR_HANDLERS and
    # asserts the union across all jobs covers the full registry minus the
    # documented allowlist.
    strategy_coverage: list[str] = Field(default_factory=list)

    # 6.11 chapter_preserve: assert code_set columns with chapter_preserve:true
    # produce output codes in the same chapter as the source codes.
    chapter_preserve: list[ChapterPreserveSpec] = Field(default_factory=list)

    # Phase 3c: relabel-invariant correlation check for value-changing masked pairs.
    # Pairs declared here are checked via Cramers V (chi-square over contingency
    # COUNTS, not value labels). FPE bijections on both columns yield v_out == v_src
    # exactly, closing the carry-forward that crosstab-TVD cannot handle.
    masked_correlations: list[MaskedCorrelationSpec] = Field(default_factory=list)

    # SP-08 joint_mask consistency: each output tuple must be a real reference-table row.
    # Declared per joint_mask group; the runner checks every output row against the
    # set of valid tuples from the shipped reference table.
    joint_mask_consistency: list[JointMaskConsistencySpec] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# FlightManifest -- the top-level manifest model (JobSpec + InvariantSpec)
# ---------------------------------------------------------------------------


class FlightManifest(BaseModel):
    """Full test-flight job manifest.

    Combines the JobSpec (what to run) and InvariantSpec (what to assert) in
    one validated model. Loaded from jobs/<job_name>/manifest.yaml.

    `config` is the raw pipeline config dict. In Phase 0 this may be a
    skeleton {}; Phase 1+ populates a full PipelineConfig-compatible dict that
    the runner validates through PipelineConfig.model_validate(raw).model_dump()
    before calling run_pipeline.
    """

    model_config = ConfigDict(extra="forbid")

    # --- JobSpec fields ---
    job_name: str
    topology: TopologyLiteral
    seed: int = Field(ge=0, description="8-byte big-endian job seed passed to run_pipeline.")
    master_key_label: str = Field(description="Label used with make_key_resolver for FPE keys.")
    tables: list[TableSpec] = Field(min_length=1)
    relationships: list[RelationshipSpec] = Field(default_factory=list)
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw pipeline config dict (skeleton in Phase 0; full in Phase 1).",
    )

    # --- InvariantSpec ---
    invariants: InvariantSpec


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_manifest(manifest_path: Path) -> FlightManifest:
    """Load and validate a test-flight manifest.yaml.

    Reads `manifest_path`, parses YAML, and validates against
    `FlightManifest`. Fail-closed: any unknown key or type mismatch
    raises `ValueError` with the full Pydantic validation trace so the
    caller can surface the exact schema problem.

    Args:
        manifest_path: Path to the manifest.yaml file.

    Returns:
        A fully validated `FlightManifest` instance.

    Raises:
        ValueError: If the manifest fails schema validation.
        FileNotFoundError: If manifest_path does not exist.
    """
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"Manifest at {manifest_path} must be a YAML mapping, got {type(raw).__name__}."
        )
    try:
        return FlightManifest.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Manifest validation failed for {manifest_path}:\n{exc}") from exc
