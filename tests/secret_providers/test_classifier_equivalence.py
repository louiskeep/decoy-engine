"""F13 §5 equivalence: the config-only classifier and the compiled-plan classifier agree.

The config classifier runs BEFORE any source read, so it is conservative: it must report keyed
whenever the plan is keyed (drift would admit keyed work without a secret), and it must agree
exactly on every strategy it recognises. Unrecognised strategies default to keyed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from decoy_engine.config import PipelineConfig
from decoy_engine.execution._technique_class import TECHNIQUE_CLASS_BY_STRATEGY
from decoy_engine.plan import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import profile_source
from decoy_engine.secret_providers import (
    secret_requirements_for_config,
    secret_requirements_for_plan,
)
from decoy_engine.secret_providers._requirements import (
    ALWAYS_KEYED_STRATEGIES,
    KNOWN_UNKEYED_STRATEGIES,
    column_is_keyed,
)

# Strategies whose columns compile with only a name + strategy (no extra required config).
SIMPLE = [
    "redact",
    "passthrough",
    "truncate",
    "hash",
    "fpe",
    "shuffle",
    "categorical",
    "text_redact",
]


_DIR = Path(tempfile.mkdtemp(prefix="f13-equiv-"))
(_DIR / "t.csv").write_text("email\n" + "\n".join(["a", "b", "a", "b"] * 5) + "\n")


def _compile(_unused, columns):
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "csv", "path": str(_DIR / "t.csv")}},
            "tables": [{"name": "t", "columns": columns}],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(_DIR / "o.csv")}},
        }
    ).model_dump()


def test_strategy_universe_is_fully_classified():
    universe = set(TECHNIQUE_CLASS_BY_STRATEGY) | {"from_parent"}
    assert universe <= ALWAYS_KEYED_STRATEGIES | KNOWN_UNKEYED_STRATEGIES, (
        "a strategy the registry knows is unclassified; the conservative default would force a "
        "secret for it. Add it to ALWAYS_KEYED_STRATEGIES or KNOWN_UNKEYED_STRATEGIES."
    )
    assert not ALWAYS_KEYED_STRATEGIES & KNOWN_UNKEYED_STRATEGIES


def test_unrecognised_strategies_default_to_keyed():
    assert column_is_keyed("brand_new", deterministic=False, vault=False, provider_config={})
    assert column_is_keyed(
        "nested", deterministic=False, vault=False, provider_config={"strategy": "brand_new"}
    )
    assert not column_is_keyed("redact", deterministic=False, vault=False, provider_config={})


@settings(max_examples=60, deadline=None)
@given(
    strategy=st.sampled_from(SIMPLE),
    deterministic=st.booleans(),
    vault=st.booleans(),
)
def test_config_and_plan_classifiers_agree_on_recognised_strategies(strategy, deterministic, vault):
    col = {"name": "email", "strategy": strategy, "namespace": "ns"}
    if strategy == "truncate":
        col["provider_config"] = {"length": 3}
    if strategy == "categorical":
        col["provider_config"] = {"categories": ["a", "b"]}
    if deterministic and strategy in ("categorical",):
        col["deterministic"] = True
    if vault:
        col["vault"] = True
    cfg = _compile(None, [col])
    prof = profile_source(cfg, seed=1)
    try:
        plan = compile_plan(cfg, prof, decoy_engine_version="f13")
    except PlanCompileError:
        assume(False)  # the compiler rejects this combination; nothing to classify
    assert bool(secret_requirements_for_config(cfg)) == bool(secret_requirements_for_plan(plan))


def test_composite_fk_groups_are_keyed_from_config_alone():
    cfg = _compile(None, [{"name": "a", "strategy": "redact"}])
    cfg["relationships"] = [
        {
            "parent": {"table": "p", "columns": ["x", "y"]},
            "children": [{"table": "t", "columns": ["a", "b"]}],
            "orphan_policy": "preserve",
        }
    ]
    assert secret_requirements_for_config(cfg)


def test_generate_tables_do_not_require_a_secret():
    cfg = _compile(None, [{"name": "a", "strategy": "redact"}])
    cfg["tables"] = [
        {"name": "g", "generate_columns": [{"name": "n", "type": "int"}], "columns": []}
    ]
    assert secret_requirements_for_config(cfg) == ()
