"""Phase D / Dcomp: composite-FK acceptance cases on the sequential (SEQ) route, no companion needed.

Hash-key subset. The composite key compute is already native (D2a) and composite resolution is a
tuple remap in Python by design, so these cases pin behavior and parity, not new compute. SEQ is
compared with the full-frame oracle (FF) on three channels kept apart on purpose, because the
strict comparator covers output tables only:

  - output tables: `compare_strict` over its 9 dimensions;
  - warnings, row errors and quarantine: each asserted on its own channel;
  - native provenance: `MarkingKernel` returns a distinguishable token, so a column counts as
    natively produced only when the emitted table carries the marked value.

"SEQ native" below is the stand-in kernel (runs everywhere); "SEQ python" hides the companion. The
real compiled kernel runs the same shapes in `test_phase_d_dcomp_composite_seq_real.py`. The
errored-key shapes use bucketize and code_set on key columns, which run on Python on a key, so
they assert oracle parity and make no native claim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential_native import SEQ_HASH_DISPOSITION_KEY
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _dcomp_shapes as d
from tests.parity import _strict_compare as sc
from tests.parity._d2a_seq_data import RecordingSink
from tests.parity._seq_witness import MARK, MarkingKernel, hide_companion, install

FAIL, WARN, PRESERVE, REMAP = (
    OrphanPolicy.FAIL,
    OrphanPolicy.WARN,
    OrphanPolicy.PRESERVE,
    OrphanPolicy.REMAP,
)
COMPOSITE_ORPHAN_MSG = (
    "1 orphan row(s) in C.('k', 'j') reference no parent key in P.('k', 'j') (orphan_policy=fail)."
)


def _marked_expectation(shape: s.Shape, unmarked: dict[str, frozenset[str]] | None = None) -> Any:
    """The oracle's tables with the stand-in's mark on every derived token."""
    unmarked = unmarked or {}
    oracle = s.run_full(shape).outputs
    return {
        t: {
            c: s.marked_like(oracle[t], c, MARK, unmarked=unmarked.get(t, frozenset()))
            for c in oracle[t].column_names
        }
        for t in oracle
    }


def _emitted(result: Any) -> Any:
    return {t: out.to_pydict() for t, out in result.outputs.items()}


def _seq_native(shape: s.Shape, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[Any, Any]:
    kernel = install(monkeypatch, MarkingKernel())
    return s.run_seq(shape, **kw), kernel


def _seq_python(shape: s.Shape, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    with monkeypatch.context() as m:
        hide_companion(m)
        return s.run_seq(shape, **kw)


# C1: orphan policy FAIL, genuine orphan.


_FAIL_SHAPES = {
    "composite-shape": lambda: s.composite_shape(FAIL),
    "golden-claims": lambda: d.golden_composite_shape(FAIL, orphan=True),
}


def _raised(call: Any) -> ExecutionError:
    with pytest.raises(ExecutionError) as exc:
        call()
    return exc.value


@pytest.mark.parametrize("name", _FAIL_SHAPES)
def test_fail_genuine_composite_orphan_raises_one_message_on_ff_seq_native_and_seq_python(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _FAIL_SHAPES[name]
    ff = _raised(lambda: s.run_full(build()))
    install(monkeypatch, MarkingKernel())
    seq_native = _raised(lambda: s.run_seq(build()))
    with monkeypatch.context() as m:
        hide_companion(m)
        seq_python = _raised(lambda: s.run_seq(build()))
    assert ff.code == seq_native.code == seq_python.code == "orphan_fk_violation"
    assert ff.message == seq_native.message == seq_python.message
    if name == "composite-shape":
        assert ff.message == COMPOSITE_ORPHAN_MSG
    else:
        # Identifiers only: the orphan's key values and the parent's must never reach the text.
        assert ff.message.startswith("1 orphan row(s) in claims.")
        for leaked in ("M-ORPHAN", "P-ORPHAN", "1999-01-01", "CL-ORPHAN"):
            assert leaked not in ff.message


@pytest.mark.parametrize("mode", ["native", "python"])
def test_fail_orphan_aborts_the_seq_sink_and_publishes_nothing(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = RecordingSink()
    if mode == "native":
        install(monkeypatch, MarkingKernel())
    else:
        hide_companion(monkeypatch)
    with pytest.raises(ExecutionError) as exc:
        s.run_seq(s.composite_shape(FAIL), sink=sink)
    assert exc.value.code == "orphan_fk_violation"
    assert (sink.commits, sink.aborts, sink.committed) == (0, 1, {})


def test_fail_does_not_count_a_partial_null_tuple_as_an_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tuple with one null component resolves to all-null and the row survives; it is not an
    # orphan, so FAIL has nothing to raise on and SEQ still equals the oracle.
    parent = d.key_table(["a", "b"], ["x", "y"])
    child = d.key_table(["a", None, "b"], ["x", "y", None])
    shape = d.composite_tables(parent, child, FAIL)
    ff = s.run_full(shape)
    d.assert_strict_parity(ff, _seq_python(shape, monkeypatch), "seq-python")
    assert ff.outputs["C"].column("k").to_pylist()[1:] == [None, None]
    assert ff.outputs["C"].column("j").to_pylist()[1:] == [None, None]


# C2: orphan policy FAIL, errored-parent cascade (a quarantine disposition, not an orphan raise).


def _quarantine(tmp_path: Path, triggers: list[str]) -> dict[str, Any]:
    return {"enabled": True, "output_path": str(tmp_path / "q.jsonl"), "triggers": triggers}


@pytest.mark.parametrize(
    ("order", "winner", "loser"),
    [
        (("k", "j"), "format_error", "mask_error"),
        (("j", "k"), "mask_error", "format_error"),
    ],
    ids=["k-first", "j-first"],
)
def test_errored_parent_cascades_under_fail_and_the_first_declared_key_column_names_the_trigger(
    order: tuple[str, str], winner: str, loser: str, tmp_path: Path
) -> None:
    shape = d.errored_key_shape(d.write_chapter_corpus(tmp_path), order)
    quarantine = _quarantine(tmp_path, ["format_error", "mask_error"])
    ff = s.run_full(shape)
    seq = d.run_seq_quarantined(shape, quarantine)  # must not raise orphan_fk_violation

    # Row-error channel: identical on both routes, parent records and the child cascade alike.
    def rows(result: Any) -> list[tuple[Any, ...]]:
        return sorted(
            (r.table, r.column, r.row_index, r.trigger, r.reason) for r in result.row_errors
        )

    assert rows(seq) == rows(ff)
    parent_triggers = {r.trigger for r in seq.row_errors if r.table == "P"}
    assert parent_triggers == {winner, loser}  # two distinct covered errors on one parent row
    (cascade,) = [r for r in seq.row_errors if r.table == "C"]
    # setdefault over the declared key columns: the FIRST declared column's trigger wins.
    assert (cascade.column, cascade.row_index, cascade.trigger) == (order[0], 1, winner)
    assert "cascaded to quarantine" in cascade.reason

    # Surviving rows are the oracle's with the errored parent row and its child removed, strict on
    # all 9 dimensions (schema, metadata and width, not just values).
    for table, keep in (("P", [0, 2, 3]), ("C", [0, 2])):
        assert sc.compare_strict(ff.outputs[table].take(keep), seq.outputs[table]) == d.ALL_TRUE, (
            table
        )
    for table, column in (("P", "k"), ("C", "k"), ("P", "j"), ("C", "j")):
        assert not {"bad", "U07.1"} & set(seq.outputs[table].column(column).to_pylist())

    # Quarantine channel: the raw errored key lives only in the quarantine sidecar, and the cascaded
    # child entry carries the winning trigger and a nulled key.
    entries = [json.loads(line) for line in (tmp_path / "q.jsonl").read_text().splitlines()]
    by_table = {e["_source_table"]: e for e in entries}
    assert len(entries) == 2
    assert (by_table["P"]["k"], by_table["P"]["j"]) == ("bad", "U07.1")
    assert (by_table["C"]["k"], by_table["C"]["j"]) == (None, None)
    assert by_table["C"]["_quarantine_trigger"] == winner
    assert seq.quality_metrics["quarantine"]["counts_by_trigger"] == {winner: 2, loser: 1}


@pytest.mark.parametrize("coverage", ["quarantine-off", "winning-trigger-uncovered"])
def test_uncovered_row_error_still_fails_the_run_and_publishes_nothing(
    coverage: str, tmp_path: Path
) -> None:
    shape = d.errored_key_shape(d.write_chapter_corpus(tmp_path), ("k", "j"))
    # order ("k", "j") makes format_error the winning (first-declared) trigger. "winning-trigger-
    # uncovered" covers ONLY the loser (mask_error): partial coverage that omits the winner still
    # fails the whole run, it does not silently quarantine on the covered-loser trigger alone.
    quarantine = None if coverage == "quarantine-off" else _quarantine(tmp_path, ["mask_error"])
    sink = RecordingSink()
    with pytest.raises(RowErrorsFailedError) as exc:
        d.run_seq_quarantined(shape, quarantine, sink=sink)
    assert "orphan_fk_violation" not in str(exc.value)
    assert (sink.commits, sink.aborts, sink.committed) == (0, 1, {})
    assert not (tmp_path / "q.jsonl").exists()


def test_genuine_orphan_still_raises_beside_an_errored_parent_cascade(tmp_path: Path) -> None:
    # The two dispositions are separate: a cascaded row is not counted as an orphan, and a real
    # orphan in the same child still fails the run under FAIL.
    shape = d.errored_key_shape(d.write_chapter_corpus(tmp_path), ("k", "j"))
    shape.sources["C"] = d.key_table(["10", "bad", "30", "70"], ["A01", "U07.1", "B01", "B01"])
    quarantine = _quarantine(tmp_path, ["format_error", "mask_error"])
    ff = _raised(lambda: s.run_full(shape))
    seq = _raised(lambda: d.run_seq_quarantined(shape, quarantine))
    assert ff.code == seq.code == "orphan_fk_violation"
    assert seq.message == ff.message
    assert seq.message.startswith("1 orphan row(s) in C.")


# C3: orphan policy WARN, exact channel.


def _orphan_warnings(result: Any) -> list[Any]:
    return [w for w in result.warnings if w.code == "orphan_fk"]


_WARN_SHAPES = {
    "composite-shape": lambda: s.composite_shape(WARN),
    "golden-claims": lambda: d.golden_composite_shape(WARN, orphan=True),
}


@pytest.mark.parametrize("name", _WARN_SHAPES)
def test_warn_composite_orphan_warning_is_byte_identical_on_ff_seq_native_and_seq_python(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = _WARN_SHAPES[name]
    ff = s.run_full(build())
    native, kernel = _seq_native(build(), monkeypatch)
    python = _seq_python(build(), monkeypatch)
    # Other warnings (the golden tables' undeclared columns) interleave per table on SEQ, so the
    # orphan channel is compared on its own and the full set only as a multiset.
    assert _orphan_warnings(ff) == _orphan_warnings(native) == _orphan_warnings(python)
    assert sorted(map(repr, ff.warnings)) == sorted(map(repr, native.warnings))
    assert sorted(map(repr, ff.warnings)) == sorted(map(repr, python.warnings))
    (warning,) = _orphan_warnings(ff)
    assert (warning.code, warning.detail["orphan_rows"]) == ("orphan_fk", 1)
    assert warning.column == ",".join(warning.detail["child_columns"])
    # Output channel: python SEQ equals the oracle on all 9 dimensions; native SEQ is the
    # oracle's tables with the stand-in's mark on every derived token.
    d.assert_strict_parity(ff, python, name)
    assert _emitted(native) == _marked_expectation(build())
    assert kernel.calls, "the stand-in kernel was never called, so SEQ native proved nothing"


def test_warn_keeps_the_orphan_tuple_unmasked_and_preserve_emits_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warned = _seq_python(s.composite_shape(WARN), monkeypatch)
    preserved = _seq_python(s.composite_shape(PRESERVE), monkeypatch)
    assert warned.outputs["C"].to_pydict() == preserved.outputs["C"].to_pydict()
    assert (
        warned.outputs["C"].column("k")[2].as_py(),
        warned.outputs["C"].column("j")[2].as_py(),
    ) == (
        "q",
        "w",
    )
    assert preserved.warnings == ()
    assert [w.code for w in warned.warnings] == ["orphan_fk"]


# C5: wide-int and mixed component typing. FF and SEQ are named explicitly in each case.

_INT_TYPES = [(pa.int64(), d.BIG), (pa.uint64(), d.UBIG)]
_INT_IDS = ["int64", "uint64"]


@pytest.mark.parametrize(("n_type", "big"), _INT_TYPES, ids=_INT_IDS)
@pytest.mark.parametrize("policy", [PRESERVE, REMAP, WARN], ids=lambda p: p.name.lower())
def test_c5a_integer_passthrough_component_keeps_exact_values_and_type_on_ff_and_seq(
    n_type: pa.DataType, big: int, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = d.int_component_shape(
        policy,
        parent_n=[big, 7, None],
        # matched, matched, orphan, null k, null n, orphan (its parent tuple has a null component)
        child_k=["a", "b", "q", None, "a", "c"],
        child_n=[big, 7, 5, 8, None, 9],
        n_type=n_type,
    )
    ff = s.run_full(shape)
    native, kernel = _seq_native(shape, monkeypatch)
    python = _seq_python(shape, monkeypatch)
    expected_n = {"P": [big, 7, None], "C": [big, 7, 5, None, None, 9]}
    for label, result in (("ff", ff), ("seq-native", native), ("seq-python", python)):
        for table, values in expected_n.items():
            column = result.outputs[table].column("n")
            # Exact value, exact Arrow width (not folded to float64), null stays null (no NaN).
            assert column.type == n_type, (label, table)
            assert column.to_pylist() == values, (label, table)
            assert column.null_count == values.count(None), (label, table)
    d.assert_strict_parity(ff, python, "seq-python")
    # Only the hashed sibling reaches the kernel: three parent rows, plus the two orphans under REMAP.
    assert sorted(c.rows for c in kernel.calls) == ([2, 3] if policy is REMAP else [3])
    assert _emitted(native) == _marked_expectation(shape)
    backends = {
        (e["table"], e["column"]): e["executed_backend"]
        for e in native.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "scalar"
    }
    assert backends == {("P", "k"): "rust_companion"}


@pytest.mark.parametrize("runner", ["ff", "seq"])
def test_c5b_nullable_hashed_integer_component_is_still_rejected_on_ff_and_seq(
    runner: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Retained rejection, not a success path: a hashed integer key component with a null is refused
    # up front on both routes, where the passthrough integer above is accepted.
    install(monkeypatch, MarkingKernel())
    shape = s.composite_shape(PRESERVE)
    sources = dict(shape.sources)
    sources["P"] = pa.table({"k": s.strs(["a", "b", "c"]), "j": pa.array([1, None, 3], pa.int64())})
    sources["C"] = pa.table({"k": s.strs(["a"]), "j": pa.array([1], pa.int64())})
    shape = s.Shape(shape.plan, shape.graph, sources)
    run = s.run_full if runner == "ff" else s.run_seq
    assert _raised(lambda: run(shape)).code == "null_bearing_int_unsupported"


@pytest.mark.parametrize("policy", [PRESERVE, WARN], ids=lambda p: p.name.lower())
def test_c5c_matched_float_beside_an_unrepresentable_integer_orphan_fails_closed_on_ff_and_seq(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The magnitude guard: a whole-valued float orphan past 2**53 folds to an integer the matched
    # float column cannot absorb, so every route refuses with the shared code. 2**62 is itself
    # exactly representable, so this pins the magnitude bound; the precision-loss case is below.
    def shape() -> s.Shape:
        return d.float_int_orphan_shape(policy, orphan_n=float(2**62))

    ff = _raised(lambda: s.run_full(shape()))
    install(monkeypatch, MarkingKernel())
    seq_native = _raised(lambda: s.run_seq(shape()))
    with monkeypatch.context() as m:
        hide_companion(m)
        seq_python = _raised(lambda: s.run_seq(shape()))
    code = "out_of_core_fk_key_dtype_unsupported"
    assert ff.code == seq_native.code == seq_python.code == code
    assert ff.message == seq_native.message == seq_python.message
    # No per-row value reaches the message, in either the decimal or the float-repr form.
    assert str(2**62) not in ff.message
    assert repr(float(2**62)) not in ff.message


@pytest.mark.parametrize("policy", [PRESERVE, WARN], ids=lambda p: p.name.lower())
def test_c5c_ii_a_non_exact_integer_orphan_fails_closed_on_ff_and_seq(
    policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The precision-loss case: 2**62 + 1 is odd, so it has no exact float64 value. The orphan is an
    # int64 child component beside a float parent column, so the resolved column would have to hold
    # both and silently round the orphan. Every route must refuse. (Removing or bypassing the
    # mixed-type rejection would silently round the orphan; this test's raise kills that mutation.)
    def shape() -> s.Shape:
        return d.float_parent_int_child_shape(policy, orphan_int=2**62 + 1)

    ff = _raised(lambda: s.run_full(shape()))
    install(monkeypatch, MarkingKernel())
    seq_native = _raised(lambda: s.run_seq(shape()))
    with monkeypatch.context() as m:
        hide_companion(m)
        seq_python = _raised(lambda: s.run_seq(shape()))
    code = "out_of_core_fk_key_dtype_unsupported"
    assert ff.code == seq_native.code == seq_python.code == code
    assert ff.message == seq_native.message == seq_python.message
    assert str(2**62 + 1) not in ff.message


def test_c5c_control_a_float_orphan_at_the_exact_bound_is_accepted_and_matches_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 2**53 still round-trips through float64, so the same shape one step inside the bound is a
    # success with strict parity: the refusal above is the precision limit, not a blanket ban.
    shape = d.float_int_orphan_shape(PRESERVE, orphan_n=float(2**53))
    d.assert_strict_parity(s.run_full(shape), _seq_python(shape, monkeypatch))


# C-bound: boundary inputs. A composite table that is empty, or whose resolved component is all null.


_BOUND_CASES = {
    "both-empty": (d.key_table([], []), d.key_table([], [])),
    "child-empty": (d.key_table(["a", "b"], ["x", "y"]), d.key_table([], [])),
    "parent-empty": (d.key_table([], []), d.key_table(["a", "q"], ["x", "w"])),
    "child-j-all-null": (
        d.key_table(["a", "b"], ["x", "y"]),
        d.key_table(["a", "b"], [None, None]),
    ),
    "child-both-all-null": (
        d.key_table(["a", "b"], ["x", "y"]),
        d.key_table([None, None], [None, None]),
    ),
}
# Kernel calls per case: only a parent with rows hashes, and REMAP mints a token only for an
# orphan. A child whose components resolve to null owns no orphan, so it adds no call.
_BOUND_CALLS: dict[str, dict[OrphanPolicy, list[int]]] = {
    "both-empty": {PRESERVE: [], REMAP: []},
    "child-empty": {PRESERVE: [2, 2], REMAP: [2, 2]},
    "parent-empty": {PRESERVE: [], REMAP: [2, 2]},
    "child-j-all-null": {PRESERVE: [2, 2], REMAP: [2, 2]},
    "child-both-all-null": {PRESERVE: [2, 2], REMAP: [2, 2]},
}


@pytest.mark.parametrize("policy", [PRESERVE, REMAP], ids=lambda p: p.name.lower())
@pytest.mark.parametrize("name", _BOUND_CASES)
def test_cbound_empty_and_all_null_composites_match_the_oracle_with_honest_compute_evidence(
    name: str, policy: OrphanPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = _BOUND_CASES[name]
    shape = d.composite_tables(parent, child, policy)
    ff = s.run_full(shape)
    python = _seq_python(shape, monkeypatch)
    d.assert_strict_parity(ff, python, name)
    native, kernel = _seq_native(shape, monkeypatch)
    assert sorted(c.rows for c in kernel.calls) == _BOUND_CALLS[name][policy], name
    # Native provenance: whatever the kernel produced is the marked oracle; nothing else is native.
    assert _emitted(native) == _marked_expectation(shape), name
    paths = {
        (e["table"], e["column"], e["path"]): e["executed_backend"]
        for e in native.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    }
    if parent.num_rows == 0:
        # An empty parent computed nothing, so it must not claim the companion did.
        assert {paths[("P", c, "scalar")] for c in ("k", "j")} == {"none"}
    if name.startswith("child-") and name != "child-empty":
        # No orphan tuple, so no remap compute is reported.
        assert not [p for p in paths if p[2] == "fk_remap"]


@pytest.mark.parametrize("n_type", [pa.int64(), pa.uint64()], ids=["int64", "uint64"])
def test_cbound_all_null_resolved_integer_component_keeps_its_integer_type_on_ff_and_seq(
    n_type: pa.DataType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every child row has a null component, so the resolved integer column is entirely null. It
    # must stay an integer column (the all-null write-back branch), never a float64 of NaN.
    shape = d.int_component_shape(
        PRESERVE, parent_n=[1, 2], child_k=["a", None], child_n=[None, 2], n_type=n_type
    )
    ff = s.run_full(shape)
    python = _seq_python(shape, monkeypatch)
    for result in (ff, python):
        column = result.outputs["C"].column("n")
        assert column.type == n_type
        assert column.to_pylist() == [None, None]
    d.assert_strict_parity(ff, python)


def test_cbound_composite_group_node_itself_masks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The group node derives nothing of its own: a matched child tuple is the parent's tuple, and
    # a REMAP group legitimately reaches the parent components' kernels only for its orphans.
    shape = s.composite_shape(REMAP)
    native, kernel = _seq_native(shape, monkeypatch)
    inherited = {
        (e["table"], e["column"])
        for e in native.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "fk_inherited"
    }
    assert inherited == {("C", "k"), ("C", "j")}
    assert sorted(c.rows for c in kernel.calls) == [1, 1, 3, 3]  # parents once, one orphan each
    assert all(v.startswith(MARK) for v in native.outputs["P"].column("k").to_pylist())
