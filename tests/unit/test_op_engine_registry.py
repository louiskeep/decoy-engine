"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression — if someone adds a new op that forgets to declare,
the test fails.
"""

from __future__ import annotations

import pytest

from decoy_engine.graph.conversion import VALID_ENGINES
from decoy_engine.graph.ops import OPS
from decoy_engine.graph.registry import native_engine_for


def test_every_op_declares_native_engine():
    missing = [
        kind for kind, op in OPS.items() if not hasattr(op, "NATIVE_ENGINE")
    ]
    assert not missing, (
        f"ops missing NATIVE_ENGINE declaration: {missing}. "
        "Each op module must declare NATIVE_ENGINE = 'pandas' | 'polars' | 'duckdb' | 'arrow'."
    )


def test_every_declared_engine_is_valid():
    invalid = []
    for kind, op in OPS.items():
        engine = getattr(op, "NATIVE_ENGINE", None)
        if engine not in VALID_ENGINES:
            invalid.append((kind, engine))
    assert not invalid, (
        f"ops with invalid NATIVE_ENGINE: {invalid}. "
        f"Must be one of {VALID_ENGINES}."
    )


def test_pandas_mode_forces_pandas_for_all_ops():
    """Until Phase 4 ships the `engine: hybrid` flag, the runner always
    runs in pandas mode and every op must resolve to pandas regardless
    of its declaration."""
    for kind in OPS:
        assert native_engine_for(kind, "pandas") == "pandas", (
            f"{kind} should resolve to pandas when graph engine mode is pandas"
        )


def test_hybrid_mode_respects_op_declaration():
    """In hybrid mode, the registry should return whatever the op declared."""
    for kind, op in OPS.items():
        declared = getattr(op, "NATIVE_ENGINE", "pandas")
        assert native_engine_for(kind, "hybrid") == declared, (
            f"{kind} declared NATIVE_ENGINE={declared!r} but registry "
            f"returned {native_engine_for(kind, 'hybrid')!r} in hybrid mode"
        )


def test_unknown_kind_falls_back_to_pandas():
    """If a kind isn't in the registry (would normally fail validation),
    the registry must not raise — defensive fallback."""
    assert native_engine_for("does_not_exist", "hybrid") == "pandas"
    assert native_engine_for("does_not_exist", "pandas") == "pandas"


@pytest.mark.parametrize("kind,expected_engine", [
    # All Phase 2 ops should be pandas. This list is the explicit baseline:
    # if someone changes an op's NATIVE_ENGINE in Phase 3+, they update this
    # test alongside, which makes the engine-flip auditable in PR diffs.
    ("source.file", "pandas"),
    ("source.db", "pandas"),
    ("filter", "pandas"),
    ("sort", "pandas"),
    ("dedupe", "pandas"),
    ("derive", "pandas"),
    ("drop_column", "pandas"),
    ("select_column", "pandas"),
    ("limit", "pandas"),
    ("run_storm", "pandas"),
    ("mask", "pandas"),
    ("generate", "pandas"),
    ("target.file", "pandas"),
    ("target.db", "pandas"),
])
def test_phase_2_baseline_declarations(kind, expected_engine):
    """Frozen baseline. Updates here are intentional; surprises are not."""
    op = OPS[kind]
    assert getattr(op, "NATIVE_ENGINE") == expected_engine
