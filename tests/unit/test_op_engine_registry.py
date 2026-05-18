"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression -- if someone adds a new op that forgets to declare,
the test fails.
"""

from __future__ import annotations

import types

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
    """`engine: pandas` is the post-Phase-8 opt-out / safety hatch:
    every op resolves to pandas regardless of its declaration. Lives for
    one release cycle past the default flip; then the pandas fallbacks
    get deleted and this flag becomes a no-op."""
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
    the registry must not raise -- defensive fallback."""
    assert native_engine_for("does_not_exist", "hybrid") == "pandas"
    assert native_engine_for("does_not_exist", "pandas") == "pandas"


@pytest.mark.parametrize("kind,expected_engine", [
    # Frozen baseline of NATIVE_ENGINE per kind. The list moves explicitly
    # as phases land: Phase 3 flipped the relational ops to polars; Phase 4
    # flipped the source.* / target.* file ops to duckdb. A surprise diff
    # in this list = an undocumented engine flip -- fail loud, don't shrug.
    #
    # File source / target ops (Phase 4: DuckDB COPY ... TO / read_csv)
    ("source.file", "duckdb"),
    ("source.db", "duckdb"),
    ("target.file", "duckdb"),
    ("target.db", "duckdb"),
    # Cloud source / target ops (same DuckDB path as file ops via _cloud_io)
    ("source.s3", "duckdb"),
    ("source.gcs", "duckdb"),
    ("source.sftp", "duckdb"),
    ("target.s3", "duckdb"),
    ("target.gcs", "duckdb"),
    ("target.sftp", "duckdb"),
    # SQL escape hatch (DuckDB in-memory connection per invocation)
    ("sql_run", "duckdb"),
    # File-type converter (DuckDB COPY ... TO)
    ("convert.file_type", "duckdb"),
    # Relational transform ops (Phase 3: polars SQLContext / sort)
    ("filter", "polars"),
    ("sort", "polars"),
    ("dedupe", "polars"),
    ("derive", "polars"),
    ("drop_column", "polars"),
    ("select_column", "polars"),
    ("limit", "polars"),
    # Two-port router (polars SQLContext, same dialect as filter)
    ("if", "polars"),
    # Masking / generation -- stay on pandas (per-row Python callbacks)
    ("run_storm", "pandas"),   # Phase 1 benchmark: 2.4% Arrow overhead; stays pandas
    ("mask", "pandas"),        # per-row Faker / scipy strategies
    ("generate", "pandas"),    # per-row Faker / sequence / categorical
    # Multi-table join -- stays on pandas (df.merge / pd.concat)
    ("unite", "pandas"),
    # Gate / review -- stays on pandas for substrate-neutral pre-conversion
    ("flag_gate", "pandas"),
    # Orchestration ops -- emit / receive Arrow across sub-pipeline boundary
    ("sub_pipeline", "arrow"),
    ("iterate_fixed", "arrow"),
    ("iterate_loop", "arrow"),
    ("iterate_files", "arrow"),
])
def test_op_engine_baseline_declarations(kind, expected_engine):
    """Frozen baseline. Updates here are intentional; surprises are not."""
    op = OPS[kind]
    assert getattr(op, "NATIVE_ENGINE") == expected_engine


def test_baseline_covers_all_registered_ops():
    """The parametrized baseline above should cover every op in OPS.

    This test fails if a new op is added to OPS without also being added
    to test_op_engine_baseline_declarations. Keeps the frozen list
    self-maintaining: you can't register a new op without explicitly
    documenting its engine choice.
    """
    baseline_kinds = {
        # File source / target
        "source.file", "source.db", "target.file", "target.db",
        # Cloud source / target
        "source.s3", "source.gcs", "source.sftp",
        "target.s3", "target.gcs", "target.sftp",
        # SQL / convert
        "sql_run", "convert.file_type",
        # Relational transforms
        "filter", "sort", "dedupe", "derive",
        "drop_column", "select_column", "limit",
        # Router
        "if",
        # Masking / generation
        "run_storm", "mask", "generate",
        # Multi-table
        "unite",
        # Gate
        "flag_gate",
        # Orchestration
        "sub_pipeline", "iterate_fixed", "iterate_loop", "iterate_files",
    }
    registered = set(OPS.keys())
    uncovered = registered - baseline_kinds
    assert not uncovered, (
        f"ops not in NATIVE_ENGINE baseline: {sorted(uncovered)}. "
        "Add each new op to test_op_engine_baseline_declarations with its "
        "expected NATIVE_ENGINE before merging."
    )


def test_validator_rejects_bad_native_engine_declaration(monkeypatch):
    """GraphConfigValidator must catch invalid NATIVE_ENGINE at graph-validation
    time, not silently fall back to pandas at execution time.

    Simulates the failure mode where a developer adds an op with a typo or
    wrong value in its NATIVE_ENGINE constant. Previously the registry fell
    back silently; now the validator raises NODE_BAD_NATIVE_ENGINE before
    any op executes.
    """
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

    # A minimal fake op with an invalid engine string.
    bad_op = types.SimpleNamespace(
        KIND="test_bad_engine_kind",
        NATIVE_ENGINE="not_a_real_engine",
        INPUT_ARITY=(0, None),
        OUTPUT_KIND="stream",
        validate_config=lambda cfg: None,
    )
    monkeypatch.setitem(OPS, "test_bad_engine_kind", bad_op)

    config = {
        "mode": "graph",
        "nodes": [{"id": "n1", "kind": "test_bad_engine_kind", "config": {}}],
        "edges": [],
    }

    validator = GraphConfigValidator()
    with pytest.raises(ValidationError) as exc_info:
        validator.validate(config)

    err = exc_info.value
    assert err.code == CODES.NODE_BAD_NATIVE_ENGINE
    assert "not_a_real_engine" in str(err)
    assert "test_bad_engine_kind" in str(err)
