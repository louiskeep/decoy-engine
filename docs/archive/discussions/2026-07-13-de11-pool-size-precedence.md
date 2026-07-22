# DE-11: the `pool_size` precedence problem

Status: discussion draft for Cam. No production code changed by this document.

Scope note: DE-11 in the adversarial review (`docs/adversarial-architecture-review-2026-07-12.md:144,356-366`)
bundles two defects. This document focuses on the config-location defect (a declared
`pool_size` never reaching the runtime handler) because that is the one with a real
precedence decision attached. The second defect (what quantity UNIQUE capacity should be
checked against, and null handling in the sampler) is summarized in section 5 for context
and flagged as needing its own, separate sign-off.

All file:line references below were re-read directly from the repo on `main`
(`2edbc7b3`) while writing this document.

## 1. The bug in one paragraph

An operator declares `pool_size: 250000` on a column so the engine builds a big enough
pool to draw 250,000 unique synthetic values (`examples/complex_healthcare_claims.yaml:227-232`,
the `providers.tax_id` column, no `provider_config` block at all). Compile accepts it and
the pre-flight capacity checks validate against it. At runtime, the non-chunked faker
handler (`execution/_strategies/_faker.py:49`) never sees that value: it reads
`cfg.get("pool_size", 10_000)` from `provider_config`, which this column never set, so it
silently builds a 10,000-value pool instead of 250,000. Nothing errors. The job succeeds,
and (per the adversarial review's own repro) a job that declared `pool_size: 200` and
compiled clean produced 500 distinct outputs from an undeclared runtime pool
(`docs/adversarial-architecture-review-2026-07-12.md:359-361`). The user-visible symptom is
silent under-provisioning: smaller pools than declared, which for `cardinality_mode: unique`
columns means more collisions than the operator sized for, or (in the worst case) values
that should have been unique are not.

## 2. Root cause: two config locations

`pool_size` exists in two places in a `ColumnConfig` / column dict, and nothing keeps them
in sync.

**Location A: top-level `pool_size`**

```yaml
- name: tax_id
  strategy: replace_with_synthetic
  provider: tax_id
  cardinality_mode: unique
  backend_type: pool
  pool_size: 250000            # top-level -- this is what the example ships
```

**Location B: `provider_config.pool_size`**

```yaml
- name: some_column
  strategy: faker
  provider: some_provider
  provider_config:
    pool_size: 50000           # nested -- this is what the runtime handler reads
```

### Read/write map

| Site | Location read | What it does |
|---|---|---|
| `config/_tables.py:85` | top-level `ColumnConfig.pool_size` field declaration | Pydantic field. Docstring at lines 80-84 calls it "capacity hint for the planner's basic_uniqueness_pre_flight check," explicitly a compile-time-only concept. |
| `plan/_checks.py:145-191` (`check_basic_uniqueness_pre_flight`) | top-level `col_entry.get("pool_size")` (line 173) | S1's compile-time UNIQUE capacity check: raises `pool_capacity_pre_flight_unique` if `source_distinct > pool_size`. Still wired into compile at `plan/_compile.py:301`. |
| `generation/pool/_validate.py:49-168` (`check_pool_capacity_pre_flight`) | top-level `col_entry.get("pool_size", 10_000)` (line 106) | S5's fuller compile-time check, covers UNIQUE/MATCH/SCALE. Wired into compile at `plan/_compile.py:148,250,302`. Its own docstring says it "supersedes" the S1 check for pool-backed columns, but both still run. |
| `plan/_seed_envelope.py:203-207` | top-level `col_entry.get("provider_config", {})` only | Builds the compiled `ColumnSeed.provider_config` tuple. **Only copies `provider_config`. Never reads or carries `col_entry["pool_size"]`.** This is where the top-level value is dropped. |
| `plan/_serialize.py:87-98,197-208` | `ColumnSeed.provider_config` | Round-trips the compiled plan to/from a dict for storage. No `pool_size` field exists on `ColumnSeed` to serialize (see `plan/_types.py:51-91`) so there is nothing to lose or preserve here; it was already gone. |
| `execution/_strategies/_faker.py:48-49` | `provider_config.pool_size` via `cfg.get("pool_size", _DEFAULT_POOL_SIZE)`, default `10_000` | The actual non-chunked faker/pool handler. This is the runtime consumer that silently falls back. |
| `execution/_chunked.py:152-158` (`_conditional_admission_failures`) | `provider_config.pool_size` via `cfg.get("pool_size")` | Chunked-mode admission gate for `faker` columns. **Requires** an explicit `provider_config.pool_size`; a config with only the top-level field is *rejected* from the chunked-conditional path with `chunked_strategy_conditions_unmet`. |
| `execution/_chunked.py:451-458` (`_prewarm_faker_pools` or equivalent pre-warm builder) | `provider_config.pool_size` via `cfg["pool_size"]` (line 456, no default -- "admission requires it explicitly") | Pre-warms the pool cache before chunked execution; mirrors the handler's read location exactly by design. |

### Compile-time-only vs runtime split

`ColumnSeed` (`plan/_types.py:51-91`), the dataclass that IS the compiled plan handed to
execution, has no `pool_size` field. It carries `provider_config` as an opaque
`tuple[tuple[str, Any], ...]` built exclusively from `col_entry["provider_config"]`
(`plan/_seed_envelope.py:203-207`). The top-level `col_entry["pool_size"]` is read by the
two compile-time checks above and then never touched again. It has no path into
`ColumnSeed`, so every consumer downstream of compile (the faker handler, the chunked
admission gate, the chunked pre-warm builder) is structurally unable to see it, no matter
what value the operator declared.

This means: **compile validates against the number the operator wrote, and runtime executes
against a different number (or the default) that the operator did not write.** Compile and
runtime are checking two different declarations that happen to share a key name.

### Both locations are real and tested, not just theoretical

- `tests/unit/plan/test_checks.py:266,290` tests the top-level field against
  `check_basic_uniqueness_pre_flight`.
- `tests/unit/generation/pool/test_validate.py:84-138` tests the top-level field against
  `check_pool_capacity_pre_flight`.
- `tests/unit/execution/test_faker_strategy.py:28-37`, `tests/unit/execution/test_chunked.py:67,215`,
  `tests/perf/test_job_performance_gates.py:289-307`, and `tests/unit/test_vault.py:80` all
  test `provider_config.pool_size` against the runtime handler and chunked admission.
- `examples/complex_healthcare_claims.yaml:232` ships the top-level field with an explanatory
  comment ("capacity hint for the uniqueness pre-flight"), so it is also the documented,
  example-blessed shape.

Neither location is dead code or a mistake. Both are exercised by tests and both are load-bearing
for the systems that read them.

## 3. Why it needs a decision, not pure plumbing

If only one location existed, the fix would be a plumbing task: wire the value through.
Because two independently-tested, independently-shipped locations exist, a fix has to pick a
winner for the case where both are set to *different* values. That choice changes runtime
output for any config that (deliberately or accidentally) sets both. It is a config-contract
semantic decision, not a bug where the "right" behavior is unambiguous:

- If a config sets `pool_size: 200` at top level and `provider_config: {pool_size: 5000}`,
  should the compiled plan build a 200-value pool or a 5000-value pool? Compile's capacity
  checks already validated against 200 (per the top-level-only read sites in section 2). If
  runtime picks 5000, compile validated the wrong number. If runtime picks 200, the
  `provider_config` value the operator also wrote is silently discarded, symmetric to today's
  bug except in the opposite direction.
- Whatever precedence is chosen becomes the answer for every existing config once it starts
  actually reaching the handler, including the shipped example
  (`examples/complex_healthcare_claims.yaml`) and every test fixture in section 2's list.
- The chunked-execution admission gate (`execution/_chunked.py:152-158`) was deliberately
  designed (2026-06-12, capability-gaps WS4, deferred follow-up 2; module docstring at
  `execution/_chunked.py:34-48`) to require `provider_config.pool_size` specifically, not the
  top-level field, as its "explicit whole-run capacity declaration" contract. A precedence
  fix that starts accepting top-level-only declarations changes what is admissible into the
  chunked path -- see option (a) below.

## 4. Options

### (a) Top-level wins

Flow `col_entry["pool_size"]` into the compiled `ColumnSeed` (or into `provider_config`
before it is copied), so the value the compile-time checks already validate against is the
same value the handler uses.

**For:** matches the documented/shipped shape (`examples/complex_healthcare_claims.yaml`);
matches what a naive reader of `ColumnConfig` (`config/_tables.py:85`) would expect, since
`pool_size` is a first-class field there, not buried in a free-form dict; requires the least
change to existing configs that only set the top-level field, i.e. the example.

**Against:** the chunked admission gate at `execution/_chunked.py:152-158` currently
hard-requires `provider_config.pool_size` and rejects top-level-only faker columns from the
chunked-conditional path (`chunked_strategy_conditions_unmet`). If top-level wins for the
non-chunked handler but the chunked gate is left as-is, the two paths disagree again, just
one level down: a config admissible to full-frame execution becomes inadmissible to chunked
execution for the same declared capacity. The gate would need updating too (see the rogue
branch's approach below), which touches WS4's deliberately narrow 2026-06-12 admission
contract and is its own review surface.

### (b) `provider_config` wins

Make the compile-time checks (`plan/_checks.py:173`, `generation/pool/_validate.py:106`)
read `provider_config.pool_size` (falling back to top-level, or ignoring it) instead of the
top-level field.

**For:** matches the shipped runtime contract exactly as it exists today (the faker handler
and chunked admission both already read `provider_config`); zero change to
`execution/_strategies/_faker.py` or `execution/_chunked.py`; the fix is confined to the two
compile-time checks.

**Against:** the top-level field (`config/_tables.py:85`) and the shipped example
(`examples/complex_healthcare_claims.yaml:232`) both put the value at the top level with no
`provider_config` at all. If the compile-time checks stop reading it, that field becomes
either dead (compile silently ignores it) or must be actively deprecated/removed, which is a
breaking documentation and example change, not just a code change. Also less discoverable:
`pool_size` as a typed `ColumnConfig` field is more visible to an operator skimming the
schema than a key inside a free-form `provider_config` dict.

### (c) Reject on contradiction

Keep both locations legal individually, but if a column sets both to different values, fail
compile with a clear error (`pool_size_location_conflict` or similar) instead of silently
picking one.

**For:** safest option -- never silently discards a value the operator wrote; forces the
ambiguity to surface at compile time where it is cheap to fix, not at runtime where it is a
silent capacity shortfall; compatible with either (a) or (b) as the resolution when only one
location is set.

**Against:** it is a new breaking validation. Any existing config that (for whatever reason)
already sets both to the same value would pass; any that set them to different values -- which
by definition includes the exact case this document exists because of, if the operator's
intent was for the two to differ -- now hard-fails at compile instead of running (silently
wrong). This does not by itself answer what happens when only ONE location is set (the
common case, including the shipped example), so it still needs (a) or (b) layered underneath
it for the single-location path.

### Reference precedent: the reverted `de11/poolspec-unification` branch

A branch already exists that implemented one answer to this question. It was merged to
`main` as part of an unauthorized autonomous merge (PR #53/#54) and then reverted wholesale
by Cam's explicit decision (`45806c6`, "Revert unauthorized PR #53 + #54: restore engine main
to TB-1... landing DE-11 semantic changes ... that required human sign-off"). Its code is
still reachable at `de11/poolspec-unification` / `origin/de11/poolspec-unification`. It is
cited here as a concrete precedent, not as the answer this document is pre-selecting:

```python
# generation/pool/_capacity.py (de11/poolspec-unification, not on main)
def resolve_pool_size(col_entry: dict[str, Any]) -> tuple[int, bool]:
    """Canonical order (DE-11):
    1. top-level column `pool_size` (what operators set) wins;
    2. `provider_config['pool_size']` is the pre-DE-11 fallback;
    3. otherwise DEFAULT_POOL_SIZE.
    """
    top = col_entry.get("pool_size")
    if top is not None:
        return int(top), True
    provider_config = col_entry.get("provider_config")
    if isinstance(provider_config, dict) and provider_config.get("pool_size") is not None:
        return int(provider_config["pool_size"]), True
    return DEFAULT_POOL_SIZE, False
```

This is option (a) with a silent fallback rather than option (c)'s hard reject: if both are
set, top-level wins with no error. Notably, that branch also had to touch the chunked
admission gate to keep it consistent (`execution/_chunked.py:142-146` on that branch,
comment: "admission must resolve pool_size the SAME way compile and the pre-warm builder
do... so a top-level-only declaration is admitted here instead of being refused for a config
compile already accepted"), confirming that option (a) is not confined to the non-chunked
handler -- it necessarily also loosens the WS4 chunked-admission contract from "requires
`provider_config.pool_size`" to "requires `pool_size` in either location." That is a second,
smaller precedence-adjacent decision bundled inside option (a).

## 5. The DE-11 second defect (context only, needs its own sign-off)

The adversarial review's DE-11 finding actually bundles two independent defects
(`docs/adversarial-architecture-review-2026-07-12.md:356-366`; the reverted branch's own
module docstring at `generation/pool/_capacity.py` header calls them out as "two independent
defects"). This document is about defect 2 (config location, above). Defect 1 is a separate
semantic question:

- **Compile-time UNIQUE check** (`generation/pool/_validate.py:65-68,110-136`) validates
  `pool_size >= source.distinct_count` -- the number of distinct values in the *source*
  column.
- **Runtime UNIQUE sampler** (`generation/pool/_sampler.py:124-134`) validates
  `pool.size >= n` where `n` is the *output row count passed to `sample()`* -- and, reading
  the current code, that check does not subtract null rows first; it draws `n` distinct
  values via `rng.permutation(pool.size)[:n]` for every row including ones that will later be
  overwritten with `None` by the handler (`execution/_strategies/_faker.py:91-93`).

These are three different quantities (source distinct count, total row count, non-null row
count) doing duty as "the" capacity requirement in three different places, and they will
disagree whenever a column has duplicate source values, nulls, or a row count that differs
from its source's distinct count. The reverted branch's fix defined ONE quantity (non-null
output row count) and one `unique_capacity_ok()` function shared by compile and runtime.
There is also a related, smaller dtype note: the faker handler currently rebuilds the output
column via a plain Python list comprehension (`execution/_strategies/_faker.py:93`), which
coerces the assigned column to pandas `object` dtype regardless of the source column's
original dtype.

This is a real, separate, user-visible semantic change (it changes when UNIQUE columns
raise `uniqueness_impossible`, and what "impossible" means) and deserves its own
Cam-gated discussion rather than being folded into the pool_size precedence decision. It is
included here only so the two DE-11 defects are not conflated when reviewing a future fix.

## 6. Recommendation

Lean: **option (a), top-level wins, folded into the compiled plan** -- with the chunked
admission gate updated to accept either location (as the reverted branch already did), and
serious consideration of layering **(c)'s reject-on-contradiction** on top for the case where
both are set to different values, rather than (a)'s silent-fallback version.

Reasoning:

- The top-level field is the one with real product surface: it is the typed `ColumnConfig`
  field (`config/_tables.py:85`), it is what the shipped example uses
  (`examples/complex_healthcare_claims.yaml:232`), and it is what both compile-time capacity
  checks already validate against today. Choosing (b) means either quietly breaking the
  documented/example shape (compile stops honoring the field it currently enforces) or
  shipping a deprecation, which is a larger, more visible break than making the top-level
  field actually work as documented.
- Backward compatibility favors (a): every config that already sets ONLY the top-level field
  (which, per the shipped example, is the documented pattern) starts working correctly for
  the first time instead of silently under-provisioning. No existing config that relies on
  the current runtime behavior needs to change, because the current runtime behavior for a
  top-level-only config is a bug (silent default), not a feature anyone can be depending on.
  Configs that already set `provider_config.pool_size` (the runtime-only tests, chunked
  fixtures) are unaffected under (a) unless they also set a *different* top-level value,
  which by definition is the ambiguous case (c) exists to catch.
- The chunked-admission constraint is real but not a blocker for (a): the reverted branch
  shows the fix is small (accept `pool_size` from either location in the admission check,
  same as the resolver), and it is a strict *loosening* (previously-rejected top-level-only
  configs become admissible), not a behavior change for any config that already passes
  admission today.
- Pure (a) with silent fallback still has the same silent-discard failure mode as the
  current bug, just for the less-common "both set, differ" case instead of the common
  "only top-level set" case. Given this whole document exists because a silent drop was a
  HIGH-severity finding, I would rather recommend paying the one-time cost of a hard compile
  error for that specific conflict than reintroduce a smaller version of the same problem.
  That said, this doubles the size of the change (new error path, new test surface) and is
  a genuine judgment call Cam should make explicitly rather than inherit from this
  recommendation.

## 7. Open questions for Cam

1. **Precedence: (a), (b), or (c)?** My lean is (a) top-level wins, optionally layered with
   (c)'s reject-on-contradiction for the both-set-and-differ case. Does that match the
   product intent for `pool_size`, or is there a reason `provider_config` should be treated
   as the canonical location going forward (e.g. to keep all provider-specific build knobs in
   one free-form dict rather than growing more typed top-level fields on `ColumnConfig`)?
2. **Should the chunked admission gate change alongside the precedence fix, or stay
   `provider_config`-only as a deliberate narrowing of the chunked-safe surface?** The WS4
   design explicitly chose `provider_config.pool_size` as the "explicit whole-run capacity
   declaration" for chunked mode. Loosening it to accept top-level too is a second decision
   riding on the first.
3. **Does the UNIQUE-capacity / sampler-null semantics defect (section 5) get fixed in the
   same change, or scheduled as its own separately-reviewed follow-up?** They are two
   different defects sharing one adversarial-review finding ID; this document recommends
   treating them as two decisions, not one.
4. **Migration/deprecation:** if (b) or a documented single canonical location is chosen
   instead of (a), does `examples/complex_healthcare_claims.yaml` and any user-facing docs
   referencing the top-level field need an explicit deprecation notice, or is pre-GA license
   (per `CLAUDE.md`'s "Pre-GA = hard delete" framing and `docs/compatibility-contract.md`)
   enough to just change it outright?
5. **Should `resolve_pool_size`-equivalent logic be re-authored fresh, or is the reverted
   `de11/poolspec-unification` branch (still present at that ref) an acceptable starting
   point to re-review and re-land through a proper, authorized PR** rather than reimplementing
   from scratch?
