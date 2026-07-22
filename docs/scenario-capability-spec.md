# Scenario capability specification

What Decoy actually does for a given customer setup, what it guarantees, what it
quietly does not, and what it refuses outright.

This is the semantics companion to [`capability-matrix.md`](capability-matrix.md).
That file is a generated inventory: which strategies, providers, and detectors
exist. This file answers the different question a customer or a sales conversation
actually asks: "I have these tables and this goal, what will Decoy give me, and
what will it not?"

Status legend used throughout:

- **Shipped**: on `main`, tested, claimable today.
- **In flight**: built but not merged. Named explicitly, never counted as shipped.
- **Config-only**: the capability exists but is driven by an operator-supplied
  knob rather than learned from the source data. This is the category most likely
  to be mistaken for fidelity.
- **Not built**: no implementation. Listed so the gap is visible.

## 1. The five axes

Every scenario is a point in this grid. The apparent combinatorial explosion
collapses once the axes are treated as independent.

| Axis | Options | Notes |
|---|---|---|
| Mode, per table | mask, generate | Set per table, so one job can mix both |
| Column value source | providers (unfitted), fitted snapshot, DP snapshot | Determines whether output distributions resemble the source |
| Cross-column fidelity | independent, `condition_on` | `condition_on` needs a joint in the snapshot |
| Cross-table fidelity | referential validity, fanout shape, joint across tables | Three separate things, commonly conflated |
| Privacy guarantee | none, marginal DP | DP is opt-in via `global_settings.dp` |

The two axes that cause most confusion are **column value source** and
**cross-table fidelity**, because both have a mode that looks like fidelity and
is not.

## 2. Scenario grid

Common setups, and what each actually produces.

| # | Scenario | Column distributions | Cross-column | FK integrity | FK fanout | Privacy claim |
|---|---|---|---|---|---|---|
| 1 | Mask one table | **Strategy-dependent** (see 3.1) | Row alignment preserved | n/a | n/a | None. Masking is not DP |
| 2 | Mask N linked tables | **Strategy-dependent** | Row alignment preserved | Legitimate joins preserved | Preserved from source | None |
| 3 | Generate one table, providers only | Not matched to source | Independent | n/a | n/a | None |
| 4 | Generate one table from a fitted snapshot | Matched per column | Independent unless `condition_on` | n/a | n/a | None |
| 5 | Generate N linked tables from snapshots | Approximated per column | Independent unless `condition_on` | Scalar acyclic generate-to-generate only | **Config-only** | None |
| 6 | Mask 2 tables, generate a 3rd that references them | **NOT SUPPORTED** | n/a | n/a | n/a | n/a |
| 7 | Scenario 4 or 5 with `global_settings.dp` | Approximate, noised, numeric + categorical only | **Independent only**, `condition_on` refused | As row 5 | **Config-only** | (epsilon, delta)-DP on DP-verified generated marginals only, in flight |

**Scenario 6 is rejected at config validation.** A generated child referencing a
mask-kind parent is deferred to V2.1 (`config/_pipeline.py:172`, regression test
`test_v2_generation.py:1396`). Mixed mask-parent to generate-child is not a
supported topology. It is listed here because it is an obvious thing to attempt.

Scenario 7 does not extend to scenario 6, and DP never covers masked output.

## 3. Column value source

Three tiers, and only the middle two involve your data.

**Providers, unfitted.** Faker and Decoy-native providers emit plausible values
of the right shape. A `person_email` looks like an email. Nothing about the
distribution reflects your source. Correct for schema realism, format testing,
and volume. Wrong if anyone expects the output to resemble the input
statistically.

**Fitted snapshot, non-DP.** `decoy fit` builds a distribution snapshot, and
`type: statistical` columns generate from it. Column distributions **approximate**
the source. The fit is lossy by construction: numeric values are drawn uniformly
inside histogram bins, categorical tail mass is redistributed or emitted as
`__other__`, datetimes are uniform within a selected year, and free text preserves
length only. A correct sampler can legitimately score below the default fidelity
threshold. This, not DP, is what makes generated data resemble your data.

**DP snapshot.** Not the non-DP fit with noise bolted on. Scope B never builds an
exact snapshot; it runs a separate OpenDP measurement schedule. Supported kinds are
numeric and categorical only, and datetime and free text are rejected. **Turning DP
on normally reduces expected fidelity** relative to the same non-DP fit. What it
buys is a bounded, approximate `(epsilon, delta)` privacy loss under
add-or-remove-one-row adjacency. It does not mean zero leakage.

A recurring misconception worth stating plainly in customer-facing copy: DP is
not the feature that makes output resemble the source. Fitting is. DP is the
feature that makes the resemblance safe to publish.

### 3.1 Masking is not automatically distribution-preserving

Mask mode preserves row alignment and, when configured correctly, legitimate
joins. Statistical fidelity depends entirely on the chosen strategy. The engine
classifies its own strategies in `execution/_distribution_behavior.py`: faker and
uniform categorical destroy frequency, redact collapses, truncate and bucketize
coarsen, and shuffle preserves the marginal while breaking row identity. Never
claim blanket distribution preservation for mask mode.

## 4. Cross-column fidelity

**Independent (default).** Each column samples from its own distribution. Age
distribution correct, diagnosis distribution correct, no guarantee the diagnosis
on a row suits the age on that row.

**`condition_on` (shipped, non-DP only).** A column can be generated conditioned
on another column's value, backed by a joint distribution in the snapshot. The
conditioning column must be generated first, which the compiler enforces.

It is narrower than it sounds: **pairwise, categorical-dependent only, and
approximate.** Joint snapshots retain only top cells, and a parent value missing
from the joint falls back to the marginal. It is not a general cross-column model.

**Under DP: refused.** `condition_on` on a `type: statistical` column under a
declared `dp` block is a hard compile error (`dp_joint_unsupported`). This is
deliberate. The existing joint path measures exact joint counts, which is not
private, so permitting it under a DP declaration would silently void the
guarantee. Fail closed is correct here.

Joint distributions **under** DP are DPS-4 (PrivBayes, MST, AIM). Not built.

## 5. Cross-table fidelity

Three distinct properties, routinely collapsed into "does it handle relationships."

**Referential validity: guaranteed within a supported topology.** Generated foreign
keys are drawn from the parent's already-generated key column ("mint-a-pool"), with
the compiler enforcing parent-before-child ordering. The precise claim is: *non-null
scalar FKs in supported acyclic generate-to-generate topologies reference an emitted
parent value.* The carve-outs are real:

- An empty parent pool yields nulls.
- `null_probability` can null FK values after sampling.
- Composite FKs are not sampled tuple-wise, so multi-column keys are not jointly valid.
- Generate-child to mask-parent is rejected outright (scenario 6).
- In mask mode, configured orphan policies may intentionally retain invalid source keys.

**Fanout shape: config-only.** How many children each parent gets is an operator
knob, not a learned property:

- `distribution: random` (default): uniform over parent keys
- `distribution: sequential`: round robin
- `distribution: weighted`: operator-supplied weights
- `min_per_parent` / `max_per_parent`: optional cardinality repair

These **fail soft**, which is worse than failing loud. A wrong-length weight vector
silently becomes uniform, an unknown `distribution` name silently becomes random,
and infeasible min/max bounds warn and then emit violating output. Bounds also do
not compose with `sequential`, and later null injection can invalidate
`min_per_parent`.

Nothing measures the real fanout from the source. If production has most patients
with one or two claims and a few with fifty, the default output flattens that.
An operator can approximate it by hand with `weighted`, which requires knowing the
shape in advance.

**Joint distributions across tables: not built.** No capability models
correlations spanning a relationship, for example "this parent attribute predicts
that child attribute."

## 6. What Decoy refuses

Refusals are a feature. Each of these fails closed at fit or compile time rather
than producing output that quietly voids a claim.

| Refusal | Trigger | Why |
|---|---|---|
| `dp_joint_unsupported` | `condition_on` under `global_settings.dp` | Joint path is not private, would void the DP claim |
| anti-DP knobs | `allow_real_categories: true` or `high_cardinality: true` under `dp` | Both release real vocabulary |
| budget exceeded | Composed spend over the declared ceiling | Declared epsilon/delta are an enforced ceiling, not an annotation |
| missing public declarations | DP fit without declared column kinds and domains | Inferring kinds from data leaks a predicate at zero budget |

## 7. What the DP guarantee does not cover

Worth stating explicitly because it is the most likely source of an overclaim.

The per-column releases are DP and compose into a total. Everything outside that
release set is treated as **public** and carries no guarantee:

- schema, column names, column types
- the configured synthetic output row count (public). Note the *source* fit row
  count is DP-noised and does spend budget; these are different numbers
- FK graph structure, parent key sets, children per parent
- the column kinds and domains the caller declares as public metadata
- all masked output. DP covers DP-verified generated marginals and their
  post-processing, nothing else
- datetime and free-text columns, which are rejected rather than covered
- joints and conditional synthesis

Two further limits deserve their own statement:

**Adjacency is one ROW, not one PERSON.** A patient with fifty claims contributes
fifty rows, and nothing bounds that person-level contribution. Row-level DP being
read as patient-level privacy is the single most likely misunderstanding in a
healthcare sale, and entity-level contribution bounds are not built.

**Composition is per compiled plan, not lifetime.** Release IDs compose within one
plan. Repeated fits against the same source population over time are not tracked,
so cumulative privacy spend can silently exceed any intended ceiling.

The last point is deliberate. Requiring the caller to declare kinds and domains is
what makes them defensible as public, instead of Decoy silently deriving them from
private data.

## 8. Gaps, as candidate roadmap items

Listed with a rough size and the reason a customer would care. None of these are
decisions; they are options for the roadmap conversation.

**A. Learned fanout distribution.** Fit the parent-child multiplicity from source
and generate against it, instead of the current uniform default. This is the
analogue of a distribution snapshot for relationships. Likely the highest
ratio of realism gained to work required, since it is one distribution per
relationship rather than a joint model. Small to medium.

**B. Fanout under DP.** Once A exists, the honest follow-on is whether the fanout
release is itself DP, which would move part of the FK structure inside the
guarantee. Medium, and it needs a survey first: sensitivity across a join does not
behave like sensitivity within a table.

**C. DPS-4, joint DP, single table.** Private structure learning plus a consistency
solve (Private-PGM / `mbi`). The sampling and joint-capable snapshot format already
exist for the non-DP path, so this is "make the joint machinery survive DP" rather
than a from-scratch build. Large, plus genuine utility-validation work.

**D. DPS-4 relational.** Joint DP spanning a relationship. Open-ended. Thin
literature, and the off-the-shelf tools assume a single flat table. Needs a scoped
survey before any commitment.

**E. Scenario presets.** The grid in section 2 is derivable but currently requires
expertise to navigate. A named preset per common scenario would remove that.
Product decision, not an engine one.

## 9. Open questions for the roadmap review

1. Which cells in section 2 do we claim publicly, and which do we document as
   deliberately unsupported?
2. Is fanout fidelity (gap A) a launch requirement or a follow-on? It is the most
   likely thing a relational-data buyer assumes already works.
3. Does joint DP (C) have a buyer, or is marginal DP sufficient for the uses we
   are selling into?
4. Do we want the config-only knobs (`distribution`, `min_per_parent`,
   `max_per_parent`) surfaced as fidelity controls, or hidden as advanced options
   so nobody mistakes them for learned behavior?

## Related

- [`capability-matrix.md`](capability-matrix.md): generated inventory of
  strategies, providers, connectors, detectors
- [`relationships.md`](relationships.md): FK handling detail
- [`strategies.md`](strategies.md): per-strategy narrative
- [`what-we-cannot-prove.md`](what-we-cannot-prove.md): the standing honesty
  document, including the DP claim boundary
- `decoy-platform/docs/ROADMAP.md`: the single cross-repo roadmap, including the
  DPS section
