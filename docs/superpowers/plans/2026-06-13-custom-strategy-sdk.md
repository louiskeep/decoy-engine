# Custom-Strategy SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user register a custom masking algorithm
(`class MyHandler(StrategyHandler)`) and reference it in config as
`strategy: "custom:my_handler"`, with built-ins, `nested` children, and custom
handlers all resolving through one registry, and unregistered names rejected at
compile time instead of mid-job.

**Architecture:** Introduce a single `STRATEGY_REGISTRY` in a new
`execution/_plugin_registry.py` that imports only the `_adapter` Protocol types
and stdlib. Built-ins push themselves into it at import; `nested` and the pandas
adapter pull from it (inverting today's circular `SCALAR_HANDLERS` import). User
handlers register via `register_strategy()` or `importlib.metadata` entry points
(the PyPA plugin pattern used by pytest/flake8/sphinx). The compile-time
`unknown_strategy` check is **only safe after** a fixture migration that
classifies every strategy string currently in the test tree — that migration is
Part A and is the de-risking prerequisite.

**Tech Stack:** Python 3.10-3.12, `importlib.metadata` entry points, the
in-repo `register_faker_provider_v2` precedent, pandas execution adapter,
pytest, mypy, import-linter.

---

## Plan verification (2026-06-13, against live engine `main @ 3e7d0d3`)

Every file/line reference below was checked against source. Confirmed: the
`StrategyHandler.run(self, df, column, plan, ctx)` 4-param contract
(`_adapter.py:97-103`), `StrategyContext` fields (`_adapter.py:85-89`), the
adapter construction + fpe override (`_pandas_adapter.py:97-98`), the two compile
check sites (`_compile.py:138,309`), the `register_faker_provider_v2` precedent
(`providers_v2/_faker_adapter.py:226`), and the nested recursive guard
(`_nested.py:110-115`). Corrected after verification (already folded into the
tasks below):

- **Config shape is a LIST**, not nested dicts: `config["tables"]` is
  `[{"name": ..., "columns": [{"name": ..., "strategy": ...}]}]`. The B7 check
  and its test use this shape and inline the walk (there is **no**
  `_iter_masking_columns` helper; each check inlines, mirroring
  `check_unknown_provider:70-92`).
- **`PlanCompileError(code=, path=, message=)`** takes a `path=` arg (B7).
- **`_nested` resolves via `child_strategy_name`** and raises `StrategyError`
  (not the registry error); B4 preserves that exact error.
- **`sdk.py` also re-exports `ColumnSeed`** (`plan/_types.py:51`) and
  **`QualityWarning`** (`generation/pool/_events.py:15`). This widens the public
  surface; per `docs/compatibility-contract.md` it is PO-gated and frozen once
  shipped.
- **`examples/` is untracked** (`git ls-files examples` = 0). B8 does not import
  it as a package; it runs the file. Pre-existing gap flagged below.

> **Pre-existing issue surfaced during this review:** the entire `examples/` dir
> is untracked in git, yet ROADMAP Sprint 3 makes "bundled examples run green on
> a fresh `pip install`" a release gate. Those examples are not in the package.
> This is independent of item 9 and should be raised with the PO / folded into
> Sprint 3.

## Increment structure (eng-review decision, 2026-06-14)

Per the plan-eng-review scope challenge, the work ships as **three independent
increments**, smallest blast radius first. Each lands, is tested, and is reviewed
on its own branch before the next starts. This isolates the PII-critical `_nested`
rewire from the safe additive SDK.

```
INCREMENT 0 — Census spike (FIRST, zero commitment)   [outside-voice re-sequence]
  Task: A1 (strategy-name census) ONLY, run as a spike before anything is frozen.
  Why first: A1 costs nothing and sizes the real migration blast radius. Increment 1
  freezes the public decoy.sdk surface (PO-gated), so we must know the census is
  small BEFORE that freeze. If the census comes back large/ugly, re-decide scope.

INCREMENT 1 — Additive SDK core (low blast radius)
  Tasks: B1 (registry, incl. ensure_builtins_loaded), B2 (sdk.py), B3 (built-ins
         via registry), B5a (adapter builds from registry, NO discovery yet),
         B8 (example+docs, direct registration only; import-linter contract).
  Outcome: register_strategy() + decoy.sdk + a custom strategy masks via the
  adapter. Nothing existing changes behavior; SCALAR_HANDLERS stays a backed alias.

INCREMENT 2 — Nested + discovery + polars coverage (core-touching)
  Tasks: B4 (nested import-cycle inversion), B6 (entry-points discovery WITH
         per-plugin isolation), B5b (wire discovery into adapter construction),
         POLARS (regression test for the shared _nested change + custom-on-polars
         falls back to the pandas oracle, tested), example gains nested-child line.
  Outcome: custom strategies work as nested children, on BOTH substrates, and
  auto-register from installed packages.

INCREMENT 3 — Compile-time safety (the fixture migration)
  Tasks: A2-A4 (fixture migration; A1 already ran in Inc 0), B7 (compile check),
         B9 (full gate).
  Outcome: unknown strategies fail at compile, not mid-job.
```

**Eng-review decisions folded into the tasks below:**

1. **Discovery failure isolation (Increment 2, B6).** Each entry-point load is
   wrapped in try/except; a broken third-party plugin is logged and skipped, never
   crashes core masking. A bad installed package must not be able to disable a PII
   tool. (Reflected in B1's `discover_entry_point_strategies` + a B6 test.)
2. **DRY walk (Increment 3, B7).** `check_known_strategy` inlines the walk like the
   4 existing checks (minimal diff, no risk to working PII validation). A TODO
   captures extracting a shared `_iter_masking_columns` across all checks as a
   separate refactor — NOT item 9's job.

**Outside-voice corrections folded in (2026-06-14), all verified against source:**

3. **[P1, FATAL] Registry population (Increment 1, B1).** Only `_pandas_adapter.py`
   imports `_strategies`, and validation runs before any adapter is built — so
   without a trigger, `check_known_strategy` and `decoy.sdk` read an EMPTY registry
   and reject every built-in. Fixed: `ensure_builtins_loaded()` (in B1) lazily
   imports `_strategies`; `resolve_strategy`, `registered_strategy_names`, and
   `discover_entry_point_strategies` all call it. **Add a test:** in a fresh process
   that imports ONLY `decoy_engine.sdk` (never the adapter),
   `registered_strategy_names()` contains the 13 built-ins, and `check_known_strategy`
   accepts `faker` (subprocess test, since import state is process-global).
4. **[P2] Determinism guard (Increment 1 or 2).** The engine already hard-fails a
   deterministic column whose source value maps to two outputs
   (`validation/post/_checks/_determinism_sample.py`). **Add a test:** a deliberately
   non-deterministic custom handler (returns a per-call random value) on a
   deterministic-mode column trips that post-check. Residual cross-run risk stays a
   documented caveat in the SDK page (the engine cannot enforce it).
5. **[P1] Polars coverage (Increment 2)** — per the decision above. Add a polars
   regression test for the `_nested` change, and a test that a `custom:` strategy on
   the polars substrate routes through the pandas oracle (it is absent from
   `_POLARS_NATIVE_STRATEGIES`, so `supports_strategy` is False — confirm the fallback
   actually runs it, do not assume).
6. **[P3] Broaden the A1 census (Increment 0).** The `strategy: "literal"` regex
   misses dynamically-built names, factory/param args, the nested child key
   (`strategy_config.strategy`), and `composite` member strategies. Also scan for the
   nested-child key and composite members; treat the full-suite run (B7 step 6) as the
   real backstop, and say so. The census reduces risk, it does not eliminate it.

## Context: why Part A exists (the blocker)

A `2026-06-13` implementation attempt found that adding the compile-time
`unknown_strategy` check naively **breaks dozens of fixtures.** The engine
*deliberately* treats `strategy` as an open string at compile time and only
validates at execution (`execution/_pandas_adapter.py:232-237`). The census in
this plan's Task A1 confirms the test tree uses strategy strings in three
distinct classes, each needing a different fix:

- **Registered built-ins (13):** `passthrough, redact, truncate, faker, hash,
  bucketize, shuffle, categorical, date_shift, formula, fpe, text_redact,
  nested` (verified in `execution/_strategies/__init__.py:27-44`).
- **Semantic aliases used in fixtures but NOT registered:** `faker_name` (~8
  uses, incl. the core `simple_config`), `faker_email` (~4), `hash_email` (~2).
  These are opaque placeholder strings the fixtures never execute; they exist to
  exercise compile/validation paths.
- **Structural pseudos (valid, handled outside `SCALAR_HANDLERS`):** the
  `composite*` family (`composite`, `composite_person`, `composite_address`, …)
  and `from_pool`. These must be **allowlisted** by the compile check.
- **Deliberately-invalid negative-test strings:** `no_such_strategy`, `x`, `s`,
  `stub`, `reversing_redact`. Tests that pass these currently assert an
  *execution* failure; after this change they must assert a *compile* failure.

Part A makes the valid surface explicit and migrates each class, so the Task B7
compile check can be turned on without breaking the suite.

## File structure

**Part A (migration, no new runtime code):**
- Create: `tests/unit/plan/test_strategy_name_census.py` — the enumerate-and-classify guard.
- Modify: fixture files under `tests/fixtures/` and inline configs that use
  semantic aliases / negative strings (enumerated by A1).

**Part B (the feature):**
- Create: `src/decoy_engine/execution/_plugin_registry.py` — owns `STRATEGY_REGISTRY` + register/resolve/discover.
- Create: `src/decoy_engine/sdk.py` — the published import surface (re-exports).
- Create: `examples/custom_strategy.py` — runnable `UppercaseHandler` demo.
- Create: `docs/custom-strategy-sdk.md` — the SDK reference page.
- Create: `tests/unit/execution/test_plugin_registry.py` — registry unit tests.
- Modify: `src/decoy_engine/execution/_strategies/__init__.py:27-44` — populate built-ins through the registry, keep `SCALAR_HANDLERS` as a backed alias.
- Modify: `src/decoy_engine/execution/_strategies/_nested.py:69-72,117-127` — read the registry instead of the lazy `SCALAR_HANDLERS` import.
- Modify: `src/decoy_engine/execution/_pandas_adapter.py:97-101` — build handlers from the registry.
- Modify: `src/decoy_engine/plan/_checks.py` — add `check_known_strategy`.
- Modify: `src/decoy_engine/plan/_compile.py:138,309` — register the new check.
- Modify: `pyproject.toml` — document the `decoy_engine.strategies` entry-point group; import-linter contract.

---

# Part A — Strategy-name census + fixture migration

## Task A1: Census test (make the unknown concrete)

**Files:**
- Create: `tests/unit/plan/test_strategy_name_census.py`

- [ ] **Step 1: Write the failing census test**

```python
"""Guard: every strategy string in the repo is in a known class.

Classes: registered built-in, structural pseudo, custom-prefixed, or an
explicitly-allowed test placeholder. An unknown string here means a fixture
will silently fail the Task B7 compile check; surface it now, not then.
"""
import re
from pathlib import Path

from decoy_engine.execution._strategies import SCALAR_HANDLERS

REPO = Path(__file__).resolve().parents[2]

# Structural pseudos validated outside SCALAR_HANDLERS (composite family + pool ref).
STRUCTURAL_PSEUDOS = {"from_pool"}
STRUCTURAL_PREFIXES = ("composite",)

# Placeholders that fixtures deliberately use; each must be justified here.
# Negative-test strings (expected to be rejected) live in NEGATIVE.
NEGATIVE = {"no_such_strategy", "x", "s", "stub", "reversing_redact"}
# Semantic aliases pending migration in A3 — this set must shrink to empty.
PENDING_ALIASES = {"faker_name", "faker_email", "hash_email"}

# Word-boundary anchored (proven necessary by the Increment-0 spike, 2026-06-14):
# an unanchored `strategy` matches `sample_strategy` / `child_strategy` / `mask_strategy`
# and produces false-positive migration work (the spike caught `full` from sample_strategy).
STRATEGY_RE = re.compile(r"""(?<![A-Za-z0-9_])strategy["']?\s*[:=]\s*["']([a-z0-9_:.\-]+)["']""")


def _all_strategy_strings() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in REPO.joinpath("tests").rglob("*"):
        if path.suffix not in {".py", ".yaml", ".yml", ".json"}:
            continue
        for m in STRATEGY_RE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
            found.setdefault(m.group(1), []).append(str(path.relative_to(REPO)))
    return found


def _classify(name: str) -> str:
    if name in SCALAR_HANDLERS:
        return "builtin"
    if name.startswith("custom:"):
        return "custom"
    if name in STRUCTURAL_PSEUDOS or name.startswith(STRUCTURAL_PREFIXES):
        return "pseudo"
    if name in NEGATIVE:
        return "negative"
    if name in PENDING_ALIASES:
        return "pending_alias"
    return "UNKNOWN"


def test_no_unclassified_strategy_strings():
    found = _all_strategy_strings()
    unknown = {n: locs for n, locs in found.items() if _classify(n) == "UNKNOWN"}
    assert not unknown, f"Unclassified strategy strings (classify or migrate): {unknown}"


def test_pending_alias_set_is_tracked():
    # A3 migrates these; once migrated, PENDING_ALIASES shrinks. This asserts we
    # never ADD a new unregistered semantic alias.
    found = set(_all_strategy_strings())
    stray = {n for n in found if n in PENDING_ALIASES} - PENDING_ALIASES
    assert not stray
```

- [ ] **Step 2: Run it to see the real surface**

Run: `pytest tests/unit/plan/test_strategy_name_census.py -v`
Expected: PASS if the four sets above are complete; FAIL listing any `UNKNOWN`
string the census found that this plan did not anticipate. **If it fails, do not
edit the sets blindly — classify each new string into builtin/pseudo/negative/
alias and update the plan.** This is the moment the migration size becomes a
fact instead of an estimate.

- [ ] **Step 3: Commit the census guard**

```bash
git add tests/unit/plan/test_strategy_name_census.py
git commit -m "test(plan): census guard for strategy-name surface (item 9 part A)"
```

## Task A2: Define the canonical valid-name surface in one place

**Files:**
- Create: `src/decoy_engine/execution/_strategy_names.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/execution/test_strategy_names.py
from decoy_engine.execution._strategy_names import (
    STRUCTURAL_PSEUDO_PREFIXES, STRUCTURAL_PSEUDO_NAMES, is_structural_pseudo,
)

def test_composite_family_is_pseudo():
    assert is_structural_pseudo("composite_person")
    assert is_structural_pseudo("from_pool")
    assert not is_structural_pseudo("faker")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/execution/test_strategy_names.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the canonical surface**

```python
# src/decoy_engine/execution/_strategy_names.py
"""Single source of truth for strategy names that are valid but NOT handler-backed.

The compile-time check (plan/_checks.check_known_strategy) treats a name as valid
if it is a registered handler, a `custom:` name, or a structural pseudo named
here. Keep this list narrow; a real handler belongs in the registry, not here.
"""
from __future__ import annotations

STRUCTURAL_PSEUDO_PREFIXES: tuple[str, ...] = ("composite",)
STRUCTURAL_PSEUDO_NAMES: frozenset[str] = frozenset({"from_pool"})


def is_structural_pseudo(name: str) -> bool:
    return name in STRUCTURAL_PSEUDO_NAMES or name.startswith(STRUCTURAL_PSEUDO_PREFIXES)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/unit/execution/test_strategy_names.py -v`
Expected: PASS.

- [ ] **Step 5: Point the census at the canonical surface** — replace the
  inline `STRUCTURAL_PSEUDOS`/`STRUCTURAL_PREFIXES` in A1's test with imports
  from `_strategy_names` so there is one definition.

Run: `pytest tests/unit/plan/test_strategy_name_census.py -v` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/_strategy_names.py tests/unit/execution/test_strategy_names.py tests/unit/plan/test_strategy_name_census.py
git commit -m "feat(execution): canonical structural-pseudo strategy-name surface (item 9 part A)"
```

## Task A3: Migrate the semantic-alias fixtures

The aliases `faker_name`, `faker_email`, `hash_email` are opaque to execution.
For each fixture using one, decide: is this fixture testing **compile/validation
behavior that does not run** (then change the string to a registered strategy so
the future compile check passes), or is it a **placeholder that should stay
unvalidated** (then it must become `custom:`-prefixed or move to the NEGATIVE set
with a justification)?

Default rule: `faker_name` → `faker` with `provider: name`; `faker_email` →
`faker` with `provider: email`; `hash_email` → `hash`. Make the change and the
test assertion in the **same commit**.

**Files:**
- Modify: each fixture/config listed by the A1 census under `pending_alias`
  (run A1 to get the exact, current list of file paths).

- [ ] **Step 1: Get the exact file list**

Run: `pytest tests/unit/plan/test_strategy_name_census.py -v` and read the
`pending_alias` locations it prints (add a temporary `print` of
`_all_strategy_strings()` filtered to `PENDING_ALIASES` if needed).

- [ ] **Step 2: For each file, migrate the string and adjust the assertion**

The swap is `strategy` string → registered strategy + `provider`, keeping the
fixture's existing structure (the canonical config has `tables` as a LIST of
`{name, columns: [{name, strategy, provider, ...}]}`; match whatever shape the
fixture already uses). Apply per file; do not batch-replace blindly — confirm
each fixture still tests what it intends:

```
# before:  {"name": "full_name", "strategy": "faker_name"}
# after:   {"name": "full_name", "strategy": "faker", "provider": "name"}
```

- [ ] **Step 3: After each file, run that file's owning test**

Run: `pytest <the test that loads this fixture> -v` — Expected: PASS.

- [ ] **Step 4: Shrink the PENDING_ALIASES set**

As each alias disappears from the tree, remove it from `PENDING_ALIASES` in the
census test. Goal state: `PENDING_ALIASES = set()`.

Run: `pytest tests/unit/plan/test_strategy_name_census.py -v` — Expected: PASS
with an empty pending set.

- [ ] **Step 5: Commit (one commit per few files, or per logical group)**

```bash
git add -A
git commit -m "test(fixtures): migrate semantic strategy aliases to registered strategies (item 9 part A)"
```

## Task A4: Convert negative-test fixtures to assert compile-time rejection

The strings in `NEGATIVE` (`no_such_strategy`, `x`, `s`, `stub`,
`reversing_redact`) are used in tests that currently expect an **execution**
error (`unsupported_strategy` at `_pandas_adapter.py:232`). After Task B7 these
fail at **compile**. Find those tests now and prepare them.

**Files:**
- Modify: the tests that assert execution failure on a NEGATIVE string (grep
  `unsupported_strategy`, `no_such_strategy`, etc.).

- [ ] **Step 1: Locate them**

Run: `grep -rn 'no_such_strategy\|unsupported_strategy\|reversing_redact' tests/`

- [ ] **Step 2: Mark each with the post-B7 expectation in a comment**

Do NOT change the assertion yet (the compile check does not exist until B7).
Add a `# TODO(item9-B7): becomes a compile-time unknown_strategy error` comment
at each so B7 has an exact worklist. Leave behavior unchanged.

- [ ] **Step 3: Commit the markers**

```bash
git add -A
git commit -m "test: mark negative-strategy tests for the item-9 compile check (item 9 part A)"
```

**Part A done-definition:** census green with `PENDING_ALIASES` empty; full suite
green (`pytest -q`); no behavior change yet. The compile check is now safe to add.

---

# Part B — The registry + SDK

## Task B1: The plugin registry module

**Files:**
- Create: `src/decoy_engine/execution/_plugin_registry.py`
- Create: `tests/unit/execution/test_plugin_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/execution/test_plugin_registry.py
import pytest
from decoy_engine.execution import _plugin_registry as reg


class _Good:
    name = "custom:good"
    def run(self, df, column, plan, ctx):  # 4 positional params
        return df, []


def setup_function():
    reg.unregister_strategy("custom:good")


def test_register_and_resolve():
    reg.register_strategy("custom:good", _Good())
    assert reg.resolve_strategy("custom:good").name == "custom:good"
    assert "custom:good" in reg.registered_strategy_names()


def test_reject_builtin_collision_without_override():
    with pytest.raises(reg.StrategyRegistryError) as e:
        reg.register_strategy("redact", _Good())
    assert e.value.code == "strategy_builtin_collision"


def test_reject_wrong_arity_run():
    class Bad:
        name = "custom:bad"
        def run(self, df):  # too few params
            return df, []
    with pytest.raises(reg.StrategyRegistryError) as e:
        reg.register_strategy("custom:bad", Bad())
    assert e.value.code == "strategy_bad_signature"


def test_unregister_clears():
    reg.register_strategy("custom:good", _Good())
    reg.unregister_strategy("custom:good")
    assert "custom:good" not in reg.registered_strategy_names()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/execution/test_plugin_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: _plugin_registry`.

- [ ] **Step 3: Implement the registry**

```python
# src/decoy_engine/execution/_plugin_registry.py
"""Single registry for strategy handlers (built-in + custom).

Imports ONLY the `_adapter` Protocol types and stdlib — never a handler module —
so `_nested` and the adapter can read it without the
`__init__.py -> handler-modules` import cycle. Built-ins push themselves in from
`_strategies/__init__.py`; `_nested`/adapter pull.

Discovery follows the PyPA "Creating and discovering plugins" entry-points
pattern (importlib.metadata, group "decoy_engine.strategies"; same mechanism as
pytest/flake8/sphinx). API shape and trust model mirror the in-repo
`providers_v2.register_faker_provider_v2` precedent.
"""
from __future__ import annotations

import importlib.metadata
import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import StrategyHandler

ENTRY_POINT_GROUP = "decoy_engine.strategies"

# Names owned by built-ins; cannot be overridden without override=True.
_BUILTIN_NAMES: set[str] = set()

STRATEGY_REGISTRY: dict[str, "StrategyHandler"] = {}
_entry_points_loaded = False
_builtins_loaded = False


def ensure_builtins_loaded() -> None:
    """Populate the registry with built-in handlers (idempotent).

    Built-ins register as a side effect of importing `_strategies/__init__.py`.
    EVERY public reader calls this, because a reader that does NOT go through the
    pandas adapter (compile-time `check_known_strategy`, `decoy.sdk`, discovery)
    would otherwise see an EMPTY registry and reject every built-in. Verified
    2026-06-14: only `_pandas_adapter.py` imports `_strategies`, and validation
    runs before any adapter is built, so this trigger is load-bearing. The lazy
    import keeps the module-load contract (only `_adapter` types + stdlib at
    module top). `_builtins_loaded` is set BEFORE the import so the
    `register_strategy(_builtin=True)` calls it triggers don't re-enter.
    """
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    import decoy_engine.execution._strategies  # noqa: F401  side-effect: populates registry


class StrategyRegistryError(Exception):
    """Invalid strategy registration. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def _validate(name: str, handler: "StrategyHandler") -> None:
    if not isinstance(name, str) or not name:
        raise StrategyRegistryError(code="strategy_bad_name", message="name must be a non-empty str")
    run = getattr(handler, "run", None)
    if run is None or not callable(run):
        raise StrategyRegistryError(code="strategy_bad_signature", message=f"{name}: handler has no callable run()")
    sig = inspect.signature(run)
    has_var_positional = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    # Light runtime guard, NOT a strict contract: accept exactly (df, column,
    # plan, ctx) OR a *args-style wrapper (decorators/adapters are legitimate).
    # mypy + the StrategyHandler Protocol are the real structural gate; this only
    # catches gross mistakes early (eng-review: don't over-reject valid handlers).
    if not has_var_positional and len(positional) != 4:
        raise StrategyRegistryError(
            code="strategy_bad_signature",
            message=f"{name}: run() must accept (df, column, plan, ctx) or *args; "
                    f"got {len(positional)} positional params")


def register_strategy(name: str, handler: "StrategyHandler", *, override: bool = False,
                      _builtin: bool = False) -> None:
    _validate(name, handler)
    if not _builtin and name in _BUILTIN_NAMES and not override:
        raise StrategyRegistryError(code="strategy_builtin_collision",
                                    message=f"{name} is a built-in; pass override=True or use a custom: name")
    if _builtin:
        _BUILTIN_NAMES.add(name)
    STRATEGY_REGISTRY[name] = handler


def unregister_strategy(name: str) -> None:
    STRATEGY_REGISTRY.pop(name, None)
    _BUILTIN_NAMES.discard(name)


def resolve_strategy(name: str) -> "StrategyHandler":
    ensure_builtins_loaded()
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError:
        raise StrategyRegistryError(code="strategy_unregistered", message=f"{name} is not registered") from None


def registered_strategy_names() -> frozenset[str]:
    ensure_builtins_loaded()
    return frozenset(STRATEGY_REGISTRY)


def discover_entry_point_strategies() -> None:
    """Idempotently load third-party handlers from the entry-point group.

    Per-plugin isolation (eng-review decision A, 2026-06-14): a broken third-party
    plugin is logged and skipped, NEVER allowed to crash core masking. A bad
    installed package must not be able to disable a PII tool.
    """
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    ensure_builtins_loaded()
    log = logging.getLogger(__name__)
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            register_strategy(ep.name, ep.load()())
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not disable masking
            log.warning("skipping custom strategy entry point %r: %s", ep.name, exc)
    _entry_points_loaded = True
```

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/unit/execution/test_plugin_registry.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/_plugin_registry.py tests/unit/execution/test_plugin_registry.py
git commit -m "feat(execution): strategy plugin registry (item 9)"
```

## Task B2: Publish the SDK contract

**Files:**
- Create: `src/decoy_engine/sdk.py`
- Test: `tests/unit/test_sdk_surface.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_sdk_surface.py
def test_sdk_exports():
    from decoy_engine.sdk import (  # noqa: F401
        StrategyHandler, StrategyContext, register_strategy,
        unregister_strategy, registered_strategy_names,
    )
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_sdk_surface.py -v` — Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement `sdk.py`** (re-export from stable paths)

```python
# src/decoy_engine/sdk.py
"""Public SDK surface for custom strategy handlers.

Stable import path: `from decoy_engine.sdk import StrategyHandler`. See
docs/custom-strategy-sdk.md and docs/compatibility-contract.md (this is a frozen
public surface once shipped).
"""
from __future__ import annotations

from decoy_engine.execution._adapter import StrategyContext, StrategyHandler
from decoy_engine.execution._plugin_registry import (
    StrategyRegistryError,
    register_strategy,
    registered_strategy_names,
    unregister_strategy,
)
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

__all__ = [
    "StrategyHandler", "StrategyContext", "ColumnSeed", "QualityWarning",
    "StrategyRegistryError", "register_strategy", "unregister_strategy",
    "registered_strategy_names",
]
```

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/unit/test_sdk_surface.py -v` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/sdk.py tests/unit/test_sdk_surface.py
git commit -m "feat: publish decoy_engine.sdk strategy-handler surface (item 9)"
```

## Task B3: Populate built-ins through the registry

**Files:**
- Modify: `src/decoy_engine/execution/_strategies/__init__.py:27-44`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/execution/test_builtins_registered.py
def test_all_builtins_in_registry():
    from decoy_engine.execution._strategies import SCALAR_HANDLERS
    from decoy_engine.execution._plugin_registry import registered_strategy_names
    assert set(SCALAR_HANDLERS) <= set(registered_strategy_names())
    for name in ("redact", "faker", "hash", "nested", "fpe", "text_redact"):
        assert name in registered_strategy_names()
```

- [ ] **Step 2: Run to verify it fails** — Run: `pytest tests/unit/execution/test_builtins_registered.py -v` — Expected: FAIL (built-ins not yet in registry).

- [ ] **Step 3: Register built-ins on import; keep `SCALAR_HANDLERS` as a backed alias**

In `__init__.py`, after constructing the 13 instances, register each as a
built-in and expose `SCALAR_HANDLERS` from the registry so existing importers do
not churn:

```python
# append to src/decoy_engine/execution/_strategies/__init__.py
from decoy_engine.execution._plugin_registry import (
    STRATEGY_REGISTRY, register_strategy,
)

for _h in (
    PassthroughHandler(), RedactHandler(), TruncateHandler(), FakerStrategyHandler(),
    HashStrategyHandler(), BucketizeStrategyHandler(), ShuffleStrategyHandler(),
    CategoricalStrategyHandler(), DateShiftStrategyHandler(), FormulaStrategyHandler(),
    FpeStrategyHandler(), TextRedactHandler(), NestedStrategyHandler(),
):
    register_strategy(_h.name, _h, _builtin=True)

# SCALAR_HANDLERS is now a view backed by the registry (public alias preserved).
SCALAR_HANDLERS: dict[str, StrategyHandler] = STRATEGY_REGISTRY
```

Remove the old standalone `SCALAR_HANDLERS` comprehension (lines 27-44) so there
is one source. Keep `__all__`.

- [ ] **Step 4: Run to verify it passes** — Run: `pytest tests/unit/execution/test_builtins_registered.py -v` — Expected: PASS.

- [ ] **Step 5: Run the execution suite (no regression)** — Run: `pytest tests/unit/execution -q` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/decoy_engine/execution/_strategies/__init__.py tests/unit/execution/test_builtins_registered.py
git commit -m "refactor(execution): back SCALAR_HANDLERS with the plugin registry (item 9)"
```

## Task B4: Route `nested` through the registry (break the cycle)

**Files:**
- Modify: `src/decoy_engine/execution/_strategies/_nested.py:69-72,117-127`

- [ ] **Step 1: Confirm current nested behavior is covered** — Run: `pytest tests/unit/execution/test_nested_strategy.py -v` — Expected: PASS (baseline).

- [ ] **Step 2: Replace the lazy import with a top-level registry read**

In `_nested.py`, delete the inside-`run()` lazy import
(`from decoy_engine.execution._strategies import SCALAR_HANDLERS`, line 72) and
replace the `SCALAR_HANDLERS.get(child_strategy_name)` lookup (line 117) with a
registry membership check that **preserves the existing `StrategyError`** (the
registry's own `StrategyRegistryError` must NOT leak out of nested):

```python
# top of module:
from decoy_engine.execution._plugin_registry import (
    registered_strategy_names, resolve_strategy,
)

# was: child_handler = SCALAR_HANDLERS.get(child_strategy_name)
#      if child_handler is None: raise StrategyError(code="nested_child_strategy_unknown", ...)
if child_strategy_name not in registered_strategy_names():
    raise StrategyError(
        code="nested_child_strategy_unknown",
        strategy="nested",
        message=(
            f"nested child strategy {child_strategy_name!r} is not a registered "
            f"strategy (column={column!r}). A typo here silently dropped PII pre-fix."
        ),
    )
child_handler = resolve_strategy(child_strategy_name)
```

Keep the recursive guard (lines 110-115) and the `nested_target_unset` /
`nested_strategy_unset` guards (lines 89-108) exactly as-is. Update only the
error message wording away from "SCALAR_HANDLERS key" to "registered strategy."
A `custom:` strategy can now be a nested child.

- [ ] **Step 3: Run nested tests (incl. unknown-child + recursive rejection)** — Run: `pytest tests/unit/execution/test_nested_strategy.py -v` — Expected: PASS, all prior behaviors preserved.

- [ ] **Step 4: Commit**

```bash
git add src/decoy_engine/execution/_strategies/_nested.py
git commit -m "refactor(execution): nested resolves children via the registry, breaking the import cycle (item 9)"
```

## Task B5: Route the pandas adapter through the registry

**Files:**
- Modify: `src/decoy_engine/execution/_pandas_adapter.py:97-101`

- [ ] **Step 1: Build adapter handlers from the registry, after discovery**

At `_pandas_adapter.py:97`, replace `self._handlers = dict(SCALAR_HANDLERS)` with
a registry-sourced dict, ensuring entry-point discovery has run first; preserve
the live `fpe_chunk_count` override at line 98:

```python
from decoy_engine.execution._plugin_registry import (
    STRATEGY_REGISTRY, discover_entry_point_strategies,
)

discover_entry_point_strategies()
self._handlers = dict(STRATEGY_REGISTRY)
# (keep the existing fpe_chunk_count override that follows)
```

- [ ] **Step 2: Add an end-to-end test: a custom handler masks a frame**

```python
# tests/unit/execution/test_custom_handler_e2e.py
import pandas as pd
from decoy_engine.sdk import register_strategy, unregister_strategy

class Upper:
    name = "custom:upper"
    def run(self, df, column, plan, ctx):
        df = df.copy(); df[column] = df[column].str.upper(); return df, []

def test_custom_handler_runs_via_adapter():
    register_strategy("custom:upper", Upper())
    try:
        # build a minimal mask config using strategy: "custom:upper" and run the
        # pandas adapter spine; assert the column is uppercased.
        ...  # mirror tests/unit/execution/test_redact_strategy.py's harness
    finally:
        unregister_strategy("custom:upper")
```

Fill the harness by mirroring an existing single-strategy execution test
(`test_redact_strategy.py` or `test_passthrough` style); do not invent a new
runner.

- [ ] **Step 3: Run** — `pytest tests/unit/execution/test_custom_handler_e2e.py -v` — Expected: PASS.

- [ ] **Step 4: Run full execution suite** — `pytest tests/unit/execution -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/decoy_engine/execution/_pandas_adapter.py tests/unit/execution/test_custom_handler_e2e.py
git commit -m "feat(execution): pandas adapter resolves handlers via the registry incl. entry points (item 9)"
```

## Task B6: Entry-point discovery test

**Files:**
- Test: `tests/unit/execution/test_entry_point_discovery.py`

- [ ] **Step 1: Write the test (monkeypatch importlib.metadata.entry_points)**

```python
# tests/unit/execution/test_entry_point_discovery.py
import importlib.metadata as md
from decoy_engine.execution import _plugin_registry as reg

class _EP:
    name = "custom:from_ep"
    def load(self):
        class H:
            name = "custom:from_ep"
            def run(self, df, column, plan, ctx): return df, []
        return H

def test_discovery_registers_entry_point(monkeypatch):
    reg._entry_points_loaded = False
    monkeypatch.setattr(md, "entry_points", lambda *, group: [_EP()] if group == reg.ENTRY_POINT_GROUP else [])
    reg.discover_entry_point_strategies()
    assert "custom:from_ep" in reg.registered_strategy_names()
    reg.unregister_strategy("custom:from_ep")


class _BadEP:
    name = "custom:broken"
    def load(self):
        raise ImportError("this plugin is broken on import")

def test_discovery_isolates_a_broken_plugin(monkeypatch, caplog):
    # eng-review finding 1 (decision A): one bad plugin must NOT crash discovery;
    # good plugins still load, the bad one is skipped with a warning.
    reg._entry_points_loaded = False
    monkeypatch.setattr(md, "entry_points",
                        lambda *, group: [_BadEP(), _EP()] if group == reg.ENTRY_POINT_GROUP else [])
    reg.discover_entry_point_strategies()  # must NOT raise
    assert "custom:from_ep" in reg.registered_strategy_names()   # good plugin survived
    assert "custom:broken" not in reg.registered_strategy_names()  # bad one skipped
    assert any("custom:broken" in r.message for r in caplog.records)
    reg.unregister_strategy("custom:from_ep")
```

- [ ] **Step 2: Run** — Expected: PASS (discovery is idempotent + picks up the stub).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/execution/test_entry_point_discovery.py
git commit -m "test(execution): entry-point strategy discovery (item 9)"
```

## Task B7: Compile-time `unknown_strategy` check (now safe)

Part A made this safe: every fixture string is now builtin / pseudo / custom, and
negative tests are marked.

**Files:**
- Modify: `src/decoy_engine/plan/_checks.py` (add `check_known_strategy`)
- Modify: `src/decoy_engine/plan/_compile.py:138,309` (register the check)
- Modify: the negative tests marked in Task A4

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/plan/test_check_known_strategy.py
import pytest
from decoy_engine.plan._checks import check_known_strategy
from decoy_engine.plan._errors import PlanCompileError  # verified module path

def test_unregistered_custom_rejected():
    cfg = {"tables": [{"name": "t", "columns": [
        {"name": "c", "strategy": "custom:not_registered"},
    ]}]}
    with pytest.raises(PlanCompileError) as e:
        check_known_strategy(cfg)
    assert e.value.code == "unknown_strategy"

def test_builtin_and_pseudo_accepted():
    cfg = {"tables": [{"name": "t", "columns": [
        {"name": "a", "strategy": "faker", "provider": "name"},
        {"name": "b", "strategy": "composite_person"},
    ]}]}
    check_known_strategy(cfg)  # no raise

def test_registered_custom_accepted():
    # gap from test review: a REGISTERED custom strategy must pass compile.
    from decoy_engine.execution._plugin_registry import register_strategy, unregister_strategy
    class _H:
        name = "custom:ok"
        def run(self, df, column, plan, ctx): return df, []
    register_strategy("custom:ok", _H())
    try:
        cfg = {"tables": [{"name": "t", "columns": [{"name": "c", "strategy": "custom:ok"}]}]}
        check_known_strategy(cfg)  # no raise
    finally:
        unregister_strategy("custom:ok")
```

Config shape (`tables` is a LIST), the `PlanCompileError` import path
(`decoy_engine.plan._errors`), and the walk are all verified against
`check_unknown_provider` (`_checks.py:50-92`).

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (`ImportError` on `check_known_strategy`).

- [ ] **Step 3: Implement the check (mirror `check_unknown_provider`)**

```python
# src/decoy_engine/plan/_checks.py  (new function; inline the SAME walk as
# check_unknown_provider:70-92 — config["tables"] is a LIST of dicts.)
from decoy_engine.execution._plugin_registry import (
    discover_entry_point_strategies, registered_strategy_names,
)
from decoy_engine.execution._strategy_names import is_structural_pseudo


def check_known_strategy(config: dict[str, Any]) -> None:
    """Reject masking columns whose strategy is neither a registered handler nor
    a structural pseudo. Closes the silently-compiles-fails-at-runtime gap
    (was: _pandas_adapter.py:232/:400 unsupported_strategy at execution time)."""
    discover_entry_point_strategies()
    known = registered_strategy_names()
    tables = config.get("tables", []) if isinstance(config.get("tables"), list) else []
    for table_entry in tables:
        if not isinstance(table_entry, dict):
            continue
        table_name = table_entry.get("name", "?")
        for col_entry in table_entry.get("columns", []) or []:
            if not isinstance(col_entry, dict):
                continue
            strategy = col_entry.get("strategy")
            if not isinstance(strategy, str):
                continue
            if strategy in known or is_structural_pseudo(strategy):
                continue
            col_name = col_entry.get("name", "?")
            raise PlanCompileError(
                code="unknown_strategy",
                path=f"tables.{table_name}.columns.{col_name}.strategy",
                message=(
                    f"Unknown strategy {strategy!r} at {table_name}.{col_name}. "
                    "Use a built-in, a registered custom: handler, or a composite."
                ),
            )
```

The walk and the `PlanCompileError(code=, path=, message=)` construction mirror
`check_unknown_provider` exactly (`_checks.py:70-92`). Do not hand-roll a new
traversal or omit `path=`.

- [ ] **Step 4: Register the check in `_compile.py`**

Add `check_known_strategy(config)` next to `check_unknown_provider` at
`_compile.py:138`, and mirror at `:309` if that is the second (post-profile)
check site, matching how `check_unknown_provider` is wired in both places.

- [ ] **Step 5: Run the new test + flip the Task A4 negative tests**

Run: `pytest tests/unit/plan/test_check_known_strategy.py -v` — Expected: PASS.
Then update each test marked `# TODO(item9-B7)` in A4 to assert
`PlanCompileError(code="unknown_strategy")` at compile instead of the execution
error, and remove the markers.

- [ ] **Step 6: Run the FULL suite** — Run: `pytest -q` — Expected: PASS. The
  census (A1) and every prior task guarantee no fixture trips the new check.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(plan): compile-time unknown_strategy check (item 9 B7)"
```

## Task B8: Example, docs, import-linter contract

**Files:**
- Create: `examples/custom_strategy.py`
- Create: `docs/custom-strategy-sdk.md`
- Modify: `pyproject.toml` (entry-point group docs + import-linter contract)
- Test: `tests/integration/test_example_custom_strategy.py`

- [ ] **Step 1: Write the example test first**

```python
# tests/integration/test_example_custom_strategy.py
# NOTE: examples/ is NOT an importable package (and is currently untracked in
# git — see the verification section). Run the file by path, do not import it.
import runpy
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "custom_strategy.py"

def test_example_runs():
    ns = runpy.run_path(str(EXAMPLE), run_name="__main__")
    assert ns["main"]() is not None  # masks a small frame directly + as a nested child
```

- [ ] **Step 2: Run to verify it fails** — Expected: FAIL (`FileNotFoundError`,
  the example does not exist yet).

- [ ] **Step 3: Write `examples/custom_strategy.py`** — a runnable
  `UppercaseHandler(StrategyHandler)`, `register_strategy("custom:upper", ...)`,
  masking a small DataFrame both directly and as a `nested` child, with a
  `main()` that returns the masked frame. Note in a comment that determinism for
  custom handlers means keying on `derive(ctx.job_seed, plan.namespace, ...)`
  (cite `determinism/_derive.py`); the engine cannot enforce it.

- [ ] **Step 4: Run to verify it passes** — Expected: PASS.

- [ ] **Step 5: Write `docs/custom-strategy-sdk.md`** — the `StrategyHandler`
  Protocol, `register_strategy` vs entry points, the `custom:` naming rule, the
  determinism caveat, and the **trust boundary** (loading user code = arbitrary
  in-process execution; identical posture to `custom_providers/README.md`;
  platform-side gating is a separate decision). Add the same line to
  `SECURITY.md`.

- [ ] **Step 6: Add the entry-point group + import-linter contract to `pyproject.toml`** —
  document the `[project.entry-points."decoy_engine.strategies"]` convention and
  add an import-linter contract forbidding `_plugin_registry` from importing any
  `execution._strategies.*` handler module (locks in the dependency inversion).

- [ ] **Step 7: Build docs** — Run: `sphinx-build -W docs docs/_build` (or the
  repo's `[docs]` invocation) — Expected: clean build, no warnings.

- [ ] **Step 8: Commit**

```bash
git add examples/custom_strategy.py docs/custom-strategy-sdk.md SECURITY.md pyproject.toml tests/integration/test_example_custom_strategy.py
git commit -m "docs+example: custom strategy SDK reference, runnable example, trust boundary (item 9)"
```

## Task B9: Full engine gate

- [ ] **Step 1: Run the 5-gate locally** (mirror CI; mypy is the sensitive part —
  the adapter must type-check against the registry-typed dict)

```bash
ruff format --check . && ruff check . && mypy src && pytest -q && lint-imports
```

Expected: all green. If mypy complains about `STRATEGY_REGISTRY` variance, type
it as `dict[str, StrategyHandler]` and ensure `_adapter.StrategyHandler` is the
Protocol the handlers structurally satisfy.

- [ ] **Step 2: Final commit / open PR** on `gap-closure/engine` (or a fresh
  `feat/custom-strategy-sdk` branch off green main).

---

## Self-review (run before handing off)

**Spec coverage** (against `decoy-platform/docs/backlog/gap-closure/09-custom-strategy-sdk.md` §4):
- Publish the contract → B2. New registry module → B1. Populate built-ins through
  one path → B3. Route nested → B4. Route adapter → B5. Compile-time rejection →
  B7. Classification (auto "needs classification") → unchanged, noted in B7.
  Mount + docs → B8. Example → B8. The blocker's fixture migration → **Part A
  (the addition this plan makes over the spec).**

**Placeholder scan:** the only intentionally-deferred specifics are the two test
harnesses (B5 custom-handler e2e, B7 config shape) that must mirror an existing
named test rather than invent a runner — each names the exact file to copy.

**Type consistency:** `StrategyHandler`/`StrategyContext` come from
`execution/_adapter.py` throughout; `register_strategy`/`resolve_strategy`/
`registered_strategy_names`/`unregister_strategy`/`StrategyRegistryError` are
defined in B1 and re-exported unchanged in B2; `STRATEGY_REGISTRY` is the single
dict everywhere.

## Out of scope (per spec §1 "OUT")

Platform-side arbitrary-code-execution gating (separate security decision);
polars-native custom handlers (route through the pandas oracle, auto-fallback);
sandboxing (documented trust boundary instead). Item 5 (`binary`) registers
through this path later and is **not** part of this plan.

## NOT in scope (eng-review)

- **Extracting a shared `_iter_masking_columns` across all validation checks** —
  the cross-check DRY cleanup (finding 2). Deferred to its own refactor; captured as
  a TODO. Rationale: touching correctness-critical PII validation is not item 9's job.
- **Polars-NATIVE custom handlers** — custom strategies run via the pandas oracle on
  the polars substrate (Increment 2 tests the fallback). A native polars path is a
  separate fast-follow.
- **Sandboxing custom code** — documented trust boundary instead (same posture as
  `custom_providers`). Platform-side admin gating is a separate security decision.
- **Item 5 (`binary` masking)** — registers through this registry later, separate plan.

## What already exists (reused, not rebuilt)

- `register_faker_provider_v2` (`providers_v2/_faker_adapter.py:226`) — the
  register/unregister/names pattern this mirrors. **Reused, not reinvented.**
- `StrategyHandler` Protocol + `StrategyContext` (`_adapter.py:92-103,77-89`) — the
  contract is published as-is via `decoy.sdk`, not redefined.
- `SCALAR_HANDLERS` (`_strategies/__init__.py`) — becomes a registry-backed alias, a
  refactor of an existing dict, not a new system.
- `_determinism_sample` post-check — already guards within-run non-determinism;
  reused (finding 4) instead of building a new determinism guard.
- `importlib.metadata` entry points — the PyPA-standard plugin mechanism. [Layer 1]

## Failure modes (per new codepath)

| Codepath | Realistic failure | Test? | Error handling? | Silent? |
|---|---|---|---|---|
| `discover_entry_point_strategies` | a broken installed plugin raises on load | YES (B6 isolation test) | YES (try/except + warn, skip) | No — warned |
| `check_known_strategy` at compile | registry empty (population bug) → rejects built-ins | YES (B1 subprocess test) | YES (`ensure_builtins_loaded`) | Would have been silent-fatal; now fixed |
| custom handler on polars substrate | not native → no fallback | YES (Increment 2 test) | to verify (oracle fallback) | must not be silent |
| non-deterministic custom handler | breaks reproducibility | YES (finding 4 test) | existing post-check hard-fails (within-run) | cross-run = documented caveat |
| `_nested` registry resolution | custom child not registered | YES (test_nested_strategy) | preserves `nested_child_strategy_unknown` StrategyError | No |

No critical gaps (no failure mode is untested AND unhandled AND silent after the folds).

## Worktree parallelization

| Increment | Modules touched | Depends on |
|---|---|---|
| 0 Census spike | tests/ | — |
| 1 SDK core | execution/ (registry, adapter, _strategies), sdk.py | Inc 0 result (go/no-go) |
| 2 Nested+discovery+polars | execution/ (_nested, adapters, polars/) | Inc 1 (registry) |
| 3 Compile safety | plan/, tests/fixtures/ | Inc 1 (registry) + Inc 0 (census) |

Lane A: Increment 0 (independent, run now). Then **sequential**: Inc 1 → Inc 2, and
Inc 1 → Inc 3 (Inc 2 and Inc 3 both depend only on Inc 1's registry, so after Inc 1
they could run in parallel worktrees, BUT both touch `execution/` and `plan` imports
the registry — coordinate). Recommendation: 0 → 1 → 2 → 3, sequential, per the
go-slow directive.

## Implementation Tasks
Synthesized from this review. Run with Claude Code; checkbox as you ship.

- [ ] **T1 (P1, human: ~30min / CC: ~5min)** — execution — run the A1 census spike FIRST; report the real migration size before freezing `decoy.sdk`.
  - Surfaced by: outside-voice sequencing finding.
  - Files: `tests/unit/plan/test_strategy_name_census.py`
  - Verify: `pytest tests/unit/plan/test_strategy_name_census.py -v`
- [ ] **T2 (P1, human: ~1h / CC: ~10min)** — execution — registry + `ensure_builtins_loaded` population trigger + subprocess test.
  - Surfaced by: outside-voice P1 (empty-registry-at-compile).
  - Files: `src/decoy_engine/execution/_plugin_registry.py`, `tests/unit/execution/test_plugin_registry.py`
  - Verify: fresh-process import of `decoy_engine.sdk` only → 13 built-ins present.
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — execution/polars — `_nested` polars regression test + custom-on-polars oracle-fallback test.
  - Surfaced by: outside-voice P1 (polars omission).
  - Files: `src/decoy_engine/execution/polars/`, `tests/.../test_*polars*`
  - Verify: `pytest -k polars -v` green; a custom strategy masks on a polars job.
- [ ] **T4 (P3, human: ~1h / CC: ~10min)** — plan — extract shared `_iter_masking_columns` across all validation checks (DEFERRED TODO, finding 2).
  - Surfaced by: code-quality DRY finding.
  - Files: `src/decoy_engine/plan/_checks.py`

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Outside Voice | Claude subagent | Independent 2nd opinion | 1 | issues_found | codex not installed; subagent caught 2 P1 (registry population, polars), 3 P2, 1 P3 — all folded |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | issues_found → folded | 6 source-verification fixes + 2 review findings + 6 outside-voice corrections; scope sliced to 3 increments + a census spike |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **CROSS-MODEL:** the outside voice caught the fatal registry-population bug and the polars omission that the primary eng-review missed; both verified against source and folded. No remaining cross-model tension.
- **VERDICT:** ENG CLEARED — plan source-verified, corrected, and sliced to a census spike + 3 increments. Ready to implement, starting with Increment 0 (the census spike) before freezing any public surface.

NO UNRESOLVED DECISIONS
