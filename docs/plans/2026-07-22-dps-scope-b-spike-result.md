# DPS Scope B: dependency spike result (STOP)

**Date:** 2026-07-22
**Spike scope:** guide section 3.2, run against `docs/plans/2026-07-22-dps-scope-b-implementation-guide.md` before any adapter code.
**Result:** STOP per guide section 3.3 / 9.3. No adapter, budget, or fit code was written. `pyproject.toml` is unchanged.

## What passed

1. **Wheel resolution across the supported Python range.** `pip download --only-binary=:all: --no-deps opendp==0.15.1` resolved the identical `opendp-0.15.1-cp310-abi3-manylinux_2_28_x86_64.whl` for Python 3.10, 3.11, and 3.12 (the wheel's `abi3` tag covers all three; verified against `pyproject.toml`'s declared `requires-python` and classifier matrix).
2. **Clean install alongside Decoy's existing dependency set.** `opendp==0.15.1` installed into the repo's real dev venv (`.venv`, Python 3.10.20, already carrying pandas/polars/pyarrow/duckdb/etc.) with a single new transitive dependency (`deprecated`). No resolver conflict was reported.
3. **Exported mechanism name matches the guide's assumption.** `opendp.measurements` exports `make_laplace_threshold` and `make_gaussian_threshold` in 0.15.1, matching section 3.2's assumed name exactly (confirmed both via `docs.opendp.org/.../thresholded-noise-mechanisms.html` and direct introspection: `dir(dp.m)`). `opendp.transformations` exports `make_count_by` (unknown-key grouped count) as assumed. No name-mismatch failure.
4. **The compositor pattern itself is sound.** Built one `dp.Context.compositor(...)` over a mixed numeric+categorical fixture, ran a bounded numeric histogram query and an unknown-key categorical `make_count_by`+threshold query through it, read the composed privacy loss off the context, and confirmed a third unscheduled query is refused (`ValueError: Privacy allowance has been exhausted`). This is the exact section 3.2 smoke checklist, and it works -- **but only under one specific Polars version** (see below).

## What failed: Polars integration conflicts with Decoy's dependency range

Section 3.3 and 9.3 both name this as an explicit stop condition and flag it as unverified at authorship time ("The exact OpenDP Polars compatibility range was not verified from repository source during authorship... The implementer must verify it in the mandatory spike"). It is now verified, and it fails.

**The only OpenDP-native way to run one Context compositor over a mixed-column table schedule (row count + N numeric columns + 2M categorical columns, section 4.3's binding design) is `opendp.extras.polars`'s LazyFrame-integrated `Context.compositor`.** Confirmed by enumerating `opendp.extras`: the only tabular/multi-column submodules are `polars`, `numpy`, `sklearn`, `mbi`. There is no non-Polars multi-column/record domain in the core package. The base transformations (`make_count_by`, numeric histogram builders) operate on single homogeneous `VectorDomain<AtomDomain<T>>` columns; nothing in core OpenDP composes heterogeneous per-column queries against one shared table object except the Polars LazyFrame domain. Building N+2M separate per-column compositors instead would mean manually summing their independently-reported budgets back into one artifact-level total -- exactly the "manual floating-point composition" the guide forbids as a response to a spike failure (section 3.3), so it is not an available workaround, it is the same defect in a different shape.

The Polars LazyFrame path requires an **exact** Polars version match, not a range. `opendp[polars]==0.15.1`'s metadata pins `polars==1.36.1` (verified via `importlib.metadata`), and the underlying Rust FFI layer embeds a compiled `DSL_SCHEMA_HASH` for that exact Polars release. Decoy's `pyproject.toml` declares `polars>=1.0,<2.0`; the repo's own `.venv` and `uv.lock` currently resolve `polars==1.42.0`.

Running the section 3.2 smoke script against Decoy's actual resolved Polars version reproduces a hard failure at `Context.compositor()` construction, before any query executes:

```
opendp.mod.OpenDPException:
  FFI("Installed python polars version (1.42.0) != expected version (1.36.1).
  Error when deserializing 'DslPlan'. This may be because you're using
  features from Polars that are not currently supported. deserialization failed
  given DSL_SCHEMA_HASH: 5e62ab1f... is not compatible with this Polars version
  which uses DSL_SCHEMA_HASH: 4aade69a...
  error: can't deserialize DSL with incompatible schema")
```

Verified this is deterministic and Python-version-independent, not an artifact of one environment:

| Environment | Polars resolved | Result |
|---|---|---|
| Repo `.venv` (Python 3.10.20, Decoy's real dependency graph) | 1.42.0 | FFI DSL schema mismatch, fails |
| Fresh venv, Python 3.12, `polars>=1.0,<2.0` (Decoy's stated range) | 1.43.0 | Same FFI DSL schema mismatch, fails |
| Fresh venv, Python 3.10, `polars==1.36.1` exactly (OpenDP's own pin) | 1.36.1 | Full smoke checklist passes (histogram + categorical + composed loss + refused extra query) |

Also checked, for context only, whether a newer OpenDP build widens the Polars pin: the newest available release on PyPI as of 2026-07-22, including the `0.15.1.dev20260714001` pre-release ahead of the current stable `0.15.1`, still declares `polars==1.36.1` exactly for the `polars` extra. This is not a version-bump-away problem; it is a standing characteristic of how the compiled core embeds a Polars DSL schema, and it will recur at whatever OpenDP/Polars pair is current until the two projects' release cadences line up or OpenDP widens its own compatibility window.

## Disposition

Per guide section 3.3 ("Stop before adapter work if: ... OpenDP's required Polars integration conflicts with Decoy's supported dependency range") and section 9.3 ("Stop and escalate before changing Decoy's existing `polars>=1,<2` constraint or adding `opendp[polars]`"): this is a stop, not a design problem to route around. No production code was touched -- `quality/dp.py`, `quality/dp_budget.py`, `plan/_checks_dp.py`, `plan/_types.py`, `plan/_compile.py`, `generation/synthesize.py`, and `pyproject.toml` are all unchanged from `6728e3d`.

Per the guide's own instructions and the standing "do not hand-roll DP math" rule, the responses that are explicitly *not* available here: pinning Decoy's `polars` dependency down to `==1.36.1` unilaterally (a frozen-surface-adjacent dependency change affecting every other Polars-substrate code path in the engine, and section 9.3 requires escalation before it), silently falling back to `opendp[polars]`'s pin in a way that would break Decoy's own Polars-adapter tests, or hand-building a non-Polars multi-column compositor equivalent (the "manual composition" the guide forbids).

## Options for a human decision (not mine to pick)

1. **Pin Decoy's `polars` dependency to exactly `1.36.1`** for the DP path (or engine-wide), accepting whatever regression risk that carries against the existing Polars-substrate code and its own compatibility testing. Needs its own reviewed dependency change, separate from this build, per section 9.3.
2. **Wait for/track an OpenDP release whose `polars` extra widens past an exact pin**, or file an upstream OpenDP issue asking for a version-range pin instead of an exact one — no such release exists today per the PyPI check above, including pre-releases.
3. **Re-scope Scope B's architecture away from the Polars-integrated Context** and toward per-column compositors with a different, explicitly-designed budget-composition strategy that is not "manual floating-point summation" in spirit — this is a design change to the guide itself, not an implementer's call under the current instructions ("The implementer may not renegotiate these decisions", section 2; the single-compositor mixed schedule is section 4.3's binding design).
4. **Re-open the library choice** for the categorical/numeric marginal path specifically (e.g., Google `dp_accounting` for the accountant, OpenDP only for individual non-Polars-tabular measurements) if a workable non-Polars mixed-schedule composition shape can be designed — again a guide-level redesign, not an implementer substitution the binding decisions in section 2 permit.

## Spike artifacts (not committed, for reproducibility on request)

Wheel downloads, verification venvs, and the smoke script used above ran under `/tmp/claude-1000/-home-cam/88bef986-97ee-41ef-a1d8-8225f41d5b8d/scratchpad/dps-spike/` (session scratchpad, not part of the repo). The smoke script is reproducible from the commands in this document plus the "confirmed" checklist in the "What passed" section; nothing in it depends on scratchpad-local state beyond ordinary `pip`/`uv` installs.
