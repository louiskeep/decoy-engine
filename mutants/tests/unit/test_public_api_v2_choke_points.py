"""Smoke cells for the V2 mask-job choke-point re-exports.

CLI.1 (2026-06-01, Q-CLI1-1 Dennis verdict OVERSIGHT): `compile_plan`
and `select_execution_adapter` are V2 public-surface entry names but
were historically only importable from submodules. Folding the
re-export into CLI.1 closes the gap so callers can write
``from decoy_engine import compile_plan, select_execution_adapter``
instead of submodule chains.

These three cells are a regression guard: if either name disappears
from the top-level package, the CLI's V2 dispatch breaks at import
time. The fuller __all__ enumeration lives in test_public_api.py;
this file exists so a future contributor who deletes either symbol
gets a small, targeted failure first.
"""


def test_v2_choke_points_importable_from_top_level():
    from decoy_engine import compile_plan, select_execution_adapter

    assert callable(compile_plan)
    assert callable(select_execution_adapter)


def test_compile_plan_identity_matches_submodule():
    from decoy_engine import compile_plan as top_compile_plan
    from decoy_engine.plan import compile_plan as sub_compile_plan

    assert top_compile_plan is sub_compile_plan


def test_select_execution_adapter_identity_matches_submodule():
    from decoy_engine import select_execution_adapter as top_select
    from decoy_engine.execution import select_execution_adapter as sub_select

    assert top_select is sub_select
