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
| 1 | Mask one table | Preserved (real rows, values transformed) | Preserved | n/a | n/a | None. Masking is not DP |
| 2 | Mask N linked tables | Preserved | Preserved | Preserved | Preserved | None |
| 3 | Generate one table, providers only | Not matched to source | Independent | n/a | n/a | None |
| 4 | Generate one table from a fitted snapshot | Matched per column | Independent unless `condition_on` | n/a | n/a | None |
| 5 | Generate N linked tables from snapshots | Matched per column | Independent unless `condition_on` | Guaranteed | **Config-only** | None |
| 6 | Mask 2 tables, generate a 3rd that references them | Preserved in masked, matched in generated | As above | Guaranteed | **Config-only** | None |
| 7 | Any of 4 to 6 with `global_settings.dp` | Approximate, noised | **Independent only**, `condition_on` refused | Guaranteed | **Config-only** | (epsilon, delta)-DP, marginal, in flight |

Scenario 6 is your worked example. The answer to "is distribution preserved in
that FK column" is: referential validity yes, fanout no, unless the operator
supplies weights.

## 3. Column value source

Three tiers, and only the middle two involve your data.

**Providers, unfitted.** Faker and Decoy-native providers emit plausible values
of the right shape. A `person_email` looks like an email. Nothing about the
distribution reflects your source. Correct for schema realism, format testing,
and volume. Wrong if anyone expects the output to resemble the input
statistically.

**Fitted snapshot, non-DP.** `decoy fit` builds a distribution snapshot, and
`type: statistical` columns generate from it. Column distributions match the
source. This, not DP, is what makes generated data look like your data.

**DP snapshot.** The same fit with noise added and a privacy budget accounted.
Distributions match approximately. **Turning DP on always reduces fidelity
relative to the same non-DP fit.** What it buys is a provable statement that the
released distribution does not leak any individual row.

A recurring misconception worth stating plainly in customer-facing copy: DP is
not the feature that makes output resemble the source. Fitting is. DP is the
feature that makes the resemblance safe to publish.

## 4. Cross-column fidelity

**Independent (default).** Each column samples from its own distribution. Age
distribution correct, diagnosis distribution correct, no guarantee the diagnosis
on a row suits the age on that row.

**`condition_on` (shipped, non-DP only).** A column can be generated conditioned
on another column's value, backed by a joint distribution in the snapshot. The
conditioning column must be generated first, which the compiler enforces. This is
the existing answer to "make drug type depend on age."

**Under DP: refused.** `condition_on` on a `type: statistical` column under a
declared `dp` block is a hard compile error (`dp_joint_unsupported`). This is
deliberate. The existing joint path measures exact joint counts, which is not
private, so permitting it under a DP declaration would silently void the
guarantee. Fail closed is correct here.

Joint distributions **under** DP are DPS-4 (PrivBayes, MST, AIM). Not built.

## 5. Cross-table fidelity

Three distinct properties, routinely collapsed into "does it handle relationships."

**Referential validity: guaranteed.** Generated foreign keys are drawn from the
parent table's already-generated key column ("mint-a-pool"), with the compiler
enforcing parent-before-child ordering. Every child FK points at a real parent.
In mask mode, FK preservation keeps the relationships intact across the masked
key space.

**Fanout shape: config-only.** How many children each parent gets is an operator
knob, not a learned property:

- `distribution: random` (default): uniform over parent keys
- `distribution: sequential`: round robin
- `distribution: weighted`: operator-supplied weights
- `min_per_parent` / `max_per_parent`: optional cardinality repair

Nothing measures the real fanout from the source. If production has most patients
with one or two claims and a few with fifty, the default output flattens that.
An operator can approximate it by hand with `weighted`, which requires knowing the
shape in advance.

**Joint distributions across tables: not built.** No capability models
correlations spanning a relationship, for example "this parent attribute predicts
that child attribute."

## 6. What Decoy refuses

Refusals are a feature. Each of these fails closed at compile time rather than
producing output that quietly voids a claim.

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
- row counts
- FK graph structure, parent key sets, children per parent
- the column kinds and domains the caller declares as public metadata

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
