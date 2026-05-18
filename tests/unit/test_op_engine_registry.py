"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression -- if someone adds a new op that forgets to declare,
the test fails.

Sprint 1.5 additions: registry-level checks for KIND, INPUT_ARITY,
OUTPUT_KIND, and OUTPUT_PORTS contract compliance.
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


def test_every_op_declares_kind():
    """Sprint 1.5: every op must declare KIND matching its registry key."""
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "KIND")]
    assert not missing, (
        f"ops missing KIND declaration: {missing}. "
        "Each op module must declare KIND = '<registry-key>'."
    )
    mismatched = [
        (key, op.KIND) for key, op in OPS.items() if op.KIND != key
    ]
    assert not mismatched, (
        f"ops where KIND != registry key: {mismatched}. "
        "The KIND constant must equal the key used in OPS."
    )


def test_every_op_declares_input_arity():
    """Sprint 1.5: every op must declare a structurally valid INPUT_ARITY."""
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "INPUT_ARITY")]
    assert not missing, (
        f"ops missing INPUT_ARITY declaration: {missing}. "
        "Each op module must declare INPUT_ARITY = (min: int, max: int | None)."
    )
    malformed = []
    for kind, op in OPS.items():
        arity = op.INPUT_ARITY
        ok = (
            isinstance(arity, tuple)
            and len(arity) == 2
            and isinstance(arity[0], int)
            and arity[0] >= 0
            and (arity[1] is None or (isinstance(arity[1], int) and arity[1] >= arity[0]))
        )
        if not ok:
            malformed.append((kind, arity))
    assert not malformed, (
        f"ops with malformed INPUT_ARITY: {malformed}. "
        "Must be (min: int>=0, max: int>=min or None)."
    )


def test_every_op_declares_output_kind():
    """Sprint 1.5: every op must declare a valid OUTPUT_KIND."""
    _VALID = {"stream", "split"}
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "OUTPUT_KIND")]
    assert not missing, (
        f"ops missing OUTPUT_KIND declaration: {missing}. "
        f"Each op module must declare OUTPUT_KIND in {_VALID}."
    )
    invalid = [
        (kind, op.OUTPUT_KIND) for kind, op in OPS.items()
        if op.OUTPUT_KIND not in _VALID
    ]
    assert not invalid, (
        f"ops with invalid OUTPUT_KIND: {invalid}. Must be one of {_VALID}."
    )


def test_split_ops_declare_output_ports():
    """Sprint 1.5: ops declaring OUTPUT_KIND='split' must have OUTPUT_PORTS."""
    bad = []
    for kind, op in OPS.items():
        if getattr(op, "OUTPUT_KIND", None) != "split":
            continue
        ports = getattr(op, "OUTPUT_PORTS", None)
        if (
            not ports
            or not isinstance(ports, (tuple, list))
            or not all(isinstance(p, str) for p in ports)
        ):
            bad.append((kind, ports))
    assert not bad, (
        f"split ops with missing/malformed OUTPUT_PORTS: {bad}. "
        "Must be a non-empty tuple of strings."
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
    time, not silently fall back to pandas at execution time."""
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

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


def test_validator_rejects_kind_mismatch(monkeypatch):
    """Validator catches when op.KIND != registry key."""
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

    bad_op = types.SimpleNamespace(
        KIND="wrong_kind_name",
        NATIVE_ENGINE="pandas",
        INPUT_ARITY=(1, 1),
        OUTPUT_KIND="stream",
        validate_config=lambda cfg: None,
    )
    monkeypatch.setitem(OPS, "test_kind_mismatch_op", bad_op)

    config = {
        "mode": "graph",
        "nodes": [{"id": "n1", "kind": "test_kind_mismatch_op", "config": {}}],
        "edges": [],
    }

    validator = GraphConfigValidator()
    with pytest.raises(ValidationError) as exc_info:
        validator.validate(config)

    err = exc_info.value
    assert err.code == CODES.NODE_KIND_MISMATCH
    assert "wrong_kind_name" in str(err)


def test_validator_rejects_bad_input_arity(monkeypatch):
    """Validator catches malformed INPUT_ARITY declarations."""
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

    bad_op = types.SimpleNamespace(
        KIND="test_bad_arity_op",
        NATIVE_ENGINE="pandas",
        INPUT_ARITY="one",  # wrong type
        OUTPUT_KIND="stream",
        validate_config=lambda cfg: None,
    )
    monkeypatch.setitem(OPS, "test_bad_arity_op", bad_op)

    config = {
        "mode": "graph",
        "nodes": [{"id": "n1", "kind": "test_bad_arity_op", "config": {}}],
        "edges": [],
    }

    validator = GraphConfigValidator()
    with pytest.raises(ValidationError) as exc_info:
        validator.validate(config)

    assert exc_info.value.code == CODES.NODE_BAD_INPUT_ARITY


def test_validator_rejects_bad_output_kind(monkeypatch):
    """Validator catches invalid OUTPUT_KIND values."""
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

    bad_op = types.SimpleNamespace(
        KIND="test_bad_output_kind_op",
        NATIVE_ENGINE="pandas",
        INPUT_ARITY=(1, 1),
        OUTPUT_KIND="firehose",  # not a real kind
        validate_config=lambda cfg: None,
    )
    monkeypatch.setitem(OPS, "test_bad_output_kind_op", bad_op)

    config = {
        "mode": "graph",
        "nodes": [{"id": "n1", "kind": "test_bad_output_kind_op", "config": {}}],
        "edges": [],
    }

    validator = GraphConfigValidator()
    with pytest.raises(ValidationError) as exc_info:
        validator.validate(config)

    assert exc_info.value.code == CODES.NODE_BAD_OUTPUT_KIND


def test_validator_rejects_split_missing_ports(monkeypatch):
    """Validator catches split ops that forget OUTPUT_PORTS."""
    from decoy_engine.internal.validator import GraphConfigValidator, ValidationError
    from decoy_engine.validation_result import CODES

    bad_op = types.SimpleNamespace(
        KIND="test_split_no_ports_op",
        NATIVE_ENGINE="pandas",
        INPUT_ARITY=(1, 1),
        OUTPUT_KIND="split",
        # OUTPUT_PORTS intentionally absent
        validate_config=lambda cfg: None,
    )
    monkeypatch.setitem(OPS, "test_split_no_ports_op", bad_op)

    config = {
        "mode": "graph",
        "nodes": [{"id": "n1", "kind": "test_split_no_ports_op", "config": {}}],
        "edges": [],
    }

    validator = GraphConfigValidator()
    with pytest.raises(ValidationError) as exc_info:
        validator.validate(config)

    assert exc_info.value.code == CODES.NODE_SPLIT_MISSING_PORTS
