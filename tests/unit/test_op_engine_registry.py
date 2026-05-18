"""Phase 2 of polars-duckdb hybrid plan: every op declares its NATIVE_ENGINE
and the registry resolves it correctly.

Phase 2 leaves every op on `pandas` so behavior is unchanged from Phase 1.
Phases 3 + 4 flip individual ops to polars / duckdb; this test prevents
silent regression -- if someone adds a new op that forgets to declare,
the test fails.

Sprint 1.5 adds metadata validation tests for KIND, INPUT_ARITY, OUTPUT_KIND,
and OUTPUT_PORTS, completing the op contract checklist from the audit
remediation roadmap.
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
# Sprint 1.5 remaining: op metadata contract tests
# ---------------------------------------------------------------------------
#
# These tests complete the audit remediation roadmap Sprint 1.5 task:
# "Validate op metadata for release ops: KIND, INPUT_ARITY, OUTPUT_KIND,
# OUTPUT_PORTS, and side-effect behavior."
#
# The NATIVE_ENGINE tests above covered the first attribute. The tests below
# cover the rest, making them explicit contracts rather than silent runtime
# assumptions with fallback defaults in the validator.

VALID_OUTPUT_KINDS = frozenset({"stream", "sink", "split"})

# Ops that intentionally stay on pandas because their strategies rely on
# per-row Python callbacks that do not vectorize to polars or duckdb.
# Listing them explicitly here catches a new op accidentally using pandas
# as the default rather than as a deliberate choice.
#
# If you add an op to this set, add a comment in the op module explaining
# why pandas (e.g. "per-row Faker callbacks", "orchestration-only",
# "scan mode only; Arrow overhead > polars gain at V1 scale").
_INTENTIONALLY_PANDAS: frozenset[str] = frozenset({
    "mask",         # per-row Faker/scipy/custom masking callbacks
    "generate",     # per-row Faker reference lookups and relationship handlers
    "run_storm",    # scan mode only; phase-1 benchmark: Arrow overhead > polars gain
    "flag_gate",    # operates on row counts and column names, not cell values
    "sub_pipeline", # orchestration-only; delegates to a child runner
    "iterate_fixed",  # orchestration; injects iteration.value as template vars
    "iterate_loop",   # orchestration; same pattern as iterate_fixed
    "iterate_files",  # orchestration; iterates over file paths
})


def test_every_op_declares_kind():
    """Every op module must declare KIND matching its registry key."""
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "KIND")]
    assert not missing, (
        f"ops missing KIND declaration: {missing}. "
        "Add KIND = '<registry_key>' to the op module."
    )


def test_kind_matches_registry_key():
    """KIND declared on the op module must match the key it's registered under.

    Catches copy-paste where a new op forgets to update KIND after copying
    from another op module.
    """
    mismatches = [
        (key, getattr(op, "KIND"))
        for key, op in OPS.items()
        if hasattr(op, "KIND") and getattr(op, "KIND") != key
    ]
    assert not mismatches, (
        f"op KIND doesn't match registry key: {mismatches}. "
        "The KIND constant in the module and the OPS dict key must be identical."
    )


def test_every_op_declares_input_arity():
    """Every op must declare INPUT_ARITY so cardinality validation has a contract.

    Without an explicit declaration the validator falls back to (1, 1), which
    is wrong for source ops (0, 0) and multi-input ops like unite.
    """
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "INPUT_ARITY")]
    assert not missing, (
        f"ops missing INPUT_ARITY declaration: {missing}. "
        "Declare INPUT_ARITY = (min_inputs, max_inputs) where max_inputs "
        "is int or None (None means unlimited)."
    )


def test_input_arity_is_well_formed():
    """INPUT_ARITY must be a 2-tuple of (int, int|None)."""
    bad = []
    for kind, op in OPS.items():
        arity = getattr(op, "INPUT_ARITY", None)
        if arity is None:
            continue
        if not isinstance(arity, tuple) or len(arity) != 2:
            bad.append((kind, arity, "not a 2-tuple"))
            continue
        min_in, max_in = arity
        if not isinstance(min_in, int) or isinstance(min_in, bool):
            bad.append((kind, arity, "min_inputs must be int"))
        if max_in is not None and (not isinstance(max_in, int) or isinstance(max_in, bool)):
            bad.append((kind, arity, "max_inputs must be int or None"))
    assert not bad, (
        f"ops with malformed INPUT_ARITY (expected (int, int|None)): {bad}"
    )


def test_every_op_declares_output_kind():
    """Every op must declare OUTPUT_KIND so edge and sink validation have a contract.

    Without an explicit declaration the validator falls back to 'stream', which
    is wrong for sink ops (target.*) and split ops (if_router).
    """
    missing = [kind for kind, op in OPS.items() if not hasattr(op, "OUTPUT_KIND")]
    assert not missing, (
        f"ops missing OUTPUT_KIND declaration: {missing}. "
        f"Declare OUTPUT_KIND = one of {sorted(VALID_OUTPUT_KINDS)}."
    )


def test_output_kind_is_valid():
    """OUTPUT_KIND must be one of the known valid values."""
    invalid = [
        (kind, getattr(op, "OUTPUT_KIND"))
        for kind, op in OPS.items()
        if hasattr(op, "OUTPUT_KIND") and getattr(op, "OUTPUT_KIND") not in VALID_OUTPUT_KINDS
    ]
    assert not invalid, (
        f"ops with invalid OUTPUT_KIND: {invalid}. "
        f"Must be one of {sorted(VALID_OUTPUT_KINDS)}."
    )


def test_split_ops_declare_output_ports():
    """Ops with OUTPUT_KIND='split' must declare OUTPUT_PORTS as a non-empty tuple.

    The edge validator uses OUTPUT_PORTS to check port names in 'from': 'node.port'
    notation. A split op without OUTPUT_PORTS makes port errors opaque.
    """
    bad = []
    for kind, op in OPS.items():
        if getattr(op, "OUTPUT_KIND", None) == "split":
            ports = getattr(op, "OUTPUT_PORTS", None)
            if not ports or not isinstance(ports, (tuple, list)) or len(ports) == 0:
                bad.append(kind)
    assert not bad, (
        f"split ops missing OUTPUT_PORTS: {bad}. "
        "Declare OUTPUT_PORTS = ('port_name_1', 'port_name_2') on the op module."
    )


@pytest.mark.parametrize("kind,expected_arity,expected_output_kind", [
    # Frozen baseline for release-path ops. Updates here are intentional.
    # Source ops produce output; they take no inputs.
    ("source.file",     (0, 0), "stream"),
    ("source.s3",       (0, 0), "stream"),
    ("source.gcs",      (0, 0), "stream"),
    ("source.sftp",     (0, 0), "stream"),
    # Transform ops: single-input, single-output.
    ("mask",            (1, 1), "stream"),
    ("filter",          (1, 1), "stream"),
    ("sort",            (1, 1), "stream"),
    ("dedupe",          (1, 1), "stream"),
    ("derive",          (1, 1), "stream"),
    ("drop_column",     (1, 1), "stream"),
    ("select_column",   (1, 1), "stream"),
    ("limit",           (1, 1), "stream"),
    ("convert.file_type", (1, 1), "stream"),
    # Multi-input transform.
    ("unite",           (2, None), "stream"),
    # Routing split op.
    ("if",              (1, 1), "split"),
    # Sink ops: consume input, produce no downstream.
    ("target.file",     (1, 1), "sink"),
    ("target.s3",       (1, 1), "sink"),
    ("target.gcs",      (1, 1), "sink"),
    ("target.sftp",     (1, 1), "sink"),
])
def test_release_op_arity_and_output_kind_baseline(kind, expected_arity, expected_output_kind):
    """Frozen baseline for release-path op INPUT_ARITY and OUTPUT_KIND.

    A surprise change in this table = an undocumented contract change.
    Update intentionally, not by suppressing the failure.
    """
    op = OPS[kind]
    assert getattr(op, "INPUT_ARITY") == expected_arity, (
        f"{kind} INPUT_ARITY: expected {expected_arity}, "
        f"got {getattr(op, 'INPUT_ARITY', 'MISSING')}"
    )
    assert getattr(op, "OUTPUT_KIND") == expected_output_kind, (
        f"{kind} OUTPUT_KIND: expected {expected_output_kind!r}, "
        f"got {getattr(op, 'OUTPUT_KIND', 'MISSING')!r}"
    )
