# Task 0.6 report: parity harness and golden matrix fixtures

Status: DONE

Branch: `feat/native-phase0` (worktree `.claude/worktrees/native-phase0`)

## What shipped

The last Phase 0 piece: a pinned-oracle parity harness plus a live-registry
golden matrix. Two new files, both under the 600-LOC sentry:

- `tests/parity/native/_fixtures.py` (476 LOC): the harness (`LogicalResult`,
  `PhysicalDiff`, `run_oracle`, `run_candidate`, `assert_logical_parity`) and
  the matrix generators (`STRATEGY_MATRIX`, `PROVIDER_MATRIX`).
- `tests/parity/native/test_parity_matrix.py` (211 LOC): the harness self-test,
  the registry-completeness tests, the oracle smoke test, and the native-parity
  matrix tests.

## The harness

- `run_oracle(config, sources)` PINS the oracle: `substrate="pandas"`,
  `execution_mode="full_frame"`, `auto_chunk=False`. Single ground truth; does
  not drift with routing changes.
- `run_candidate(config, sources)` routes through the native substrate
  (`substrate="native"`), pinned like the oracle in every other dimension. That
  substrate is not wired yet (`VALID_SUBSTRATES == ("pandas", "polars")`), so it
  raises `invalid_substrate` today. That is the flip switch: as each phase wires
  native execution and reaches parity, that strategy's cases start passing. One
  module constant (`_NATIVE_SUBSTRATE`) localizes any future repoint.
- `assert_logical_parity(candidate, oracle, *, allowed_physical_diffs)` compares
  EXACTLY: output-table set, per-column values, null positions, row order,
  diagnostics (warnings AND row errors, as order-independent multisets), and the
  logical schema (column names + order). Arrow-type differences are rejected
  unless an enumerated `PhysicalDiff` allows the exact transition. Values are
  compared via positional `to_pydict()` equality; the Arrow type is compared
  separately so a width drift cannot hide behind value equality.

## Strictness (self-test)

`TestHarnessStrictness` proves the harness is not loose. It PASSES on an exact
match and on the one enumerated null-typed normalization, and genuinely FAILS
(via `pytest.raises(AssertionError)`) on each of the six divergence kinds:
differing value, reordered row, null-vs-value swap, missing warning, missing row
error, and an un-enumerated Arrow type difference (string -> large_string). A
ninth test confirms the null-typed allowance does not admit a non-null width
drift.

## The enumerated physical differences allowed

Exactly one, by default (`DEFAULT_ALLOWED_PHYSICAL_DIFFS`):

- `null_typed_normalization`: admits a column that is Arrow `null`-typed on one
  side and a concrete type on the other (XOR: exactly one side is null-typed).
  This is the SPECIFIC normalization `concat_masked_chunks`
  (`execution/_chunked.py`) already performs: a chunk whose column is entirely
  null returns from pandas as Arrow `null` type, while the full frame infers the
  concrete type its non-null chunks agree on; casting null -> that concrete type
  is lossless (every value is null) and lands exactly where whole-frame
  inference does.

  Grounding in `tests/parity/SEMANTIC_DIFFERENCES.md`: rows v1/v2 (v2 section),
  which establish that Arrow type width is representational, not logical. This
  is the narrowest instance of that class (a null-typed vs a concrete-typed
  all-null column), NOT the generic string/large_string widening those rows also
  describe. Generic Arrow widening is deliberately NOT in the default allow-list;
  a phase that needs one must add an explicit `PhysicalDiff` by decision.

## The matrix (generated from the LIVE registries)

- Strategies enumerated from `SCALAR_HANDLERS`: 24 live -> 25 `STRATEGY_MATRIX`
  cases (hash carries a null and a dtype_int variant); 13 run the oracle today.
  The 12 strategies without a minimal single-column fixture (whole-column or
  composite shapes: bucket_perturb, code_set, derived, derived_aggregate,
  geo_generalize, group_key, grouped_series, joint_mask, nested, text_mask,
  top_code, windowed_date) are still enumerated as cases so a new one cannot be
  silently omitted; they are native-migration-deferred regardless.
- Providers enumerated from `ProviderRegistry.known_providers()`: 34 live -> 34
  `PROVIDER_MATRIX` cases; 14 run the oracle today (poolable faker providers).
  Non-poolable faker and non-faker (decoy_native / composite) bindings are
  enumerated but not oracle-run. Each case is labeled with its backend and its
  Task 0.5 `classify_provider` class.
- The plan's note of "26 providers" is stale; the live registry has 34. This is
  exactly why the task forbids hardcoding a count. Because the matrix generator
  iterates the live registry directly, a newly added strategy or provider cannot
  be silently omitted: it auto-appears as an xfail native-parity case. The two
  completeness tests (`covered EQUALS live`) are therefore tautological with
  respect to additions and stay green when a newcomer is added; they act as a
  generator-regression guard that fires only if a future refactor makes the
  generator drop a registry member. The anti-omission property holds via live
  generation, not via a red suite.

Variants present across the matrix: null (passthrough/redact/hash/date_shift/
categorical/text_redact/faker), dtype (hash int64, bucketize int64), and
seed-mode (deterministic fpe/shuffle/categorical/faker).

Native-parity cases are marked `xfail(strict=False)`: complete now, each flips to
PASS (as an xpass, which strict=False tolerates) when its native phase lands.

## Test summary

`PYTHONPATH="$(pwd)/src" .venv/bin/python -m pytest tests/parity/native/ -q`:
38 passed, 59 xfailed. The 38 passed = 9 strictness self-tests + 2 completeness
+ 27 oracle-smoke (13 strategy + 14 provider). The 59 xfailed = the native-parity
matrix (25 strategy + 34 provider). The whole `tests/parity/` tree still collects
cleanly (347 tests).

Linters: `ruff check`, `ruff format --check`, and `mypy` all clean on both
modules. Test-source binding verified: the worktree venv resolves
`decoy_engine` to the worktree `src`, not the parent checkout.

## Concerns

- The candidate routes through `substrate="native"`, which is my best read of
  where the native efficiency work will plug in; it is not yet defined by any
  landed code. If a later phase exposes native execution through a different
  mechanism (a new `execution_mode`, a separate entry point), that phase must
  repoint `_NATIVE_SUBSTRATE` / `run_candidate`. Until then every native-parity
  case is xfail, which is the intended Phase 0 state. I flagged it here rather
  than guess a token the codebase has not committed to.
- 12 strategies and 20 providers are enumerated but not oracle-run (no minimal
  single-column fixture, or a non-poolable / non-faker binding). They are still
  covered for completeness and appear as xfail native-parity cases. As their
  phases land, a fixture author should add a runnable spec so the oracle smoke
  and the flipped native case exercise them for real.
- Diagnostics are compared as order-independent multisets. If a later phase must
  assert diagnostic ORDER, that is a stricter comparison than the current
  contract and would need an added option.
