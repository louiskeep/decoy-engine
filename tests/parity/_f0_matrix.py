"""Phase F F0 supported/deferred coverage matrix (data only).

Derived from the real dispatch, not from the plan's illustrative list:

- generator kinds: `synthesize._generate_column` (the `kind ==` ladder) and the
  `GenerateColumnConfig.type` Literal in `config/_tables.py`; `test_phase_f_f0_matrix.py` fails if
  a kind exists in the Literal but has no row here;
- pooled-vs-live faker: `_faker_pool.try_pool` -> `pool_eligible` (`_faker_pool.py:239`);
- composites: `generation/composite/*`, which `_generate_column` never dispatches.

``SUPPORTED`` rows name the fixture(s) in `_f0_fixtures.FIXTURES` that pin the behavior; a fixture
only counts when its entry point really exercises the family. ``DEFERRED`` rows state why and who
owns it next. F1 must either keep every SUPPORTED row byte-identical (see `HOW_F1_REUSES`) or
explicitly decline the family; it may not silently change a DEFERRED one either, but nothing here
guards it.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED = "supported-and-pinned"
DEFERRED = "deferred"


@dataclass(frozen=True)
class MatrixRow:
    family: str
    status: str
    fixtures: tuple[str, ...]
    # `_generate_column` kinds (or other dispatch surface) this row covers.
    kinds: tuple[str, ...]
    # What F1 must preserve (SUPPORTED) or why the row is deferred and to whom (DEFERRED).
    note: str


MATRIX: tuple[MatrixRow, ...] = (
    MatrixRow(
        "sequence",
        SUPPORTED,
        ("sequence",),
        ("sequence",),
        "Value is start + step * GLOBAL row ordinal; a batch must be offset by its first row.",
    ),
    MatrixRow(
        "categorical",
        SUPPORTED,
        ("categorical",),
        ("categorical",),
        "One random.Random(col_seed).choices(k=n) stream; the stream position is the global row.",
    ),
    MatrixRow(
        "pooled-faker",
        SUPPORTED,
        ("pooled_faker",),
        ("faker",),
        "Admission is the TABLE row_count vs N_THRESHOLD; one 10_000-value pool built once from "
        "the build seed; selection is default_rng(selection_seed).integers(size=n).",
    ),
    MatrixRow(
        "live-faker",
        SUPPORTED,
        ("live_faker", "live_faker_default_locale"),
        ("faker",),
        "Per-row seed_instance(row_int('faker', i)) with the GLOBAL i; locale precedence is "
        "column locale, then instance_default_locale, then Faker's default.",
    ),
    MatrixRow(
        "keyed-derivation",
        SUPPORTED,
        ("keyed_derive",),
        ("categorical", "faker", "formula"),
        "derive_key replaces the seed root per column; F1 must thread it unchanged.",
    ),
    MatrixRow(
        "composite",
        SUPPORTED,
        (
            "composite_person_deterministic",
            "composite_person_seeded_pool",
            "composite_name_email_deterministic",
            "composite_name_email_seeded_pool",
        ),
        ("composite sampler",),
        "SAMPLER contract only (generate_bundle). generate_tables does not dispatch composites, "
        "so no end-to-end composite claim is made here; see the next row.",
    ),
    MatrixRow(
        "composite (end-to-end through generate_tables)",
        DEFERRED,
        (),
        ("composite",),
        "Not dispatched: `_generate_column` has no composite kind and GenerateColumnConfig.type "
        "excludes it, so there is nothing to freeze. Revisit only if a slice adds the dispatch.",
    ),
    MatrixRow(
        "formula",
        SUPPORTED,
        ("formula",),
        ("formula",),
        "Inline path: per-row py/faker seeds from the GLOBAL i; a failed row is a silent None "
        "(pinned: `empty` and `bad` columns come out as Arrow null).",
    ),
    MatrixRow(
        "formula wall-clock helpers (today, days_from_now, ...)",
        DEFERRED,
        (),
        ("formula",),
        "Read ColumnGenerator._reference_date (the construction-time clock), so no checked-in "
        "expectation can stay true. Not pinnable without injecting a clock (a src/ change).",
    ),
    MatrixRow(
        "referenced-formula",
        SUPPORTED,
        ("referenced_formula",),
        ("formula",),
        "Post-pass runs AFTER every column, in declared order over finished siblings; a reference "
        "to a later-declared referenced formula sees its None placeholder (rendered as '').",
    ),
    MatrixRow(
        "derived",
        SUPPORTED,
        ("derived",),
        ("derived",),
        "Row-wise over already-generated siblings; null sibling propagates by default.",
    ),
    MatrixRow(
        "reference",
        SUPPORTED,
        ("reference", "mixed_fk_generated_parent"),
        ("reference",),
        "Parent pool is the first-seen, null-dropped unique of the COMPLETE parent column; "
        "cardinality repair is whole-table; parents are generated before children (topo sort).",
    ),
    MatrixRow(
        "derived_aggregate",
        SUPPORTED,
        ("derived_aggregate",),
        ("derived_aggregate",),
        "A scalar over the WHOLE column broadcast to every row: a global, not a prefix-stable, "
        "quantity. F1 needs a two-pass or carried reduction for it.",
    ),
    MatrixRow(
        "statistical (exact snapshot)",
        SUPPORTED,
        ("statistical",),
        ("statistical",),
        "Sampled from a pinned snapshot artifact; seed uses the snapshot content digest, not the "
        "snapshot path (pinned by a copy-to-another-path test).",
    ),
    MatrixRow(
        "statistical (DP-fit)",
        DEFERRED,
        (),
        ("statistical",),
        "Uses the same statistical_generate dispatch as the exact-snapshot row; DP only changes "
        "compile-time verification (release-ID budget ledger, provenance, receipt), which needs a "
        "fitted DP artifact plus ledger state. Pinning it needs a certified artifact and is "
        "owned by the DP track, not F0. F1 inherits the exact-snapshot row for generation.",
    ),
    MatrixRow(
        "conditional-sampling",
        SUPPORTED,
        ("conditional",),
        ("statistical",),
        "condition_on reads the already-built sibling; a batch needs the full or carried parent.",
    ),
    MatrixRow(
        "grouped-windowed",
        SUPPORTED,
        ("grouped_windowed",),
        ("grouped_series", "windowed_date", "group_key"),
        "Group-wise sequencing computed over the finished group_by / order_by sibling columns.",
    ),
    MatrixRow(
        "partial-null/all-null",
        SUPPORTED,
        ("nulls",),
        ("sequence", "categorical", "faker"),
        "Null mask is one default_rng(col_seed).random(n) draw; all-null and zero-row columns "
        "come out as Arrow `null` type (pinned in the schema digest).",
    ),
    MatrixRow(
        "generated-parent -> masked-child FK",
        SUPPORTED,
        ("mixed_fk_generated_parent",),
        ("run_pipeline",),
        "Pinned through run_pipeline (generate_tables alone cannot establish it): the mask side "
        "reads the COMPLETE generated parent, observable in the orphan_fk row count. The "
        "execution-layer coupling itself (chunked/out-of-core mask side) is F2's gate.",
    ),
    MatrixRow(
        "custom faker providers (provider_snapshot / live registry)",
        DEFERRED,
        (),
        ("faker",),
        "Process-global registry state; a snapshot argument exists for the shadow-parity harness "
        "and is already covered by tests/physical/test_shadow_generation_faker.py. F1 must pass "
        "provider_snapshot through unchanged.",
    ),
)

# How the later slices consume this contract (also asserted present by the matrix test).
HOW_F1_REUSES = (
    "F1 (bounded generation) reuses the oracle by producing each SUPPORTED fixture's tables "
    "through its partitioned path with IRREGULAR partitions (for example 1, 7, 13, remainder), "
    "concatenating the batches, and comparing with `_f0_canonical.table_fingerprint` against "
    "`_f0_expected.EXPECTED`. `_f0_canonical.rechunked` shows the layout-only difference that "
    "comparison must accept. The invariants in test_phase_f_f0_invariants.py (admission on the "
    "table total, pool identity, parent-pool ordering, post-pass ordering, global row ordinals) "
    "are the properties a partitioned run most easily breaks."
)
HOW_F3_ASSERTS_WITNESS = (
    "F3 (Rust generation selection) routes a selection through the compiled kernel and then "
    "asserts, with `_f0_witness.classify_columns`, that the routed column's backend moved from "
    "TODAY_BASELINE to the compiled label while the oracle fingerprint stays unchanged. The "
    "sampler-level witness (PoolSampler.sample, deterministic) and the end-to-end witness "
    "(generate_tables) are separate; the route that connects them is the pooled-faker selection "
    "in `_faker_pool._build_and_sample_returning_pool`, which today calls PoolSampler.sample "
    "non-deterministically (NumPy default_rng) and is what F3 would re-point."
)
