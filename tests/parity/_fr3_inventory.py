"""Phase F FR3: the generator-to-algorithm inventory (data only, test-side).

Plan section 4 requires the F3 slice to classify EVERY generation selection site by mode and state
before routing anything through the compiled kernel, and to state for each whether it can adopt the
HMAC index kernel WITHOUT changing the distribution. This module is that classification. The same
rows are rendered in `docs/plans/2026-10-18-phase-f-fr3-selection-inventory.md`, and
`test_phase_f_fr3_inventory.py` asserts that

- every row's `backend` is what the executed-backend witness (or a direct stream probe) observes,
- the doc lists every row with the same decision,
- the declined and untouched rows stayed byte-identical (their goldens did not move).

Decisions:

- ``ADOPT``: the selection is uniform over a finite set, or weighted with a mapping that preserves
  the configured weights, and now draws from the index kernel keyed on the global row.
- ``DECLINE``: the site stays on its Python/NumPy path. The reason is given per row.
- ``UNCHANGED``: not a selection (arithmetic, a mask, a pandas expression); the kernel does not
  apply and the bytes are unchanged.

``backend`` uses the witness taxonomy (`_f0_witness`) with ``kernel`` standing for the compiled
index kernel when the companion is installed and the byte-identical Python reference otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

ADOPT = "ADOPT"
DECLINE = "DECLINE"
UNCHANGED = "UNCHANGED"

KERNEL = "kernel"
PYTHON_RNG = "python_rng_selection"
NUMPY = "numpy_selection"
LIVE = "live_provider_generation"
NO_SELECTION = "no_selection"
FORMULA = "formula_expression"
COMPOSITE = "composite_direct_derive"
NOT_IN_CORE = "not_reachable_from_generation"


@dataclass(frozen=True)
class Site:
    sid: str
    generator: str
    code: str
    mode: str
    decision: str
    backend: str
    why: str


SITES: tuple[Site, ...] = (
    Site(
        "sequence.arithmetic",
        "sequence",
        "_column_streams.SequenceStream / synthesize._sequence",
        "value = prefix + zfill(start + global_row * step) + suffix",
        UNCHANGED,
        NO_SELECTION,
        "Not a draw. A uniform kernel draw would destroy order, step and uniqueness. The companion "
        "has no arithmetic op and the arithmetic over the global row index is already exact, so "
        "it stays and its golden is byte-identical. Order, uniqueness, step and padding are "
        "asserted by the sequence test.",
    ),
    Site(
        "categorical.uniform",
        "categorical",
        "_column_streams.CategoricalStream",
        "no weights, list of n >= 1 categories",
        ADOPT,
        KERNEL,
        "index = (HMAC-64 of the global row) mod n, so every list position has probability 1/n up "
        "to a modulo bias of at most n / 2**64. A duplicated category keeps its list multiplicity, "
        "as random.choices does.",
    ),
    Site(
        "categorical.weighted",
        "categorical",
        "_column_streams.CategoricalStream + _kernel_select.WeightedIndexer",
        "weights of length n, finite and non-negative, positive total, every positive weight owns "
        "at least one of 2**32 slots",
        ADOPT,
        KERNEL,
        "Inverse-transform sampling. The kernel draws a uniform integer u in [0, 2**32) (2**32 "
        "divides 2**64, so u is exactly uniform) and the category is the right bisect of u in the "
        "cumulative-weight thresholds. Category i owns the slice [T[i-1], T[i]); each probability "
        "is within 2**-32 of its configured share. A 90/10 split stays 90/10; it is never "
        "flattened to 50/50.",
    ),
    Site(
        "categorical.weighted_unrepresentable",
        "categorical",
        "_column_streams.CategoricalStream (Python fallback)",
        "weights of the wrong length, negative, non-finite, a zero total, or a positive weight "
        "below one 2**-32 slot",
        DECLINE,
        PYTHON_RNG,
        "The CDF mapping cannot represent the weights faithfully, so the column keeps "
        "random.choices with the configured weights (and its legacy errors). The decision reads "
        "the configuration only, never the host.",
    ),
    Site(
        "categorical.empty_or_not_a_list",
        "categorical",
        "_column_streams.CategoricalStream (Python fallback)",
        "categories empty, or not a list/tuple",
        DECLINE,
        PYTHON_RNG,
        "There is no finite index set to draw over (or the legacy generator iterates a string's "
        "characters); the Python path keeps its behavior.",
    ),
    Site(
        "faker.pooled.reuse",
        "faker (pooled)",
        "_column_streams.PooledFakerStream / _faker_pool.PooledSelector",
        "allowlisted type, table rows >= N_THRESHOLD, not opted out, non-deterministic REUSE",
        ADOPT,
        KERNEL,
        "Selection is uniform over the 10_000-entry pool: index = (HMAC-64 of the global row) mod "
        "10_000. A value repeated in the pool keeps its multiplicity, so each distinct value's "
        "frequency is its pool share. Pool CONSTRUCTION (the Faker provider calls) stays Python; "
        "the kernel selects indices only.",
    ),
    Site(
        "faker.pooled.cardinality.unique",
        "faker (pooled)",
        "pool/_sampler.PoolSampler.sample(mode=UNIQUE)",
        "selection without replacement",
        DECLINE,
        NOT_IN_CORE,
        "UNIQUE is a permutation of distinct pool values, not an independent index draw; "
        "independent kernel draws would repeat values and break the distinctness guarantee. "
        "Generation never requests it (REUSE only), so there is no generation site to move.",
    ),
    Site(
        "faker.pooled.cardinality.match_scale",
        "faker (pooled)",
        "pool/_sampler.PoolSampler.sample(mode=MATCH_SOURCE_CARDINALITY / SCALE_SOURCE_CARDINALITY)",
        "distinct count derived from a source column",
        DECLINE,
        NOT_IN_CORE,
        "These modes need a source column's cardinality; generation has no source. Masking-only "
        "and untouched.",
    ),
    Site(
        "faker.pooled.cardinality.reuse_deterministic",
        "faker (pooled)",
        "pool/_sampler.PoolSampler._deterministic",
        "REUSE keyed on a canonical source value",
        UNCHANGED,
        NOT_IN_CORE,
        "Already rides the compiled index kernel on the masking path, byte-identical to the "
        "reference (index KAT). Generation has no source value to key on, so it is not a "
        "generation site and its output is unchanged.",
    ),
    Site(
        "faker.per_row",
        "faker (live)",
        "_column_streams.PerRowFakerStream",
        "below N_THRESHOLD, type not allowlisted, locale list, opted out, or no exact provider",
        DECLINE,
        LIVE,
        "One Faker provider call per row, seeded per row. There is no finite pool to select from, "
        "so the kernel does not apply. Python by definition (Faker providers stay Python).",
    ),
    Site(
        "faker.custom_provider",
        "faker (custom)",
        "faker_setup.get_faker_providers / resolve_pool_provider",
        "a registered custom provider callable",
        DECLINE,
        LIVE,
        "An arbitrary callable; never pooled (custom override declines pooling) and never "
        "kernelized.",
    ),
    Site(
        "reference.random",
        "reference",
        "_column_streams.ReferenceStream + _kernel_select",
        "distribution random (or unrecognized), no cardinality repair",
        ADOPT,
        KERNEL,
        "Uniform over the parent's first-seen, null-dropped key list: index = (HMAC-64 of the "
        "global row) mod len(keys). Every drawn value is a parent key by construction, so FK "
        "membership holds.",
    ),
    Site(
        "reference.sequential",
        "reference",
        "_column_streams.ReferenceStream",
        "distribution sequential",
        UNCHANGED,
        NO_SELECTION,
        "keys[global_row mod len(keys)]: index arithmetic, not a draw. Order is the contract, so "
        "a kernel draw would break it. Unchanged.",
    ),
    Site(
        "reference.weighted",
        "reference",
        "_column_streams.ReferenceStream + _kernel_select.WeightedIndexer",
        "distribution weighted, weights the same length as the key list (a wrong length falls "
        "back to uniform, as before)",
        ADOPT,
        KERNEL,
        "Same inverse-CDF mapping as categorical.weighted over the key list, so the configured "
        "weights are preserved; the wrong-length fallback is the uniform draw it always was.",
    ),
    Site(
        "reference.weighted_unrepresentable",
        "reference",
        "_column_streams.ReferenceStream (Python fallback)",
        "right-length weights the CDF mapping declines",
        DECLINE,
        PYTHON_RNG,
        "Same rule as categorical.weighted_unrepresentable: keep random.choices rather than "
        "approximate the weights.",
    ),
    Site(
        "reference.cardinality_repair",
        "reference",
        "synthesize._reference + ColumnGenerator._apply_cardinality_bounds",
        "min_per_parent / max_per_parent set",
        DECLINE,
        PYTHON_RNG,
        "The plan is ineligible for the unified core (repair revisits earlier assignments), so "
        "it runs the legacy whole-table generator and keeps its golden. Its online-allocation "
        "redesign is a deferred slice.",
    ),
    Site(
        "null_probability.mask",
        "any generator",
        "_column_streams.NullMaskedStream / synthesize._apply_null_probability",
        "Bernoulli mask over rows",
        UNCHANGED,
        "null_mask",
        "A per-row coin flip, not a selection from a set; it stays on default_rng and is recorded "
        "as the null_mask sub-path. Null positions are asserted equal to the legacy path's.",
    ),
    Site(
        "windowed_date.offset",
        "windowed_date",
        "transforms/windowed_date.apply_windowed_date",
        "per-row offset in a window",
        UNCHANGED,
        "unobserved",
        "Already position-keyed (FR1) and its own derivation; outside the F3 selection targets.",
    ),
    Site(
        "python_expression_families",
        "formula, referenced_formula, derived, derived_aggregate, statistical, grouped_series, "
        "group_key",
        "synthesize._generate_column",
        "expression, aggregate, snapshot sampler, grouped transform",
        DECLINE,
        FORMULA,
        "Pandas/Python expression evaluation or a pinned-snapshot sampler. They stay Python by "
        "plan section 4.",
    ),
    Site(
        "legacy.whole_table",
        "any generator in a plan with an ineligible column",
        "synthesize._generate_tables_from_config",
        "whole-plan legacy path",
        DECLINE,
        PYTHON_RNG,
        "Eligibility is whole-plan (plan section 2). The legacy output is its own golden, so "
        "its random.choices / NumPy selections are untouched. This is the Python reference "
        "staying reachable for non-admitted cells.",
    ),
    Site(
        "composite.deterministic_multi_slot",
        "composite_person, composite_name_email, composite_address, composite_custom, "
        "composite_provider",
        "generation/composite/_person.py, _name_email.py, _address.py, _custom.py, _provider.py",
        "deterministic: one derive() latent split into 8-byte slots, each taken mod a pool length",
        DECLINE,
        COMPOSITE,
        "Not reachable from generate_tables. One derive() call feeds several independent slots "
        "(first, last, domain, dob), which is the shared-latent coherence design. The index "
        "kernel consumes only bytes[0:8], so routing it would change the seeded result and split "
        "the latent. No multi-slot compiled kernel exists. Goldens unchanged.",
    ),
    Site(
        "composite.deterministic_single_index",
        "composite_city_state_zip",
        "composite/_city_state_zip.py via PoolSampler.sample_bundle(deterministic=True)",
        "one shared index per row from _derive_pool_indices",
        UNCHANGED,
        NOT_IN_CORE,
        "Already rides the shared index kernel (compiled or reference), parity-preserving by the "
        "index KAT. Not a generation site; output unchanged.",
    ),
    Site(
        "composite.nondeterministic_bundle",
        "all composites",
        "composite/*.build_pool + PoolSampler.sample_bundle(deterministic=False)",
        "seeded REUSE over a bundle pool (default_rng)",
        DECLINE,
        NOT_IN_CORE,
        "Composite-level sampler, not a generate_tables column. Moving it would change the "
        "seeded bundle result without a generation caller to benefit; its goldens are unchanged.",
    ),
)

SITES_BY_ID: dict[str, Site] = {s.sid: s for s in SITES}


def render_markdown_table() -> str:
    """The inventory table as rendered in the FR3 inventory doc."""
    lines = [
        "| Site | Generator | Code | Mode / state | Decision | Backend after FR3 | Distribution reasoning |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in SITES:
        row = [f"`{s.sid}`", s.generator, f"`{s.code}`", s.mode, f"**{s.decision}**", s.backend]
        lines.append("| " + " | ".join(row) + f" | {s.why} |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_markdown_table())
