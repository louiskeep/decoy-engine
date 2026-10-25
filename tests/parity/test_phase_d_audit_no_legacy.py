"""Phase D / D-audit: the no-legacy COMPUTE-backend audit.

The matrix (`_d_audit_matrix.derive_matrix`) is derived from the admission predicates, not from
observed dispositions. This file proves each admitted cell ran on its classified compute, using
consumed-output witnesses (a marking kernel whose output is distinguishable from the legacy
result), never a disposition alone. Each witness is paired with a concrete mutant that discards
the marked result and recomputes the legacy value; the witness must fail on it.

Scope is the compute backend. The shared `_dispatch_mask_node` orchestration, the full-frame
oracle and the compiled kernels are not touched. Native-chunked FK cells and the full-frame
oracle are asserted EXCLUDED; no positive witness is fabricated for them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._sequential_native import _KEY_STRATEGIES, _OPS, SeqNativeRun
from decoy_engine.execution._sequential_native_ops import FAKER
from decoy_engine.execution._strategies import _code_set, _hash, _redact, _truncate
from decoy_engine.execution._strategies import _text_mask as text_mask_mod
from decoy_engine.execution._strategies import _text_redact as text_redact_mod
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution.native import (
    _kernels_scalar,
    _text_mask_native_splice,
    _text_redact_kernel,
)
from decoy_engine.execution.native._dispatch import _static_route_decision
from decoy_engine.execution.out_of_core import _mask_group_c
from decoy_engine.relationships._graph import OrphanPolicy
from decoy_engine.storm import detectors as detectors_mod
from decoy_engine.storm import ner as ner_mod
from tests.native._c6b_i_support import CORPUS
from tests.native._chunked_faker_support import oracle_pool_for
from tests.parity import _d2b_shapes as s
from tests.parity import _d_audit_matrix as mx
from tests.parity import _seq_witness as hw
from tests.parity._d1a_hash_data import JOB_SEED, chain_sources, column_seed, run_chain
from tests.parity._d1b1_bucket_data import bucket_seed
from tests.parity._d1b1_bucket_data import install_index_companion as install_bucket_ooc
from tests.parity._d1b2_categorical_data import categorical_seed
from tests.parity._d1b2_categorical_data import install_index_companion as install_cat_ooc
from tests.parity._d2b_shapes import Shape, make_plan, run_full, run_seq, strs, table_seed
from tests.parity._d2c_bucket_data import (
    BucketMarkingKernel,
    expect_last_offset,
    install_bucket,
)
from tests.parity._d2c_bucket_data import bucket_payload_columns as _bucket_cols
from tests.parity._d2c_cat_data import (
    CatMarkingKernel,
    expect_first_category,
    install_cat,
    payload_shape,
)
from tests.parity._d2c_cat_data import cat_seed as seq_cat_seed
from tests.parity._d2c_date_shift_data import (
    PARSEABLE,
    DateShiftMarkingKernel,
    date_shift_seed,
    expect_shift,
    install_date_shift,
)
from tests.parity._d2c_fpe_data import DIGITS, FpeMarkingKernel, fpe_seed, install_fpe
from tests.parity._d2c_fpe_data import MARK as FPE_MARK
from tests.parity._faker_seq_data import SRC as FAKER_SRC
from tests.parity._faker_seq_data import assert_python_ran, faker_seed, positional_seed
from tests.parity._kernel_witness import ExpectedMask, KernelWitness, force_companion_unavailable
from tests.parity._text_redact_seq_data import kernel_loads, tr_seed
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

_MODES = ["resident", "sink-batch", "sink-reorder"]
MARKED = "native:"


# ---------------------------------------------------------------------------
# A. The derived matrix: exact, so any admission change must be classified here.
# ---------------------------------------------------------------------------

_SEQ_NATIVE_PAYLOAD = {
    "hash": mx.COMPILED_RUST,
    "fpe": mx.COMPILED_RUST,
    "categorical": mx.COMPILED_RUST,
    "bucket_perturb": mx.COMPILED_RUST,
    "date_shift": mx.COMPILED_RUST,
    "faker": mx.COMPILED_SELECTION,
    "text_redact": mx.COMPILED_RUST,
    "text_mask": mx.COMPILED_RUST,
    "redact": mx.NAMED_ARROW,
    "truncate": mx.NAMED_ARROW,
    "passthrough": mx.NOOP,
}


def test_seq_payload_cells_match_the_admission_tables() -> None:
    matrix = mx.derive_matrix()
    admitted = {k[1]: v for k, v in matrix.items() if k[0] == mx.SEQ and k[2] == mx.PAYLOAD}
    for strategy, cls in _SEQ_NATIVE_PAYLOAD.items():
        assert admitted[strategy] == cls, strategy
    # Everything else on SEQ is a shared dedicated handler with no native admission anywhere.
    rest = {k for k, v in admitted.items() if k not in _SEQ_NATIVE_PAYLOAD}
    assert {admitted[k] for k in rest} == {mx.DEDICATED_PYTHON}
    assert set(_OPS) <= set(_SEQ_NATIVE_PAYLOAD)  # a new SEQ op must be classified above


def test_seq_key_cells_only_hash_and_fpe_are_compiled() -> None:
    matrix = mx.derive_matrix()
    compiled_keys = {
        k[1]
        for k, v in matrix.items()
        if k[:1] == (mx.SEQ,) and k[2] == mx.KEY and v == mx.COMPILED_RUST
    }
    assert compiled_keys == set(_KEY_STRATEGIES) == {"hash", "fpe"}
    for strategy in set(_OPS) - set(_KEY_STRATEGIES):
        assert matrix[(mx.SEQ, strategy, mx.KEY)] == mx.DEDICATED_PYTHON, strategy
    for strategy in ("redact", "truncate", "passthrough"):
        assert matrix[(mx.SEQ, strategy, mx.KEY)] == matrix[(mx.SEQ, strategy, mx.PAYLOAD)]


def test_ooc_cells_match_the_support_sets() -> None:
    matrix = mx.derive_matrix()
    ooc = {(k[1], k[2]): v for k, v in matrix.items() if k[0] == mx.OOC and v != mx.EXCLUDED}
    assert ooc == {
        ("hash", mx.PAYLOAD): mx.COMPILED_RUST,
        ("hash", mx.KEY): mx.COMPILED_RUST,
        ("redact", mx.PAYLOAD): mx.NAMED_ARROW,
        ("redact", mx.KEY): mx.NAMED_ARROW,
        ("truncate", mx.PAYLOAD): mx.NAMED_ARROW,
        ("truncate", mx.KEY): mx.NAMED_ARROW,
        ("passthrough", mx.PAYLOAD): mx.NOOP,
        ("passthrough", mx.KEY): mx.NOOP,
        ("fpe", mx.PAYLOAD): mx.COMPILED_RUST,
        ("categorical", mx.PAYLOAD): mx.COMPILED_RUST,
        ("text_redact", mx.PAYLOAD): mx.COMPILED_RUST,
        ("bucket_perturb", mx.PAYLOAD): mx.COMPILED_RUST,
        ("text_mask", mx.PAYLOAD): mx.DEDICATED_PYTHON,
        ("code_set", mx.PAYLOAD): mx.DEDICATED_PYTHON,
        # Row-error channel (bucketize/top_code) + row-offset channel (windowed_date):
        # the full-frame handler per batch via the oracle-handler bridges.
        ("bucketize", mx.PAYLOAD): mx.DEDICATED_PYTHON,
        ("top_code", mx.PAYLOAD): mx.DEDICATED_PYTHON,
        ("windowed_date", mx.PAYLOAD): mx.DEDICATED_PYTHON,
    }
    for strategy in ("faker", "date_shift", "group_key", "shuffle"):
        assert matrix[(mx.OOC, strategy, mx.PAYLOAD)] == mx.EXCLUDED, strategy


def test_every_ooc_admitted_strategy_is_inside_the_enumeration_domain() -> None:
    """`derive_matrix` enumerates `SCALAR_HANDLERS`; an OOC-admitted strategy outside it would be
    invisible to the matrix, so the live support set must be contained in that domain."""
    from decoy_engine.execution._strategies import SCALAR_HANDLERS
    from decoy_engine.execution.out_of_core import _compat

    assert set(SCALAR_HANDLERS) >= _compat._SUPPORTED_WORK_STRATEGIES


def test_native_chunked_admits_no_fk_cell_and_its_sets_are_the_registry() -> None:
    matrix = mx.derive_matrix()
    assert mx.kernel_sets_agree()
    assert not [
        c for c, v in matrix.items() if c[0] == mx.CHUNKED and c[2] == mx.KEY and v != mx.EXCLUDED
    ]
    assert matrix[(mx.CHUNKED, "faker", mx.PAYLOAD)] == mx.COMPILED_SELECTION
    assert matrix[(mx.CHUNKED, "code_set", mx.PAYLOAD)] == mx.EXCLUDED


@pytest.mark.parametrize("side", ["parent", "child"])
def test_native_chunked_reroutes_either_side_of_a_declared_fk_edge(side: str) -> None:
    config = {
        "relationships": [{"parent": {"table": "p"}, "children": [{"table": "c"}]}],
    }
    table = "p" if side == "parent" else "c"
    evidence = _static_route_decision(config, None, table=table, engine_version="0")
    assert evidence.native_admitted is False
    assert evidence.reroute_reason == "fk_relationship_not_native_route"
    assert all(r.route == "oracle" for r in evidence.node_routes)


def test_ooc_gate_excludes_the_deferred_strategies_with_a_reason() -> None:
    from decoy_engine.execution.out_of_core import _compat

    for strategy in ("faker", "date_shift"):
        assert strategy in _compat._DEFERRED_GROUP_B
    for strategy in ("geo_generalize", "formula", "derived", "nested"):
        assert strategy in _compat._DEFERRED_GROUP_C
    # bucketize/top_code (row-error channel) and windowed_date (row-offset channel)
    # are now ADMITTED, so they are no longer deferred misses.
    assert "bucketize" not in _compat._DEFERRED_GROUP_B
    assert "bucketize" in _compat._ROW_ERROR_BRIDGE_STRATEGIES
    assert "top_code" in _compat._ROW_ERROR_BRIDGE_STRATEGIES
    assert "windowed_date" in _compat._ROW_OFFSET_BRIDGE_STRATEGIES


# ---------------------------------------------------------------------------
# B. SEQ compiled operators: marking-kernel consumed-output witnesses + mutants.
# ---------------------------------------------------------------------------


class Cell:
    """One SEQ audit cell: a shape, the column under test, and the marked expectation."""

    def __init__(self, strategy: str, shape: Shape, column: str, expected: list[Any]) -> None:
        self.strategy, self.shape, self.column, self.expected = strategy, shape, column, expected
        self.kernel: Any = None
        self.python_hash_rows: list[int] | None = None
        # The full-frame oracle's value, taken before any stand-in or spy is installed.
        self.legacy = run_full(shape).outputs["P"].column(column).to_pylist()


def _hash_cell(mp: pytest.MonkeyPatch) -> Cell:
    seed = column_seed("hash", namespace="ns_hp")
    shape = payload_shape({"c": (strs(FAKER_SRC), seed)})
    expected = hw.marked(run_full(shape).outputs["P"].column("c").to_pylist())
    cell = Cell("hash", shape, "c", expected)
    cell.kernel = hw.install(mp, hw.MarkingKernel())
    cell.python_hash_rows = hw.spy_python_hash(mp)
    return cell


def _fpe_cell(mp: pytest.MonkeyPatch) -> Cell:
    shape = payload_shape({"c": (strs(DIGITS), fpe_seed())})
    oracle = run_full(shape).outputs["P"].column("c").to_pylist()
    expected = [v if v in (None, "") else FPE_MARK + v for v in oracle]
    cell = Cell("fpe", shape, "c", expected)
    cell.kernel = install_fpe(mp, FpeMarkingKernel())
    return cell


def _categorical_cell(mp: pytest.MonkeyPatch) -> Cell:
    shape = payload_shape({"c": (strs(FAKER_SRC), seq_cat_seed())})
    expected = expect_first_category(run_full(shape).outputs["P"].column("c").to_pylist())
    cell = Cell("categorical", shape, "c", expected)
    cell.kernel = install_cat(mp, CatMarkingKernel())
    return cell


def _bucket_cell(mp: pytest.MonkeyPatch) -> Cell:
    source, seed = _bucket_cols()["b_month"]
    shape = payload_shape({"c": (source, seed)})
    expected = expect_last_offset(source.to_pylist(), "month", "%Y-%m-%d")
    cell = Cell("bucket_perturb", shape, "c", expected)
    cell.kernel = install_bucket(mp, BucketMarkingKernel())
    return cell


def _date_shift_cell(mp: pytest.MonkeyPatch) -> Cell:
    source = pa.array(PARSEABLE, pa.string())
    shape = payload_shape({"c": (source, date_shift_seed(-30, 30))})
    cell = Cell("date_shift", shape, "c", expect_shift(PARSEABLE, 30, "%Y-%m-%d"))
    cell.kernel = install_date_shift(mp, DateShiftMarkingKernel())
    return cell


def _faker_cell(mp: pytest.MonkeyPatch) -> Cell:
    seed = faker_seed()
    first = str(oracle_pool_for(seed, JOB_SEED).values[0])
    shape = payload_shape({"c": (strs(FAKER_SRC), seed)})
    expected = [None if v is None else first for v in FAKER_SRC]
    cell = Cell(FAKER, shape, "c", expected)
    cell.kernel = install_cat(mp, CatMarkingKernel())
    return cell


def _faker_positional_cell(mp: pytest.MonkeyPatch) -> Cell:
    seed = positional_seed()
    first = str(oracle_pool_for(seed, JOB_SEED).values[0])
    shape = payload_shape({"c": (strs(FAKER_SRC), seed)})
    expected = [None if v is None else first for v in FAKER_SRC]
    cell = Cell(FAKER, shape, "c", expected)
    cell.kernel = install_cat(mp, CatMarkingKernel())
    return cell


_COMPILED_CELLS: dict[str, Callable[[pytest.MonkeyPatch], Cell]] = {
    "hash": _hash_cell,
    "fpe": _fpe_cell,
    "categorical": _categorical_cell,
    "bucket_perturb": _bucket_cell,
    "date_shift": _date_shift_cell,
    "faker_deterministic": _faker_cell,
    "faker_positional": _faker_positional_cell,
}


def _entry(result: Any, strategy: str, column: str) -> dict[str, Any]:
    entries = result.quality_metrics[_OPS[strategy].disposition_key]
    [entry] = [e for e in entries if e["column"] == column and e["path"] == "scalar"]
    return entry


def _assert_consumed(cell: Cell, result: Any) -> None:
    """The emitted column IS the kernel's marked output, and the legacy branch never ran."""
    got = result.outputs["P"].column(cell.column).to_pylist()
    assert got == cell.expected, cell.strategy
    assert cell.expected != cell.legacy, "vacuous: the marked output equals the legacy output"
    assert any(v is not None for v in cell.expected), "inputs must be non-null and maskable"
    assert cell.kernel.calls, "the stand-in kernel was never called"
    entry = _entry(result, cell.strategy, cell.column)
    assert entry["executed_backend"] == "rust_companion", entry
    if cell.python_hash_rows is not None:
        assert cell.python_hash_rows == [], "the Python hash handler ran for an admitted cell"


class _DiscardRecompute:
    """TEST-ONLY handler: runs the real handler (the marked kernel is called), DISCARDS its
    result, then assigns an independently recomputed legacy value for the column."""

    def __init__(self, real: Any, recompute: Callable[[pd.DataFrame, str], list[Any]]) -> None:
        self.real, self.recompute, self.name = real, recompute, real.name

    def run(self, df: pd.DataFrame, column: str, plan: Any, ctx: Any) -> Any:
        df, warnings = self.real.run(df, column, plan, ctx)
        df[column] = self.recompute(df, column)
        return df, warnings


@pytest.mark.parametrize("name", list(_COMPILED_CELLS))
def test_seq_compiled_cell_is_consumed_and_the_discard_mutant_fails(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell = _COMPILED_CELLS[name](monkeypatch)
    _assert_consumed(cell, run_seq(cell.shape))

    legacy = cell.legacy
    kernel_calls = len(cell.kernel.calls)
    adapter = PandasExecutionAdapter()
    adapter._handlers[cell.strategy] = _DiscardRecompute(
        adapter._handlers[cell.strategy], lambda df, column: legacy
    )
    mutant = run_seq(cell.shape, adapter=adapter)
    assert len(cell.kernel.calls) > kernel_calls  # the kernel ran, then its result was dropped
    with pytest.raises(AssertionError):
        _assert_consumed(cell, mutant)


def test_hash_mutant_that_recomputes_in_python_trips_the_zero_fallback_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cell = _hash_cell(monkeypatch)

    def recompute(df: pd.DataFrame, column: str) -> list[Any]:
        return _hash.hash_array(
            pa.array(FAKER_SRC, pa.string()), seed=JOB_SEED, namespace="ns_hp"
        ).to_pylist()

    adapter = PandasExecutionAdapter()
    adapter._handlers["hash"] = _DiscardRecompute(adapter._handlers["hash"], recompute)
    mutant = run_seq(cell.shape, adapter=adapter)
    assert cell.python_hash_rows, "the spy must see the Python hash recompute"
    with pytest.raises(AssertionError):
        _assert_consumed(cell, mutant)


# Text operators: the span detection is compiled Rust and the per-cell splice is Python. The
# witness marks the output AT THE CANDIDATE BOUNDARY, not the seam: a test-only wrapper injects a
# compiled span over a no-PII sentinel cell, and that injected span propagates through the Python
# splice into the emitted string ONLY when the compiled candidates are actually consumed. A mutant
# that discards the candidates (returning `[None]` per cell) forces a Python recompute that detects
# no PII in the sentinel, so the mark is absent; every Python span-detection entry point reachable
# for an eligible cell is also guarded, so the recompute raises rather than silently re-deriving.

needs_text_kernel = pytest.mark.skipif(not kernel_loads(), reason="needs the text span kernel")

# An ASCII, PII-free cell. Its only span is the one the candidate-boundary wrapper injects, so it is
# redacted ONLY on the compiled path; a Python recompute finds nothing and leaves it unchanged.
_SENTINEL = "zzmarkzz"
# One compiled candidate `(detector_index, start, end)` over the whole sentinel cell. Index 0 is
# `email` in `SUPPORTED_IDS`; `email` is requested by both witness configs (detectors=all) and its
# text_mask default strategy is `redact`, so the span visibly rewrites the cell on either operator.
_SENTINEL_SPAN = [(0, 0, len(_SENTINEL))]


def _text_cell(strategy: str, source: list[str | None], seed: Any) -> Shape:
    return payload_shape({"t": (strs(source), seed)})


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("the handler's pandas/Python per-cell loop ran for an admitted cell")


def _boom_eligible(real: Callable[..., Any]) -> Callable[..., Any]:
    """Boom when Python span detection runs for an ASCII-ELIGIBLE cell, which must use the compiled
    candidates. A non-ASCII cell legitimately detects in Python even on the compiled path, so it is
    passed through to the real detector."""

    def guard(text: Any, *a: Any, **k: Any) -> Any:
        if isinstance(text, str) and _text_redact_kernel.is_ascii_safe(text):
            raise AssertionError("Python span detection ran for an ASCII-eligible cell")
        return real(text, *a, **k)

    return guard


def _guard_python_detection(m: pytest.MonkeyPatch) -> None:
    """Boom every Python span-detection / recompute entry point the native text splice can reach
    for an ELIGIBLE cell when the compiled candidates are discarded: the kernel's `iter_spans`
    (text_redact and the text_mask non-splice path), the splice route's `iter_spans`, and both
    handlers' own per-cell loops. `merge_text_redact_spans`/`finditer` are NOT guarded: they run on
    the compiled path too (the three lookaround detectors), so they are not a recompute tell."""
    m.setattr(_kernels_scalar, "iter_spans", _boom_eligible(_kernels_scalar.iter_spans))
    m.setattr(
        _text_mask_native_splice, "iter_spans", _boom_eligible(_text_mask_native_splice.iter_spans)
    )
    m.setattr(text_redact_mod, "iter_spans", _boom)  # text_redact handler's own per-cell loop
    m.setattr(text_mask_mod, "mask_cell", _boom)  # text_mask handler's own per-cell loop


@needs_text_kernel
@pytest.mark.parametrize(
    ("strategy", "source", "seed"),
    [
        (
            "text_redact",
            ["mail a@b.com now", "ssn 123-45-6789 ok", "call 212-555-1234", "clean", None],
            tr_seed(),
        ),
        (
            # `unmatched_span_policy=passthrough` keeps non-span text verbatim (default `redact`
            # would rewrite the whole no-PII sentinel to the token, erasing the distinction). The
            # config stays admitted-native and, with its default fpe/faker span strategies, still
            # routes through `text_redact_candidates` (the non-splice path).
            "text_mask",
            list(CORPUS),
            column_seed(
                "text_mask",
                namespace=None,
                provider_config=(("unmatched_span_policy", "passthrough"),),
            ),
        ),
    ],
)
def test_seq_text_operator_runs_compiled_spans_and_never_the_handler_loop(
    strategy: str,
    source: list[str | None],
    seed: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mark originates at the compiled-candidate boundary: an injected span over a no-PII
    sentinel reaches the emitted string ONLY when the candidates are consumed. Discarding them and
    recomputing in Python yields output WITHOUT the mark (and, with detection guarded, raises)."""
    shape = _text_cell(strategy, [*source, _SENTINEL], seed)
    oracle = run_full(shape).outputs["P"].column("t").to_pylist()
    assert oracle[-1] == _SENTINEL, (
        "the sentinel carries no PII: the oracle must leave it untouched"
    )

    calls: list[int] = []
    original = _text_redact_kernel._CompiledTextRedactKernel.text_redact_candidates

    def injecting(self: Any, values: Any, detector_ids: Any, catalog_version: int) -> Any:
        calls.append(len(values))
        per_cell = list(original(self, values, detector_ids, catalog_version))
        for i, v in enumerate(values):
            if v == _SENTINEL:  # the mark: a compiled email span over the whole sentinel cell
                per_cell[i] = list(_SENTINEL_SPAN)
        return per_cell

    def discard(self: Any, values: Any, detector_ids: Any, catalog_version: int) -> Any:
        calls.append(len(values))
        original(self, values, detector_ids, catalog_version)  # real call, result DISCARDED
        return [None] * len(values)

    kernel_cls = _text_redact_kernel._CompiledTextRedactKernel

    # Positive: the injected compiled span reaches the emitted string; every real cell is the
    # oracle value, and no Python recompute of an eligible cell runs.
    with monkeypatch.context() as m:
        m.setattr(kernel_cls, "text_redact_candidates", injecting)
        _guard_python_detection(m)
        result = run_seq(shape)
    got = result.outputs["P"].column("t").to_pylist()
    assert got[:-1] == oracle[:-1], "a real cell diverged from the oracle on the compiled path"
    assert got[-1] not in (None, _SENTINEL), "the candidate-boundary mark never reached the string"
    assert calls, "the compiled span kernel was never called"
    entry = _entry(result, strategy, "t")
    assert entry["executed_backend"] == "rust_companion" and entry["native_calls"] >= 1

    # Mutant (Codex round 2), guarded: discarding the candidates forces a Python recompute of the
    # eligible cells, which every detection guard rejects. The mutant is REQUIRED to raise.
    with monkeypatch.context() as m:
        m.setattr(kernel_cls, "text_redact_candidates", discard)
        _guard_python_detection(m)
        with pytest.raises(AssertionError):
            run_seq(shape)

    # Same mutant, UNGUARDED: the recompute now succeeds, reproducing the oracle byte for byte, but
    # WITHOUT the candidate-boundary mark. This is what the old seam-level mark could not catch.
    before = len(calls)
    with monkeypatch.context() as m:
        m.setattr(kernel_cls, "text_redact_candidates", discard)
        recompute = run_seq(shape).outputs["P"].column("t").to_pylist()
    assert len(calls) > before, "the compiled kernel ran, then its result was dropped"
    assert recompute == oracle, "the Python recompute must reproduce the oracle"
    assert recompute[-1] == _SENTINEL, (
        "the mark is absent once the compiled candidates are discarded"
    )


# ---------------------------------------------------------------------------
# C. SEQ named-Arrow (redact, truncate) and no-op (passthrough): the harness gap.
# ---------------------------------------------------------------------------

_REDACT_SRC = ["alice", "bob", None, "carol", "", "dave"]
_PT_SRC = ["keep1", None, "keep2", "", "keep3", "keep1"]


def _marked_arrow(real: Callable[..., pa.Array], log: list[int]) -> Callable[..., pa.Array]:
    def marked(values: Any, **kwargs: Any) -> pa.Array:
        log.append(len(values))
        out = real(values, **kwargs)
        return pa.array([None if v is None else MARKED + v for v in out.to_pylist()], pa.string())

    return marked


def _named_arrow_shape() -> Shape:
    return payload_shape(
        {
            "r": (
                strs(_REDACT_SRC),
                column_seed("redact", namespace=None, provider_config=(("redact_with", "XXXX"),)),
            ),
            "tr": (
                strs(_REDACT_SRC),
                column_seed("truncate", namespace=None, provider_config=(("length", 3),)),
            ),
        }
    )


def _expected_named_arrow() -> dict[str, list[Any]]:
    return {
        "r": [None if v is None else MARKED + "XXXX" for v in _REDACT_SRC],
        "tr": [None if v is None else MARKED + v[:3] for v in _REDACT_SRC],
    }


def _assert_named_arrow_consumed(out: pa.Table, columns: tuple[str, ...] = ("r", "tr")) -> None:
    expected = _expected_named_arrow()
    for column in columns:
        assert out.column(column).to_pylist() == expected[column], column


def test_seq_redact_and_truncate_consume_the_named_arrow_kernels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redact_log: list[int] = []
    truncate_log: list[int] = []
    monkeypatch.setattr(_redact, "redact_array", _marked_arrow(_redact.redact_array, redact_log))
    monkeypatch.setattr(
        _truncate, "truncate_array", _marked_arrow(_truncate.truncate_array, truncate_log)
    )
    out = run_seq(_named_arrow_shape()).outputs["P"]
    _assert_named_arrow_consumed(out)  # marked values only come from the kernel result
    assert redact_log == [len(_REDACT_SRC)] and truncate_log == [len(_REDACT_SRC)]


def test_truncate_mutant_that_discards_the_kernel_result_fails_the_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TruncateHandler has no pandas fallback to force, so the mutant is a replacement handler
    that calls the marked kernel, drops its result and assigns independently sliced values."""
    log: list[int] = []
    marked = _marked_arrow(_truncate.truncate_array, log)
    monkeypatch.setattr(_truncate, "truncate_array", marked)

    class Mutant:
        name = "truncate"

        def run(self, df: pd.DataFrame, column: str, plan: Any, ctx: Any) -> Any:
            marked(
                pa.array(df[column].tolist(), pa.string()), length=3, keep="head", mask_char=None
            )
            df[column] = [None if v is None else v[:3] for v in df[column].tolist()]
            return df, []

    adapter = PandasExecutionAdapter()
    adapter._handlers["truncate"] = Mutant()
    # Only truncate is marked here, so the witness targets the truncate column alone. The
    # identical setup with the REAL handler must pass it; only then does a failure mean the
    # mutant discarded the kernel result.
    control = run_seq(_named_arrow_shape()).outputs["P"]
    assert log, "the marked kernel must be called by the unmutated handler"
    _assert_named_arrow_consumed(control, ("tr",))
    log.clear()
    mutant = run_seq(_named_arrow_shape(), adapter=adapter).outputs["P"]
    assert log, "the marked kernel must have been called before its result was discarded"
    with pytest.raises(AssertionError, match="tr"):
        _assert_named_arrow_consumed(mutant, ("tr",))


def test_redact_mutant_that_recomputes_through_the_pandas_branch_fails_the_witness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[int] = []
    marked = _marked_arrow(_redact.redact_array, log)
    monkeypatch.setattr(_redact, "redact_array", marked)

    class Mutant:
        name = "redact"

        def run(self, df: pd.DataFrame, column: str, plan: Any, ctx: Any) -> Any:
            marked(pa.array(df[column].tolist(), pa.string()), redact_with="XXXX")
            col = df[column]
            df[column] = col.where(col.isna(), "XXXX")  # the handler's legacy fallback body
            return df, []

    adapter = PandasExecutionAdapter()
    adapter._handlers["redact"] = Mutant()
    control = run_seq(_named_arrow_shape()).outputs["P"]  # real handler, same marked kernel
    assert log
    _assert_named_arrow_consumed(control, ("r",))
    log.clear()
    mutant = run_seq(_named_arrow_shape(), adapter=adapter).outputs["P"]
    assert log
    with pytest.raises(AssertionError, match="r"):
        _assert_named_arrow_consumed(mutant, ("r",))


class _NoopSpy:
    """Wraps the real no-op handler and records invocations and the values it left behind."""

    name = "passthrough"

    def __init__(self) -> None:
        self.real = PassthroughHandler()
        self.invocations = 0

    def run(self, df: pd.DataFrame, column: str, plan: Any, ctx: Any) -> Any:
        self.invocations += 1
        return self.real.run(df, column, plan, ctx)


class _LegacyReplacement:
    """The instrumented injected replacement: a pandas copy of the column. Zero calls expected."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, df: pd.DataFrame, column: str) -> None:
        self.calls += 1
        df[column] = df[column].copy()


def _passthrough_shape() -> Shape:
    seed = column_seed("passthrough", namespace=None)
    return payload_shape({"p": (strs(_PT_SRC), seed)})


def _assert_noop(result: Any, spy: _NoopSpy, legacy: _LegacyReplacement) -> None:
    assert spy.invocations >= 1, "the real no-op was never invoked"
    assert legacy.calls == 0, "a legacy replacement ran for an admitted passthrough cell"
    assert result.outputs["P"].column("p").to_pylist() == _PT_SRC  # unchanged, non-null present


def test_seq_passthrough_is_a_verified_noop_and_the_bypass_mutants_fail() -> None:
    spy, legacy = _NoopSpy(), _LegacyReplacement()
    adapter = PandasExecutionAdapter()
    adapter._handlers["passthrough"] = spy
    _assert_noop(run_seq(_passthrough_shape(), adapter=adapter), spy, legacy)

    # Mutant 1: invokes the real no-op AND THEN the instrumented replacement.
    spy2, legacy2 = _NoopSpy(), _LegacyReplacement()

    class AndThenLegacy:
        name = "passthrough"

        def run(self, df: pd.DataFrame, column: str, plan: Any, ctx: Any) -> Any:
            out = spy2.run(df, column, plan, ctx)
            legacy2(df, column)
            return out

    adapter2 = PandasExecutionAdapter()
    adapter2._handlers["passthrough"] = AndThenLegacy()
    result = run_seq(_passthrough_shape(), adapter=adapter2)
    with pytest.raises(AssertionError, match="legacy replacement ran"):
        _assert_noop(result, spy2, legacy2)

    # Mutant 2: bypass-only (the real no-op is never invoked) fails the invocation check.
    spy3, legacy3 = _NoopSpy(), _LegacyReplacement()
    adapter3 = PandasExecutionAdapter()
    adapter3._handlers["passthrough"] = SimpleNamespace(
        name="passthrough", run=lambda df, column, plan, ctx: (df, [])
    )
    result = run_seq(_passthrough_shape(), adapter=adapter3)
    with pytest.raises(AssertionError, match="never invoked"):
        _assert_noop(result, spy3, legacy3)


# ---------------------------------------------------------------------------
# D. SEQ dedicated-Python cells and the key rule.
# ---------------------------------------------------------------------------


def test_seq_code_set_runs_its_dedicated_handler_and_has_no_kernel_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = column_seed(
        "code_set", namespace=None, provider_config=(("code_set", "icd10"), ("mode", "mask"))
    )
    source = ["I10", "E11.9", None, "F32.9"]
    shape = Shape(
        make_plan({"t": table_seed({"diag": seed})}),
        s.graph(),
        {"t": pa.table({"diag": strs(source)})},
    )
    seen: list[str] = []
    real = _code_set.apply_code_set

    def spy(value: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(value)
        return real(value, *args, **kwargs)

    monkeypatch.setattr(_code_set, "apply_code_set", spy)
    out = run_seq(shape).outputs["t"].column("diag").to_pylist()
    assert seen == ["I10", "E11.9", "F32.9"]
    assert out[2] is None and all(o != i for o, i in zip(out, source, strict=True) if i)
    assert "code_set" not in _OPS and "code_set" not in mx.NATIVE_KERNEL_STRATEGIES


@needs_text_kernel
def test_seq_ner_text_redact_declines_to_the_python_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Companion-present assertion: the NER decline reason `text_redact_ner_not_native:t`
    # is only reached when the compiled span kernel loads. Without the companion the
    # admission path declines earlier with `crypto_extension_unavailable` (still Python,
    # still a decline, but a different reason), so this companion-specific reason is gated.
    def fake(texts: list[str], *, model: str, entities: Any) -> list[list[Any]]:
        return [[detectors_mod.Span("person_name", 0, min(3, len(t)), t[:3])] for t in texts]

    monkeypatch.setattr(ner_mod, "iter_ner_spans_batch", fake)
    shape = payload_shape({"t": (strs(["alice smith", "bob", None]), tr_seed(ner=True))})
    result = run_seq(shape)
    entry = _entry(result, "text_redact", "t")
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == "text_redact_ner_not_native:t"
    assert result.outputs["P"].column("t").to_pylist()[0].startswith("[REDACTED]")  # NER ran


def test_seq_faker_declined_config_runs_the_python_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    seed = faker_seed(provider="person_email")  # provider outside the native allowlist
    shape = payload_shape({"f": (strs(FAKER_SRC), seed)})
    result = run_seq(shape)
    assert_python_ran(result, "f", "faker_provider_not_native:f:person_email")
    assert [c for c in kernel.calls if c.namespace == "ns_f"] == []  # the compiled kernel idle
    assert result.outputs["P"].equals(run_full(shape).outputs["P"])


def _key_probe(strategy: str) -> list[dict[str, Any]]:
    """Which decision the SEQ route records for `strategy` configured on an FK parent+child key."""
    seed = column_seed(strategy, namespace="ns_k")
    plan = make_plan(
        {"A": table_seed({"k": seed}), "B": table_seed({"k": seed})},
    )
    graph = s.graph(s.edge("A", "k", "B", "k", "ns_k", OrphanPolicy.PRESERVE))
    run = SeqNativeRun()
    for table in ("A", "B"):
        run.prepare_table(plan, table, pa.schema([("k", pa.string())]), graph)
    return run.metrics().get(_OPS[strategy].disposition_key, [])


@pytest.mark.parametrize("strategy", sorted(_OPS))
def test_seq_fk_key_rule_hash_and_fpe_decide_everything_else_declines(strategy: str) -> None:
    entries = _key_probe(strategy)
    declined = [e for e in entries if e["path"] == "fk_key_declined"]
    if strategy in _KEY_STRATEGIES:
        assert declined == []
    else:
        assert {(e["table"], e["column"]) for e in declined} == {("A", "k"), ("B", "k")}
        assert {e["executed_backend"] for e in declined} == {"python_scalar"}
        assert mx.derive_matrix()[(mx.SEQ, strategy, mx.KEY)] == mx.DEDICATED_PYTHON


# ---------------------------------------------------------------------------
# E. Out-of-core: named-Arrow / no-op consumed witnesses, compiled cells, and fences.
# ---------------------------------------------------------------------------

_OOC_SRC = pa.table(
    {
        "r": strs(["alice", "bob", None, "carol", "dave"]),
        "tr": strs(["alice", "bob", None, "carol", "dave"]),
        "pt": strs(["k1", "k2", None, "k4", "k5"]),
    }
)
_OOC_SEEDS = (
    ("r", column_seed("redact", namespace=None, provider_config=(("redact_with", "XXXX"),))),
    ("tr", column_seed("truncate", namespace=None, provider_config=(("length", 3),))),
    ("pt", column_seed("passthrough", namespace=None)),
)


def _install_ooc_marks(mp: pytest.MonkeyPatch, *, mutant: bool) -> dict[str, list[int]]:
    from decoy_engine.execution.out_of_core import _mask

    log: dict[str, list[int]] = {"redact": [], "truncate": [], "passthrough": []}
    for name, key in (("redact_array", "redact"), ("truncate_array", "truncate")):
        real = getattr(_mask, name)
        marked = _marked_arrow(real, log[key])

        def entry(values: Any, *, _marked: Any = marked, _real: Any = real, **kw: Any) -> pa.Array:
            out = _marked(values, **kw)
            return _real(values, **kw) if mutant else out  # mutant: discard, recompute legacy

        mp.setattr(_mask, name, entry)
    real_pt = _mask.passthrough_array

    def noop(values: Any) -> pa.Array:
        log["passthrough"].append(len(values))
        return real_pt(values)

    mp.setattr(_mask, "passthrough_array", noop)
    return log


def _assert_ooc_consumed(tables: dict[str, pa.Table], log: dict[str, list[int]]) -> None:
    out = tables["t"]
    assert out.column("r").to_pylist() == [
        None if v is None else MARKED + "XXXX" for v in _OOC_SRC.column("r").to_pylist()
    ]
    assert out.column("tr").to_pylist() == [
        None if v is None else MARKED + v[:3] for v in _OOC_SRC.column("tr").to_pylist()
    ]
    assert out.column("pt").to_pylist() == _OOC_SRC.column("pt").to_pylist()
    assert log["redact"] and log["truncate"] and log["passthrough"]


@pytest.mark.parametrize("mode", _MODES)
def test_ooc_redact_truncate_passthrough_are_consumed_and_the_discard_mutant_fails(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        log = _install_ooc_marks(m, mutant=False)
        run = run_chain(_OOC_SRC, _OOC_SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path / "ok"))
    _assert_ooc_consumed(run.tables, log)
    with monkeypatch.context() as m:
        log = _install_ooc_marks(m, mutant=True)
        mutant = run_chain(
            _OOC_SRC, _OOC_SEEDS, batch_rows=2, **mode_kwargs(mode, tmp_path / "bad")
        )
    with pytest.raises(AssertionError):
        _assert_ooc_consumed(mutant.tables, log)


_HASH_PAYLOAD = pa.table({"a": strs(["w", "x", None, "z", "q"])})


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("entrypoint", "seeds", "install"),
    [
        ("categorical_array", (("a", categorical_seed()),), install_cat_ooc),
        ("bucket_perturb_array", (("a", bucket_seed("week")),), install_bucket_ooc),
    ],
)
def test_ooc_compiled_cell_is_consumed_and_its_fallback_serves_only_the_degraded_cell(
    mode: str,
    entrypoint: str,
    seeds: Any,
    install: Callable[..., Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = (
        pa.table(
            {
                "a": pa.array(
                    ["2024-03-05", "2023-07-19", None, "2022-11-30", "2021-02-14"], pa.string()
                )
            }
        )
        if entrypoint == "bucket_perturb_array"
        else _HASH_PAYLOAD
    )
    expected = [ExpectedMask("t", "a", entrypoint)]
    sources = chain_sources(payload)

    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
    from decoy_engine.execution.out_of_core import _hash_run

    def hide_hash(*_a: Any, **_k: Any) -> None:
        raise CryptoExtensionUnavailableError("isolate the column under test")

    with monkeypatch.context() as m:
        install(m)
        m.setattr(_hash_run, "load_compiled_crypto_kernel", hide_hash)
        witness = KernelWitness()
        with witness.observe():
            run = run_chain(payload, seeds, batch_rows=2, **mode_kwargs(mode, tmp_path / "n"))
        witness.assert_native_ran(sources, run.tables, expected)
        with pytest.raises(AssertionError):
            witness.assert_legacy_path_only(sources, expected)
    # Fence: the pandas bridge / Python reference is the degraded path, reachable without the
    # companion only. The categorical Python reference and the bucket bridge are RETAINED.
    with monkeypatch.context() as m:
        force_companion_unavailable(m)
        witness = KernelWitness()
        with witness.observe():
            degraded = run_chain(payload, seeds, batch_rows=2, **mode_kwargs(mode, tmp_path / "d"))
        witness.assert_legacy_path_only(sources, expected)
        with pytest.raises(AssertionError):
            witness.assert_native_ran(sources, degraded.tables, expected)


def test_ooc_bucket_bridge_symbol_is_still_the_degraded_path() -> None:
    assert callable(_mask_group_c.apply_bucket_perturb)  # the retained bridge the fence targets


@pytest.mark.parametrize(
    ("strategy", "suite"),
    [
        ("hash", "tests/parity/test_phase_d_d1a_hash_witness.py"),
        ("fpe", "tests/parity/test_ooc_fpe_native_real.py"),
        ("text_redact", "tests/parity/test_ooc_text_redact_native_real.py"),
    ],
)
def test_ooc_compiled_cells_without_a_buffer_witness_cite_their_real_kernel_suite(
    strategy: str, suite: str
) -> None:
    assert mx.derive_matrix()[(mx.OOC, strategy, mx.PAYLOAD)] == mx.COMPILED_RUST
    assert (Path(__file__).resolve().parents[2] / suite).is_file()


# Suites that drive the native-chunked route (`run_mask_chunked`, the chunked kernel entry), one
# per compiled/selection cell. Each is checked below to import the chunked entry and NOT the
# out-of-core route.
_CHUNKED_SUITES = {
    "hash": "tests/native/test_chunked_entry_parity_matrix.py",
    "categorical": "tests/native/test_chunked_categorical_parity.py",
    "bucket_perturb": "tests/native/test_chunked_bucket_perturb_parity.py",
    "date_shift": "tests/native/test_chunked_date_shift_parity.py",
    "fpe": "tests/native/test_c6a_fpe_chunked.py",
    "group_key": "tests/native/test_chunked_group_key_parity.py",
    "faker": "tests/native/test_chunked_nondet_faker_parity.py",
    "text_mask": "tests/native/test_c6b_i_text_mask_chunked.py",
    "text_redact": "tests/native/test_c6c_i_text_redact_chunked.py",
}


def test_native_chunked_compiled_cells_cite_their_real_kernel_suite() -> None:
    """Native-chunked admits no FK cell (every FK edge reroutes to the oracle), so its non-FK
    compiled cells carry no fresh witness here; each cites a chunked-route suite. What a cited suite
    proves differs by cell (see the record): the scalar cells' suites witness compiled consumption
    (`rust_companion`/`rust_pool_select`, `compiled_kernel_executed` True), but the text suites only
    witness the `arrow_python` chunked route plus oracle parity, NOT compiled-span consumption (a
    Python span recompute would pass them) -- that witness is the SEQ/OOC suite, not a chunked one.
    This test only checks the cited set equals the derived compiled/selection set, so a new chunked
    compiled cell fails here until it cites a suite, and that each suite drives the chunked route."""
    matrix = mx.derive_matrix()
    derived = {
        k[1]
        for k, v in matrix.items()
        if k[0] == mx.CHUNKED
        and k[2] == mx.PAYLOAD
        and v in (mx.COMPILED_RUST, mx.COMPILED_SELECTION)
    }
    assert set(_CHUNKED_SUITES) == derived
    root = Path(__file__).resolve().parents[2]
    for strategy, suite in _CHUNKED_SUITES.items():
        text = (root / suite).read_text()
        assert "out_of_core" not in text, f"{suite} drives the out-of-core route"
        assert "run_mask_chunked" in text or "_chunked_entry" in text, suite
        assert strategy in text or strategy.replace("_", "") in text, suite


# ---------------------------------------------------------------------------
# F. The redact conversion-failure fallback is LIVE: the audit fences it, it does not delete it.
# ---------------------------------------------------------------------------


def _redact_seed() -> Any:
    return column_seed("redact", namespace=None, provider_config=(("redact_with", "XXXX"),))


def test_redact_pandas_fallback_serves_only_the_conversion_failure_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[int] = []
    monkeypatch.setattr(_redact, "redact_array", _marked_arrow(_redact.redact_array, log))

    admitted = pd.DataFrame({"c": pd.Series(["a", "b", None])})
    out, _ = RedactHandler().run(admitted, "c", _redact_seed(), ctx=None)
    assert out["c"].tolist() == [MARKED + "XXXX", MARKED + "XXXX", None] and log == [3]

    degraded = pd.DataFrame({"c": pd.Series(pd.Categorical(["a", 5, None]))})
    with pytest.raises(pa.ArrowException):  # the pre-kernel conversion the kernel cannot catch
        pa.array(degraded["c"], from_pandas=True)
    out, _ = RedactHandler().run(degraded, "c", _redact_seed(), ctx=None)
    assert out["c"].tolist()[:2] == ["XXXX", "XXXX"] and log == [3]  # kernel not reached
