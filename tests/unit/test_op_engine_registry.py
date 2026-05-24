"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression -- if someone adds a new op that forgets to declare,
the test fails.

Sprint 1.5 additions: op contract metadata tests (KIND, INPUT_ARITY,
OUTPUT_KIND, OUTPUT_PORTS). The validator and runner rely on getattr()
fallbacks for these constants; tests here catch a missing declaration
before it reaches execution.
"""

from __future__ import annotations

import types

import pytest

from decoy_engine.graph.conversion import VALID_ENGINES
from decoy_engine.graph.ops import OPS
from decoy_engine.graph.registry import native_engine_for


def test_every_op_declares_native_engine():
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "NATIVE_ENGINE")]
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
        f"ops with invalid NATIVE_ENGINE: {invalid}. Must be one of {VALID_ENGINES}."
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


@pytest.mark.parametrize(
    "kind,expected_engine",
    [
        # Frozen baseline of NATIVE_ENGINE per kind. The list moves explicitly
        # as phases land: Phase 3 flipped the relational ops to polars; Phase 4
        # will flip the source.* / target.* ops to duckdb. A surprise diff in
        # this list = an undocumented engine flip -- fail loud, don't shrug.
        ("source.file", "duckdb"),  # Phase 4
        ("source.db", "duckdb"),  # Phase 4
        ("filter", "polars"),  # Phase 3
        ("sort", "polars"),  # Phase 3
        ("dedupe", "polars"),  # Phase 3
        ("derive", "polars"),  # Phase 3
        ("drop_column", "polars"),  # Phase 3
        ("select_column", "polars"),  # Phase 3
        ("limit", "polars"),  # Phase 3
        ("run_storm", "pandas"),  # stays pandas (Phase 1 benchmark: 2.4% Arrow overhead)
        ("mask", "pandas"),  # stays pandas (per-row Python)
        ("generate", "pandas"),  # stays pandas (per-row Python)
        ("target.file", "duckdb"),  # Phase 4
        ("target.db", "duckdb"),  # Phase 4
        ("convert.file_type", "duckdb"),  # Item 57 + 66(b): wraps DuckDB COPY ... TO
    ],
)
def test_op_engine_baseline_declarations(kind, expected_engine):
    """Frozen baseline. Updates here are intentional; surprises are not."""
    op = OPS[kind]
    assert expected_engine == op.NATIVE_ENGINE


def test_validator_rejects_bad_native_engine_declaration(monkeypatch):
    """The node-level validator must catch invalid NATIVE_ENGINE at graph-
    validation time, not silently fall back to pandas at execution time.

    Simulates the failure mode where a developer adds an op with a typo or
    wrong value in its NATIVE_ENGINE constant. Previously the registry fell
    back silently; now validate_nodes raises NODE_BAD_NATIVE_ENGINE before
    any op executes.

    V2.0-B: assertion runs against the modular validate_nodes function
    directly (the bundled GraphConfigValidator was deleted in V2.0-B).
    """
    from decoy_engine.graph.validators import known_kinds, validate_nodes
    from decoy_engine.internal.validator import ValidationError
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

    nodes = [{"id": "n1", "kind": "test_bad_engine_kind", "config": {}}]

    with pytest.raises(ValidationError) as exc_info:
        validate_nodes(nodes, known_kinds())

    err = exc_info.value
    assert err.code == CODES.NODE_BAD_NATIVE_ENGINE
    assert "not_a_real_engine" in str(err)
    assert "test_bad_engine_kind" in str(err)


# ---------------------------------------------------------------------------
# Sprint 1.5: op contract metadata declaration tests
# ---------------------------------------------------------------------------


def test_every_op_declares_kind():
    """Every op module must declare KIND matching the OPS registry key."""
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "KIND")]
    assert not missing, (
        f"ops missing KIND declaration: {missing}. "
        "Each op module must declare KIND = '<kind-string>'."
    )


def test_op_kind_matches_registry_key():
    """op.KIND must equal the key used to register it in OPS.

    This catches copy-paste errors where an op module's KIND constant
    drifts from the registry key, causing confusing validation messages.
    """
    mismatched = [(key, op.KIND) for key, op in OPS.items() if getattr(op, "KIND", None) != key]
    assert not mismatched, (
        f"ops whose KIND does not match their registry key: {mismatched}. "
        "op.KIND must equal the OPS dict key."
    )


def test_every_op_declares_input_arity():
    """Every op must declare INPUT_ARITY as (min: int, max: int | None).

    The validator and runner use INPUT_ARITY for cardinality checks.
    A missing or malformed declaration silently falls back to (1, 1),
    which is wrong for sources (0, 0), gates (0, 1), and fan-in ops.
    """
    bad = []
    for kind, op in OPS.items():
        arity = getattr(op, "INPUT_ARITY", None)
        if arity is None:
            bad.append((kind, "missing"))
        elif not (
            isinstance(arity, tuple)
            and len(arity) == 2
            and isinstance(arity[0], int)
            and (arity[1] is None or isinstance(arity[1], int))
        ):
            bad.append((kind, repr(arity)))
    assert not bad, (
        f"ops with missing or malformed INPUT_ARITY: {bad}. "
        "Must be a 2-tuple (min: int, max: int | None)."
    )


_VALID_OUTPUT_KINDS = frozenset({"stream", "sink", "split"})


def test_every_op_declares_output_kind():
    """Every op must declare OUTPUT_KIND as 'stream', 'sink', or 'split'.

    The validator uses OUTPUT_KIND to reject outgoing edges from sinks
    and to allow port notation on split ops.
    """
    bad = [
        (kind, getattr(op, "OUTPUT_KIND", None))
        for kind, op in OPS.items()
        if getattr(op, "OUTPUT_KIND", None) not in _VALID_OUTPUT_KINDS
    ]
    assert not bad, (
        f"ops with missing or invalid OUTPUT_KIND: {bad}. "
        f"Must be one of {sorted(_VALID_OUTPUT_KINDS)}."
    )


def test_split_ops_declare_output_ports():
    """Ops with OUTPUT_KIND='split' must declare OUTPUT_PORTS as a non-empty tuple.

    The runner uses OUTPUT_PORTS to key split outputs in the Arrow cache.
    A split op that omits OUTPUT_PORTS would silently produce no cached
    output and leave downstream consumers reading None.
    """
    bad = []
    for kind, op in OPS.items():
        if getattr(op, "OUTPUT_KIND", None) == "split":
            ports = getattr(op, "OUTPUT_PORTS", None)
            if not (isinstance(ports, tuple) and len(ports) > 0):
                bad.append((kind, repr(ports)))
    assert not bad, (
        f"split ops with missing or empty OUTPUT_PORTS: {bad}. "
        "Split ops must declare OUTPUT_PORTS as a non-empty tuple of port name strings."
    )
