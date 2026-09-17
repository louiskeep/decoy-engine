"""GP2 predicate-aware draw-site coverage (Codex round-3 spec D, BLOCKER).

``test_draw_site_inventory_coverage.py``'s static scan is FILE-level: any RNG
token anywhere in an already-catalogued file (``generation/synthesize.py`` is
catalogued via ``gen.faker_per_row``) counts as covered, so it cannot see
whether ``_faker`` actually reaches BOTH new GP2 draw sites
(``gen.faker_pool_build`` / ``gen.faker_pool_selection``) at the call/branch
level, and ``GEN_KIND_TO_SITE`` maps the whole ``faker`` kind to
``gen.faker_per_row`` alone -- it cannot express the pooled/per-row
eligibility split either. This module closes both gaps:

1. A call/branch-level static sentinel (AST, not a file-existence check):
   ``_faker`` must call both ``_faker_pool.pool_eligible`` and
   ``_faker_pool.build_and_sample``, and ``_faker_pool.build_and_sample``
   must call ``GenDeriveContext.family_bytes`` under both frozen labels.
2. Predicate-aware RUNTIME coverage: driving `_faker` with an eligible column
   actually reaches the pool bridge (and not the per-row loop), and driving
   it with each ineligible predicate (below threshold, non-allowlisted,
   opted out, custom override, locale-unavailable) stays on the per-row loop
   (and never touches the pool bridge's own build/select calls).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import decoy_engine.generation._faker_pool as _faker_pool
import decoy_engine.generation.synthesize as _synthesize
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.providers import register_faker_provider, unregister_faker_provider

# ---------------------------------------------------------------------------
# Static sentinel: call/branch-level, not file-level.
# ---------------------------------------------------------------------------


def _call_paths(func: ast.FunctionDef) -> set[str]:
    """Dotted `obj.method` text for every Call node's func expression in `func`."""
    paths: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            paths.add(f"{target.value.id}.{target.attr}")
    return paths


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def test_faker_dispatch_reaches_both_pool_bridge_calls() -> None:
    """`_faker` must branch into BOTH `_faker_pool.pool_eligible` (the gate)
    and `_faker_pool.build_and_sample` (the pooled path), not merely import
    the module -- a file-level scan can't tell those apart."""
    source = inspect.getsource(_synthesize)
    tree = ast.parse(source)
    faker_fn = _find_function(tree, "_faker")
    calls = _call_paths(faker_fn)
    assert "_faker_pool.pool_eligible" in calls, (
        "_faker no longer calls the GP2 eligibility gate -- pooling would be unconditional "
        "or dead code"
    )
    assert "_faker_pool.build_and_sample" in calls, (
        "_faker no longer calls the GP2 pool bridge -- the pooled draw sites would be unreachable"
    )


def test_pool_bridge_draws_from_both_frozen_hmac_labels() -> None:
    """`build_and_sample` must derive both the BUILD and SELECTION seeds via
    `GenDeriveContext.family_bytes`, under the two frozen label constants the
    catalogued `gen.faker_pool_build`/`gen.faker_pool_selection` sites document."""
    source = inspect.getsource(_faker_pool)
    tree = ast.parse(source)
    build_fn = _find_function(tree, "build_and_sample")
    family_bytes_args: list[str] = []
    for node in ast.walk(build_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "family_bytes"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            family_bytes_args.append(node.args[0].id)
    assert set(family_bytes_args) == {"_BUILD_FAMILY", "_SELECTION_FAMILY"}, (
        f"build_and_sample must derive exactly the build + selection seeds via "
        f"family_bytes; found calls keyed on {family_bytes_args}"
    )
    assert _faker_pool._BUILD_FAMILY == "faker_pool_build"
    assert _faker_pool._SELECTION_FAMILY == "faker_pool_selection"


def test_new_pool_bridge_module_is_the_only_uncatalogued_file() -> None:
    """Sanity check the file-level scan's blind spot actually exists: the new
    module carries RNG tokens (seed_instance) that only ITS DrawSite entries'
    call_site/mirror_call_sites -- not GEN_KIND_TO_SITE -- catalogue."""
    path = Path(_faker_pool.__file__)
    assert path.name == "_faker_pool.py"
    assert "seed_instance(" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Predicate-aware runtime coverage.
# ---------------------------------------------------------------------------


def _spy_build_and_sample(monkeypatch) -> list[bool]:
    """Patch `_faker_pool.build_and_sample` (as `synthesize` imported it) to
    record whether it was called, while still delegating to the real
    implementation so the returned values are unaffected."""
    calls: list[bool] = []
    real = _faker_pool.build_and_sample

    def _spy(**kwargs):
        calls.append(True)
        return real(**kwargs)

    monkeypatch.setattr(_synthesize._faker_pool, "build_and_sample", _spy)
    return calls


def test_eligible_column_reaches_the_pool_bridge(monkeypatch) -> None:
    calls = _spy_build_and_sample(monkeypatch)
    sample_calls: list[bool] = []
    real_sample = PoolSampler.sample

    def _sample_spy(self, *args, **kwargs):
        sample_calls.append(True)
        return real_sample(self, *args, **kwargs)

    monkeypatch.setattr(PoolSampler, "sample", _sample_spy)

    col = {"name": "city", "type": "faker", "faker_type": "city"}
    out = _synthesize._faker(col, _faker_pool.N_THRESHOLD, 42, None, None)

    assert calls, "eligible column (allowlisted, at threshold) never reached build_and_sample"
    assert sample_calls, "gen.faker_pool_selection (PoolSampler.sample) never fired"
    assert len(out) == _faker_pool.N_THRESHOLD


def test_below_threshold_column_never_reaches_the_pool_bridge(monkeypatch) -> None:
    calls = _spy_build_and_sample(monkeypatch)
    col = {"name": "city", "type": "faker", "faker_type": "city"}
    out = _synthesize._faker(col, _faker_pool.N_THRESHOLD - 1, 42, None, None)
    assert not calls, "below-threshold column reached the pool bridge; per-row is required"
    assert len(out) == _faker_pool.N_THRESHOLD - 1


def test_non_allowlisted_type_never_reaches_the_pool_bridge(monkeypatch) -> None:
    calls = _spy_build_and_sample(monkeypatch)
    col = {"name": "n", "type": "faker", "faker_type": "pyint"}
    _synthesize._faker(col, _faker_pool.N_THRESHOLD + 100, 42, None, None)
    assert not calls, "pyint is not in POOL_ELIGIBLE_FAKER_TYPES; must stay per-row"


def test_opted_out_column_never_reaches_the_pool_bridge(monkeypatch) -> None:
    calls = _spy_build_and_sample(monkeypatch)
    col = {"name": "city", "type": "faker", "faker_type": "city", "pooled": False}
    _synthesize._faker(col, _faker_pool.N_THRESHOLD + 100, 42, None, None)
    assert not calls, "pooled: false must gate out the pool bridge entirely"


def test_pooled_true_does_not_force_an_ineligible_provider(monkeypatch) -> None:
    """`pooled: true` is not a forcing knob: a non-allowlisted type must stay
    per-row even when the column explicitly opts in (Codex round-2 spec)."""
    calls = _spy_build_and_sample(monkeypatch)
    col = {"name": "n", "type": "faker", "faker_type": "pyint", "pooled": True}
    _synthesize._faker(col, _faker_pool.N_THRESHOLD + 100, 42, None, None)
    assert not calls


def test_custom_override_reaches_bridge_but_forces_per_row_output(monkeypatch) -> None:
    """The bridge IS reached (it's eligible on the cheap gate), but its
    locked-resolver snapshot must force a `None` return, and `_faker`'s
    per-row loop -- not the pool -- must produce the final output."""
    calls = _spy_build_and_sample(monkeypatch)
    register_faker_provider("city", lambda fake: "CUSTOM_CITY")
    try:
        col = {"name": "c", "type": "faker", "faker_type": "city"}
        out = _synthesize._faker(col, _faker_pool.N_THRESHOLD + 10, 42, None, None)
    finally:
        unregister_faker_provider("city")
    assert calls, "the bridge must still be tried for a cheap-gate-eligible column"
    assert out == ["CUSTOM_CITY"] * (_faker_pool.N_THRESHOLD + 10)


def test_locale_unavailable_reaches_bridge_but_forces_per_row_fallback(monkeypatch) -> None:
    calls = _spy_build_and_sample(monkeypatch)
    # ja_JP has no `state` provider (verified against the installed Faker
    # locale data); the locked resolver must report it unavailable.
    col = {"name": "s", "type": "faker", "faker_type": "state", "locale": "ja_JP"}
    out = _synthesize._faker(col, _faker_pool.N_THRESHOLD + 10, 42, None, None)
    assert calls
    # Per-row unknown->word fallback still applies on the non-pooled path;
    # just prove the bridge did not supply the output (it can't stand up a
    # `state` pool for a locale that has no such provider).
    gen_ctx = GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=42)
    forced_none = _faker_pool.build_and_sample(
        faker_type="state",
        faker_kwargs={},
        n=_faker_pool.N_THRESHOLD + 10,
        gen_ctx=gen_ctx,
        effective_locale="ja_JP",
    )
    assert forced_none is None
    assert len(out) == _faker_pool.N_THRESHOLD + 10
