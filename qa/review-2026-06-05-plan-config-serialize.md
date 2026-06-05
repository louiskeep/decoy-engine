# QA Review: `plan/`, `config/_tables.py`, `config/_transforms.py`, `errors.py`

**Date:** 2026-06-05  
**Reviewer:** QA (Claude)  
**Branch:** `qa/review-2026-06-05-plan-config-serialize`  
**Engine commit:** `6e3bc97` (main, post Sprint-2 docs merge)

## Scope

Files reviewed in this session (all first-time QA coverage):

| File | Size |
|---|---|
| `src/decoy_engine/plan/_compile.py` | 38 KB |
| `src/decoy_engine/plan/_checks.py` | 10 KB |
| `src/decoy_engine/plan/_types.py` | 10 KB |
| `src/decoy_engine/plan/_serialize.py` | 10 KB |
| `src/decoy_engine/plan/validate/_consolidator.py` | 4 KB |
| `src/decoy_engine/config/_tables.py` | 11 KB |
| `src/decoy_engine/config/_transforms.py` | 4 KB |
| `src/decoy_engine/errors.py` | 8 KB |

Files explicitly excluded (covered by earlier sessions on today's other branches):  
`validation/post/`, `providers_v2/identifiers/`, `internal/`, `walks/`, `quality/`,
`data_discovery.py`, `config/_pipeline.py`, `execution/_pandas_adapter.py`,
`execution/_strategies/` (all 14 handlers), `generation/synthesize.py`,
`relationships/`, `connectors/`, `determinism/`, `disguises/`, `profile/`,
`instrumentation/`, `storm/`, `transforms/`, `generators/`, `expressions.py`,
`context.py`.

---

## 1. Summary

The plan compiler is structurally sound: its check ordering, seed normalization,
and config-hash canonicalization are well-designed and most known hazards
(seed type coercion, composite FK policy drift, when+coherent_with) have
already been caught and fixed in prior sprints. Two issues stand out.

First, `_build_relationships` appends children in `profile.relationships`
iteration order without sorting; if the upstream profiler returns relationships
from an unordered source, two compiles of semantically identical configs produce
`PlanRelationship` objects with different `children` tuples, breaking
`Plan.__eq__` and YAML byte-stability -- the core reproducibility guarantee.

Second, `_namespace_from_dict` in `_serialize.py` uses `__` both as the
composite-FK column separator and as a plain split delimiter for any column name
that happens to contain `__`. A column named `account__id` or a Salesforce
`custom_field__c` round-trips as a multi-element tuple instead of a single
column, silently corrupting the namespace binding on deserialization.

---

## 2. Findings

### F1 -- HIGH | Determinism
**`plan/_compile.py` `_build_relationships`: children tuple ordering is non-deterministic**

`_build_relationships` groups children by `(parent_table, parent_cols, policy)` and
finalizes groups with `sorted(grouped.items())` -- that sort covers the outer
group order, not the children list inside each group.

```python
# current (plan/_compile.py ~L210-250)
for (parent_table, parent_cols, policy), children in sorted(grouped.items()):
    ...
    out.append(
        PlanRelationship(
            ...
            children=tuple(
                PlanRelationshipEnd(table=t, columns=c) for (t, c, _) in children
            ),
            ...
        )
    )
```

`children` is built by `grouped.setdefault(key, []).append(...)` in the order
`profile.relationships` is iterated. `profile.relationships` comes from
`profile_source()` (the profiling layer). If the profiler reads a DB's
information_schema or a Parquet with unstable row order, two runs produce
different `children` tuple orderings.

**Impact:** `Plan.__eq__` uses frozen-dataclass equality, which compares tuples
element-by-element. Two semantically identical plans with differently-ordered
children are NOT equal. The plan YAML differs byte-for-byte. Any tooling that
compares plans (cache lookups, manifest deduplication, audit replay matching)
will report a false mismatch. This breaks the `compile_plan` determinism contract
documented in the module docstring and `Plan` class docstring.

**Verify:** Construct two `Profile` objects with the same relationships declared
in different list orders; call `compile_plan` on both; assert `plan_a == plan_b`.
This assertion currently fails when any parent has more than one child.

**Fix:**

```python
# plan/_compile.py _build_relationships -- sort children before tuple-ifying
out.append(
    PlanRelationship(
        parent=PlanRelationshipEnd(table=parent_table, columns=parent_cols),
        children=tuple(
            PlanRelationshipEnd(table=t, columns=c)
            for (t, c, _) in sorted(children, key=lambda x: (x[0], x[1]))
        ),
        orphan_policy=policy,
        namespace=namespace,
    )
)
```

The sort key `(child_table, child_columns)` matches the natural identity of a
relationship end and is stable across any profile source ordering.

---

### F2 -- MEDIUM | Correctness
**`plan/_serialize.py` `_namespace_from_dict`: `__`-separator round-trip breaks for column names containing `__`**

Serialization in `_namespace_to_dict`:

```python
{"declared_by": [f"{t}.{'__'.join(cols)}" for (t, cols) in ns.declared_by]}
```

For a composite FK namespace `("users", ("first_name", "last_name"))` this
produces `"users.first_name__last_name"` (intended).  
For a SINGLE-column namespace `("accounts", ("account__id",))` this also
produces `"accounts.account__id"`.

Deserialization in `_namespace_from_dict`:

```python
cols = tuple(col_part.split("__")) if "__" in col_part else (col_part,)
```

Both inputs are indistinguishable. The single column `account__id` is
deserialised as `("account", "id")` -- a two-column composite.

**Impact:** Any pipeline targeting a column whose name contains `__`
(Salesforce custom fields `custom__c`, PostgreSQL functional-index conventions,
DBT staging patterns, snake-cased compound identifiers) will have its namespace
binding silently corrupted on plan YAML round-trip. The masking run may proceed
with the wrong namespace, breaking cross-pipeline FK stability. The bug is silent
-- no error is raised; the plan just disagrees with what was compiled.

**Verify:**
```python
from decoy_engine.plan._types import NamespaceBinding
from decoy_engine.plan._serialize import plan_to_yaml, plan_from_yaml
# ... build a minimal Plan with a NamespaceBinding containing a column named
# "account__id" (single element tuple), round-trip via YAML, assert equality.
# Currently fails: deserialized cols == ("account", "id") != ("account__id",)
```

**Fix:** Replace the joined-string format with a structured list:

```python
# _namespace_to_dict -- structured, no ambiguity
def _namespace_to_dict(ns: NamespaceBinding) -> dict[str, Any]:
    return {
        "declared_by": [
            {"table": t, "columns": list(cols)} for (t, cols) in ns.declared_by
        ],
    }

# _namespace_from_dict
def _namespace_from_dict(name: str, body: dict[str, Any]) -> NamespaceBinding:
    declared_by_raw = body.get("declared_by", []) or []
    declared_by: list[tuple[str, tuple[str, ...]]] = []
    for entry in declared_by_raw:
        if isinstance(entry, dict):
            table = entry.get("table", "")
            cols = tuple(entry.get("columns") or [])
            if table and cols:
                declared_by.append((table, cols))
        elif isinstance(entry, str) and "." in entry:
            # legacy string format (pre-fix plans); best-effort single-column
            # assumption for backward compatibility during the migration window.
            table, col_part = entry.split(".", 1)
            declared_by.append((table, (col_part,)))
    return NamespaceBinding(namespace=name, declared_by=tuple(declared_by))
```

This is a breaking YAML format change for the `namespaces` block. Pre-GA hard
delete applies (best-practices §8.1). The legacy `elif` branch can be removed
once manifests are regenerated.

---

### F3 -- MEDIUM | Security / Design
**`config/_transforms.py` `FilterOp`/`DeriveOp`: no compile-time expression parse, broader eval surface than formula masking**

`FilterOp.expression` and `DeriveOp.expression` are validated only for
`min_length=1`. Both run at execution time via `pandas.DataFrame.eval()`. The
`expressions.py` module explicitly calls this out:

> "The pandas DataFrame.eval() used by derive.py is intentionally excluded:
> it runs the pandas/NumPy expression engine, not this evaluator, and has a
> distinct security profile owned by the derive op directly."

Two concrete gaps:

1. **No syntax check at config-parse time.** A malformed expression like
   `age >=` (missing RHS) passes `PipelineConfig.model_validate()` and the
   full compile-check suite. The error surfaces mid-job when the transform
   op runs, giving a confusing `numexpr.necompiler.NumExprCachingError` or
   similar rather than a config error at the boundary.

2. **Engine defaults to numexpr but `engine='python'` is not blocked.** With
   `engine='python'`, pandas eval can invoke arbitrary callables via `@func`
   local-variable injection. If the platform exposes transform configs to
   users who can also inject local variables into the eval frame, the surface
   widens. This is environment-dependent: pure data-engineer use is lower
   risk; a future "smart filter" UI feature that hands user-supplied strings
   directly to `DeriveOp` is higher risk.

**Impact:** Short-term: surprising mid-job parse errors. Longer-term: if the
platform ever exposes transform ops to untrusted config authors, the expression
is not sandboxed at the simpleeval level.

**Fix (immediate -- compile-time syntax check):**  
Add a `model_validator(mode="after")` to `FilterOp` and `DeriveOp`:

```python
from pydantic import model_validator
import pandas as pd
import numpy as np

class FilterOp(BaseModel):
    ...
    @model_validator(mode="after")
    def _parse_expression(self) -> FilterOp:
        """Syntax-check at config time; catches malformed expressions before
        they fail mid-job with an opaque numexpr error."""
        dummy = pd.DataFrame({"_": [0]})
        try:
            dummy.eval(self.expression)
        except Exception as exc:
            raise ValueError(
                f"FilterOp expression {self.expression!r} failed "
                f"pandas.eval parse: {exc}"
            ) from exc
        return self
```

Same pattern for `DeriveOp`. The dummy frame means column references that exist
at runtime but not at parse time will raise `UndefinedVariableError`; those
should be caught and ignored (the expression is syntactically valid).

```python
    @model_validator(mode="after")
    def _parse_expression(self) -> DeriveOp:
        import pandas as pd
        dummy = pd.DataFrame({"_": [0]})
        try:
            dummy.eval(self.expression)
        except pd.core.computation.ops.UndefinedVariableError:
            pass  # column refs are valid; they just don't exist in the dummy
        except Exception as exc:
            raise ValueError(
                f"DeriveOp expression {self.expression!r} is not a valid "
                f"pandas.eval expression: {exc}"
            ) from exc
        return self
```

**Fix (longer-term):** Evaluate whether to lock `engine='numexpr'` at the
execution layer and document that `@`-prefixed local-variable injection is
not part of the supported surface.

---

### F4 -- MEDIUM | Design
**`plan/_compile.py` `_build_seed_envelope`: `when_with_coherent_with` check is dead code**

`compile_plan` calls `_check_when_with_coherent_with(config)` early in its body
(before reaching `_build_seed_envelope`). That function raises `PlanCompileError`
on any column with both `when:` and `coherent_with:` set. Execution never reaches
`_build_seed_envelope` for an ill-formed config.

However, `_build_seed_envelope`'s per-column loop contains a second, identical
check:

```python
# _build_seed_envelope, inside the per-column loop
if when is not None and coherent_with:
    raise PlanCompileError(
        code="when_with_coherent_with_unsupported",
        ...
    )
```

This check is unreachable: `_check_when_with_coherent_with` always fires first.

**Impact:** Dead enforcement code is a maintenance hazard. A future refactor
that moves or removes `_check_when_with_coherent_with` (e.g., to combine all
config checks into a `_run_all_config_checks` helper) could silently lose the
first call, leaving only the dead copy inside `_build_seed_envelope`. Or, if
`_build_seed_envelope` is ever called directly (e.g., from a test fixture that
bypasses `compile_plan`), the second check would fire but `_check_when_with_coherent_with`
never would -- an asymmetric enforcement gap.

**Fix:** Remove the duplicate check from `_build_seed_envelope`. If defense-in-depth
is desired for direct callers, add a brief comment:

```python
# when + coherent_with is already rejected by _check_when_with_coherent_with
# in compile_plan; no re-check needed here.
```

Alternatively, extract a `_reject_when_coherent_with(col_entry, table_name, col_name)`
helper called from BOTH places, so the logic is in one location and both call
sites are explicit rather than one being a silent duplicate.

---

### F5 -- LOW | Correctness
**`plan/_compile.py` `_build_seed_envelope`: invalid `backend_type` silently coerces to `"faker"` without warning**

In `_build_seed_envelope`, when `reg_caps is None` (i.e. no provider registry
match -- defensive path for scalars without providers), user-declared
`backend_type` is used but silently sanitized:

```python
backend_type_raw = col_entry.get("backend_type", "faker")
backend_type = (
    backend_type_raw
    if backend_type_raw in ("faker", "mimesis", "pool", "decoy_native")
    else "faker"  # <-- silent fallback
)
```

A config with `backend_type: fkr` (typo) or `backend_type: custom` (unsupported
value) compiles without error. The plan stamps `backend_type: faker`, the
operator sees no indication anything was wrong, and the determinism stamp is
based on an incorrect backend type.

**Impact:** Low in practice -- this path only fires when `reg_caps is None`
(provider-less scalar strategies like hash/redact/truncate, which don't declare
`backend_type`). But an operator who hand-edits a column to add a non-standard
`backend_type` gets a silently different plan than what they declared.

**Fix:** Emit a warning into the `warnings` list (same pattern as the
`backend_stamp_user_override_ignored` warning):

```python
if backend_type_raw not in ("faker", "mimesis", "pool", "decoy_native"):
    warnings.append(
        f"backend_type_coerced_to_faker: column "
        f"{table_profile.name}.{col_name} declared "
        f"backend_type={backend_type_raw!r}; value is not in the allowed set; "
        f"coerced to 'faker'. Set an explicit backend_type or remove the field."
    )
    backend_type = "faker"
else:
    backend_type = backend_type_raw
```

Alternatively, raise `PlanCompileError(code="invalid_backend_type", ...)` if the
operator intent should always be honored exactly.

---

### F6 -- LOW | Reliability
**`plan/validate/_consolidator.py` `validate_plan`: docstring implies fully exception-safe, but unexpected exceptions escape**

`validate_plan` catches `PlanCompileError` and `PoolCapacityError` and wraps them
in `PlanValidationResult(ok=False, ...)`. Any other exception from `compile_plan`
(e.g., `KeyError` on a missing required profile field, `TypeError` from a config
shape that slips past Pydantic, `AttributeError` from a future bug in any of
the nine checks) propagates uncaught.

The function's docstring says:

> "Returns a `PlanValidationResult` rather than raising: a failing check yields
> `ok=False` + a `PlanCheckError`; a clean compile yields `ok=True` with the
> `Plan` attached."

This implies callers should not need a try/except. The implied contract and the
actual behavior diverge for unexpected exceptions.

**Impact:** Platform callers that rely on `validate_plan` being exception-safe
would receive an unhandled exception from code they expect to return
`PlanValidationResult`. The S10 slice introduction suggests this is consumed at
job-validation endpoints.

**Fix:** Clarify the docstring to state explicitly that only typed compilation
errors are returned as structured failures; unexpected exceptions still propagate.
Add to the docstring:

> Note: only `PlanCompileError` and `PoolCapacityError` are caught and returned
> as structured failures. Any other exception (unexpected bug, malformed profile
> type, etc.) propagates to the caller. Callers that need full exception isolation
> should wrap in `try/except Exception`.

This sets an honest contract without swallowing genuine bugs.

---

### F7 -- NIT | Correctness
**`plan/_serialize.py` `_seed_envelope_from_dict`: `job_seed` hex length not validated**

```python
return SeedEnvelope(job_seed=bytes.fromhex(data["job_seed"]), per_table=per_table)
```

All plan-compiled envelopes have exactly 8-byte (16 hex-char) seeds, but a
hand-edited or externally-sourced YAML with `job_seed: 0000000000000000ff`
(18 chars / 9 bytes) deserializes silently to a 9-byte seed. Downstream
`derive(seed_envelope.job_seed, ...)` would produce output from the wrong key.

**Fix:**

```python
job_seed_hex = data["job_seed"]
if len(job_seed_hex) != 16:
    raise ValueError(
        f"plan_from_yaml: job_seed must be exactly 16 hex chars (8 bytes), "
        f"got {len(job_seed_hex)!r}"
    )
return SeedEnvelope(job_seed=bytes.fromhex(job_seed_hex), per_table=per_table)
```

---

### F8 -- NIT | Type Safety
**`plan/_types.py` `ColumnSeed.distribution_behavior: str | None` should be a `Literal`**

`ColumnSeed.distribution_behavior` accepts any string. Valid values per
`execution/_distribution_behavior.py` are `"preserves_all"`,
`"destroys_frequency"`, `"inherits"`, and `None`. An accidental misspelling
in `distribution_behavior_for()` or a new strategy that forgets to register
would silently produce an invalid classification string that flows into the
plan manifest and the FE drift-badge logic without error.

**Fix:**

```python
DistributionBehavior = Literal["preserves_all", "destroys_frequency", "inherits"]

@dataclass(frozen=True)
class ColumnSeed:
    ...
    distribution_behavior: DistributionBehavior | None = None
```

Verify the literal values against `_distribution_behavior.py` before landing.

---

## 3. Performance Notes

All plan-compile paths are one-shot per job execution. No hot-loop or throughput
critical code in this layer. Complexity observations:

- `_build_relationships`: O(R) where R = `len(profile.relationships)`. Two
  passes: one to build `grouped`, one over `sorted(grouped.items())`. The
  `sorted()` sorts R distinct keys worst-case; for realistic FK graphs (R < 100)
  this is negligible.
- `_build_seed_envelope`: O(T * C) where T = tables, C = columns. Iterates
  `profile.tables` once with per-column inner loops. No redundant passes.
- `check_null_bearing_int_unsupported` / `check_basic_uniqueness_pre_flight`:
  each builds a `dict` lookup from `profile.tables` then iterates `config.tables`.
  O(T*C) with O(1) lookups. Fine.
- `_hash_config`: `json.dumps` on the config dict is O(N) where N is config
  size. `hashlib.sha256` is O(N). Called once per compile. Not a bottleneck.

No profiling recommended for this layer.

---

## 4. Suggested Tests

```
tests/unit/test_plan_compile_determinism.py
  - test_relationship_children_order_stable_under_profile_reorder
      Two Profile objects with same relationships in different list order;
      compile_plan produces Plan objects that are == and YAML-identical.
      Directly tests F1 fix.

  - test_seed_envelope_job_seed_16_bytes
      plan_from_yaml(plan_to_yaml(plan)).seed_envelope.job_seed has len 8.
      After F7 fix: plan_from_yaml on a YAML with wrong-length hex raises.

tests/unit/test_plan_serialize_roundtrip.py
  - test_namespace_binding_roundtrip_double_underscore_column
      NamespaceBinding with a column named 'account__id' (single-element
      tuple): plan_to_yaml -> plan_from_yaml -> assert original == result.
      Currently FAILS (F2).

  - test_namespace_binding_roundtrip_composite_fk
      NamespaceBinding with cols=("first_name", "last_name"); round-trip.
      Should pass now and after the F2 fix.

tests/unit/test_config_transforms.py
  - test_filter_op_rejects_malformed_expression
      FilterOp(op="filter", expression="age >=") raises ValidationError.
      Tests F3 fix.

  - test_derive_op_allows_valid_column_ref
      DeriveOp(op="derive", column="arpu", expression="revenue / users")
      passes validation even though 'revenue' and 'users' are not in the
      dummy frame (UndefinedVariableError is permitted).

tests/unit/test_plan_compile_edge_cases.py
  - test_backend_type_typo_emits_warning_not_silent_coercion
      Config with backend_type='fkr' on a scalar strategy column; after
      F5 fix: plan.plan_compile.warnings contains 'backend_type_coerced'
      entry rather than silently producing backend_type='faker'.

  - test_when_coherent_with_duplicate_check_not_reached
      Confirm that a column with when + coherent_with raises PlanCompileError
      BEFORE reaching _build_seed_envelope (property test: mock
      _build_seed_envelope and assert it is never called when the bad
      config is present).
```

---

## 5. What's Good

- **`_normalize_job_seed`** is thorough: the bool+float guards (QA-3 F1) and
  the overflow check prevent the subtle PyYAML `seed: true` / `seed: 1.5`
  silent coercions that bit pre-QA-3. One function owns this logic, no duplication.

- **`_hash_config` excludes `sources`/`targets`** deliberately. The separation
  of masking semantics from data binding is the right call for audit matching
  across different deployment environments.

- **`plan/_checks.py` FK-child exemption** in `check_null_bearing_int_unsupported`
  is correct and aligned with the execution-time guard. The join path doesn't
  suffer the int+null-to-float64 ambiguity because it uses the pandas oracle on
  both substrates.

- **`PlanRelationship.__post_init__`** enforces non-empty children and
  matching column lengths at construction time -- the invariants are checked
  at the dataclass layer, not just at the compile check layer. Defense in depth.

- **`validate_plan` as a thin wrapper** over `compile_plan` is exactly the
  right design. Re-deriving the check orchestration (nine interdependent checks
  with specific ordering requirements) behind a parallel registry would
  inevitably drift. The wrapper adds zero duplication.

- **`config/_transforms.py` `TableConfig._mask_xor_generate`** catches the
  `drop_column`-vs-mask conflict at the Pydantic validation choke-point with
  a named, actionable error message. This is the right level to catch it.

- **`errors.py` `ValidationError.raw_message`** property separates the wrapped
  path-annotation from the original message so consumers can access both without
  string-parsing. Clean API.
