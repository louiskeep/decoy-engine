"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression -- if someone adds a new op that forgets to declare,
the test fails.

Sprint 1.5 additions: op contract metadata tests for KIND, INPUT_ARITY,
OUTPUT_KIND, OUTPUT_PORTS, and HAS_SIDE_EFFECTS.
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
    # will flip the source.* / target.* ops to duckdb. A surprise diff in
    # this list = an undocumented engine flip -- fail loud, don't shrug.
    ("source.file", "duckdb"),       # Phase 4
    ("source.db", "duckdb"),         # Phase 4
    ("filter", "polars"),            # Phase 3
    ("sort", "polars"),              # Phase 3
    ("dedupe", "polars"),            # Phase 3
    ("derive", "polars"),            # Phase 3
    ("drop_column", "polars"),       # Phase 3
    ("select_column", "polars"),     # Phase 3
    ("limit", "polars"),             # Phase 3
    ("run_storm", "pandas"),         # stays pandas (Phase 1 benchmark: 2.4% Arrow overhead)
    ("mask", "pandas"),              # stays pandas (per-row Python)
    ("generate", "pandas"),          # stays pandas (per-row Python)
    ("target.file", "duckdb"),       # Phase 4
    ("target.db", "duckdb"),         # Phase 4
    ("convert.file_type", "duckdb"), # Item 57 + 66(b): wraps DuckDB COPY ... TO
])
def test_op_engine_baseline_declarations(kind, expected_engine):
    """Frozen baseline. Updates here are intentional; surprises are not."""
    op = OPS[kind]
    assert getattr(op, "NATIVE_ENGINE") == expected_engine


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


# ---------------------------------------------------------------------------
# Sprint 1.5: op contract metadata tests
# These run at test time to catch missing or malformed op metadata before it
# reaches production. Because op metadata is declared in source code rather
# than user-supplied config, these are test-layer assertions rather than
# runtime validations.
# ---------------------------------------------------------------------------

_VALID_OUTPUT_KINDS = frozenset({"stream", "sink", "split"})


def test_every_op_declares_kind():
    """Every registered op module must declare a KIND string."""
    missing = [
        key for key, op in OPS.items()
        if not isinstance(getattr(op, "KIND", None), str)
    ]
    assert not missing, (
        f"ops missing KIND declaration: {missing}. "
        "Each op module must declare KIND = '<yaml-kind-string>'."
    )


def test_every_op_kind_matches_registry_key():
    """Each op's KIND must equal the key it is registered under in OPS.

    Prevents typos where the op says KIND='source.s3' but is registered
    under 'source.gcs'. A diff here is a bug, not a design choice.
    """
    mismatches = [
        (key, getattr(op, "KIND", None))
        for key, op in OPS.items()
        if getattr(op, "KIND", None) != key
    ]
    assert not mismatches, (
        f"ops whose KIND does not match their registry key: {mismatches}."
    )


def test_every_op_declares_input_arity():
    """Every registered op must declare INPUT_ARITY as a (min, max) 2-tuple.

    min is a non-negative int; max is a non-negative int or None
    (unbounded). A missing or malformed INPUT_ARITY leaves the graph
    validator unable to enforce node connection counts at validation time.
    """
    bad = []
    for key, op in OPS.items():
        arity = getattr(op, "INPUT_ARITY", None)
        if (
            not isinstance(arity, tuple)
            or len(arity) != 2
            or not isinstance(arity[0], int)
            or (arity[1] is not None and not isinstance(arity[1], int))
        ):
            bad.append((key, arity))
    assert not bad, (
        f"ops with missing or malformed INPUT_ARITY: {bad}. "
        "Declare INPUT_ARITY: tuple[int, int | None] = (min, max)."
    )


def test_every_op_declares_output_kind():
    """Every registered op must declare OUTPUT_KIND as 'stream', 'sink', or 'split'."""
    bad = [
        (key, getattr(op, "OUTPUT_KIND", None))
        for key, op in OPS.items()
        if getattr(op, "OUTPUT_KIND", None) not in _VALID_OUTPUT_KINDS
    ]
    assert not bad, (
        f"ops with missing or invalid OUTPUT_KIND: {bad}. "
        f"Must be one of {sorted(_VALID_OUTPUT_KINDS)}."
    )


def test_every_split_op_declares_output_ports():
    """Ops with OUTPUT_KIND='split' must declare OUTPUT_PORTS as a non-empty tuple.

    OUTPUT_PORTS names the downstream graph edges the runner creates for the
    op. Without it the runner cannot route branch outputs and the graph
    validator cannot check edge port references.
    """
    bad = []
    for key, op in OPS.items():
        if getattr(op, "OUTPUT_KIND", None) == "split":
            ports = getattr(op, "OUTPUT_PORTS", None)
            if not isinstance(ports, tuple) or not ports:
                bad.append((key, ports))
    assert not bad, (
        f"split ops missing OUTPUT_PORTS declaration: {bad}. "
        "Declare OUTPUT_PORTS = ('port_a', 'port_b', ...) on every split op."
    )


def test_sink_ops_declare_has_side_effects_true():
    """Ops with OUTPUT_KIND='sink' must declare HAS_SIDE_EFFECTS = True.

    The preview policy uses this flag to skip ops that write to external
    storage during a canvas preview. A sink that omits this flag may
    accidentally write to a production target while the user previews the
    pipeline.
    """
    bad = []
    for key, op in OPS.items():
        if getattr(op, "OUTPUT_KIND", None) == "sink":
            flag = getattr(op, "HAS_SIDE_EFFECTS", None)
            if flag is not True:
                bad.append((key, flag))
    assert not bad, (
        f"sink ops missing HAS_SIDE_EFFECTS = True: {bad}. "
        "Declare HAS_SIDE_EFFECTS = True on every sink op."
    )


@pytest.mark.parametrize("kind,expected_output_kind", [
    # Frozen OUTPUT_KIND baseline. A surprise diff = accidental contract change.
    ("source.file", "stream"),
    ("source.db", "stream"),
    ("source.s3", "stream"),
    ("source.gcs", "stream"),
    ("source.sftp", "stream"),
    ("filter", "stream"),
    ("sort", "stream"),
    ("dedupe", "stream"),
    ("derive", "stream"),
    ("drop_column", "stream"),
    ("select_column", "stream"),
    ("limit", "stream"),
    ("unite", "stream"),
    ("run_storm", "stream"),
    ("mask", "stream"),
    ("generate", "stream"),
    ("sql_run", "stream"),
    ("sub_pipeline", "stream"),
    ("iterate_fixed", "stream"),
    ("iterate_loop", "stream"),
    ("iterate_files", "stream"),
    ("flag_gate", "stream"),
    ("convert.file_type", "stream"),
    ("if", "split"),
    ("target.file", "sink"),
    ("target.db", "sink"),
    ("target.s3", "sink"),
    ("target.gcs", "sink"),
    ("target.sftp", "sink"),
])
def test_op_output_kind_baseline(kind, expected_output_kind):
    """Frozen OUTPUT_KIND baseline. Updates here are intentional; surprises are not."""
    op = OPS[kind]
    assert getattr(op, "OUTPUT_KIND") == expected_output_kind, (
        f"{kind!r}: expected OUTPUT_KIND={expected_output_kind!r}, "
        f"got {getattr(op, 'OUTPUT_KIND', None)!r}"
    )
