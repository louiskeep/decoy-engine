# Mutation grading: `execution/_strategies/_composite.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `_composite.py` (145 LOC) is the composite
strategy handler: `CompositeHandler.run` resolves the composite provider
(composite_person / composite_address / composite_provider / composite_custom),
fails closed on an unresolved namespace or a non-`ColumnSeed` plan slice or an
unknown provider or a malformed custom bundle, routes to the matching multi-column
generator, and writes the generated columns back (raising
`composite_output_column_missing` if a routed column is absent).

**Grade scope: FOCUSED selection only** (`tests/unit/execution/test_composite_routing.py`).

## Numbers

**122 mutants: 43 killed (35% baseline), 79 survived -> 108 killed after this
pass, 14 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts. The weak 35% baseline
was because the pre-existing suite only covered routing shape, not the guard
codes, the four provider routes' output contracts, or the source-keying/config
flow.

- **65 LOGIC survivors killed** with 11 tests. 0 product bugs.
- **14 EQUIVALENT survivors** (message prose + one dead field). All verified
  behavior-preserving.

**Dennis gate P1 remediation (2026-07-26):** the batch-gate FAILED an earlier
cut of this ledger that classified the 10 `registry=None` mutants
(`28/30/41/43/49/51/57/59/81/84`) EQUIVALENT. That was WRONG: `ctx.registry`
traces to the public `run_pipeline(registry=...)` override knob
(`ProviderRegistry.override`), and every route consumes it via
`PoolBuilder(registry)`, so a custom sub-provider override changes composite
output (proven: overriding `person_first_name` yields `first_name == ["ZZZ", ...]`
where the default gives real names). They are LOGIC, now killed by
`TestCompositeHonorsCustomRegistry` (a parametrized override-honored test per
route). Re-verified: 108 killed / 14 survived, the 14 being exactly the
message-prose + dead-field equivalents.

## LOGIC (55): killed by new tests

All in `tests/unit/execution/test_composite_routing.py`.

| Test | Kills | Pins |
|---|---|---|
| `TestCompositeGuardCodes::test_unresolved_namespace_code` | 7, 9, 11, 12 | `code == "composite_namespace_unresolved"` on the None-namespace guard |
| `::test_non_columnseed_plan_slice_code` | 17, 19, 21, 22 | `code == "unsupported_strategy"` when `plan_slice` is a GroupSeed, not a ColumnSeed |
| `::test_unknown_provider_code` | 85, 87, 89, 90 | `code == "unsupported_strategy"` on the unknown-provider else |
| `::test_custom_bundle_not_list_code` | 71, 73, 75, 76 | `code == "composite_custom_bundle_shape"` when `bundle` is not a list |
| `TestCompositePersonRouting` | 36, 37, 38, 39, 40, 42 | composite_person routes/builds (email == first.last, dob present); mis-route / None-generator / None-namespace / dropped kwarg all raise |
| `TestCompositeAddressRouting` | 44, 45, 46, 47, 48, 50 | composite_address routes; (city, state, zip) is a real locality triple + street written |
| `TestCompositeProviderRouting` | 52, 53, 54, 55, 56, 58 | composite_provider routes; 3 columns written, run-stable |
| `TestCompositeCustomRouting` | 60-70, 78, 79, 80, 82, 83 (16) | composite_custom routes; `cfg=dict(provider_config)`, `bundle_decl=cfg.get("bundle")`, and the `composite_custom(...)` call are load-bearing (bad key / None / `and` / wrong-arg all error or drop the bundle) |
| `TestCompositeSourceKeying::test_output_depends_on_first_sorted_column` | 91, 104, 109, 113 | deterministic path is source-keyed on `columns[0]`; varying only that column changes output (kills forced-non-det + the `columns[1]` index) |
| `TestCompositeProviderConfigFlow::test_email_format_from_provider_config` | 101 | `extra=dict(col_seed.provider_config)` actually flows to the generator (email_format override) |
| `TestCompositeHonorsCustomRegistry::test_route_output_reflects_registry_override` | 28, 30, 41, 43, 49, 51, 57, 59, 81, 84 | each route's `registry=ctx.registry` factory arg is load-bearing: a sentinel override of a route's sub-provider (`person_first_name` / `address_street` / `person_name`) must surface in the output; `registry=None` would fall back to the default and ignore the override |

Robustness of the routing kills: a mis-route produces the wrong output-column set
and trips `composite_output_column_missing` / `unsupported_strategy`, and a
`coherent_namespace=None` makes `derive()` raise `namespace_empty`. The
`columns[1]` / forced-non-det kills use a cross-run assertion where only
`columns[0]` differs, so the non-deterministic path (fully determined by
seed+count) collapses to equal output and the mutant is detected.

## EQUIVALENT (24)

| Mutants | Category | Why equivalent |
|---|---|---|
| `8`, `10`, `13`, `14`, `18`, `20`, `72`, `74`, `77`, `86`, `88`, `116`, `118` | error-message prose | the raised `StrategyError`'s `code` is the asserted machine field (guard-code tests above + the existing `test_missing_output_column_raises`); the message text (incl. `type(None).__name__` in one) is never machine-consumed. |
| `93` | dead field | `ProviderSpec(deterministic=None)`: no composite generator reads `spec.deterministic` (generators branch on the local `deterministic` var); `ProviderSpec.__post_init__` only validates when it is truthy, so `None` just skips validation with no output change. |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `_composite.py`, selection to
`test_composite_routing.py`, then `rm -rf mutants && python -m mutmut run`.
