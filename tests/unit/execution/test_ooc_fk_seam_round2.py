"""Round 2 of the typed out-of-core FK seam: exception text and the whole-child oracle.

Labels: R2FB = red at the round-1 head 6bb18850, green after; R2CH = green at both
(characterization or a pin for a rejected finding, not red evidence).

Class 1 (value-bearing output): an FK key value must not reach an exception's str, args,
notes, repr, cause or context. `cast_fk_chunk` is the cast every FK chunk path shares
(`fk_output_array`, `_concat_fk_chunks`, `_batch_join._cast_chunks`); pyarrow's cast error
text quotes the offending value.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE
from decoy_engine.execution.out_of_core import _batch_join
from decoy_engine.execution.out_of_core import _join as join_mod
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_from_tables
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge
from tests.parity import _ooc_temporal_fk as tf
from tests.unit.execution import _ooc_fixtures as fx

_BIG = 2**63 + 987_654_321  # a uint64 key beyond int64: the value is the canary


def _surfaces(exc: BaseException) -> list[str]:
    """Every text an exception carries: str, repr, args, notes, and the same for each link."""
    texts: list[str] = []
    link: BaseException | None = exc
    while link is not None:
        texts += [str(link), repr(link), repr(link.args), repr(getattr(link, "__notes__", None))]
        link = link.__cause__ or link.__context__
    return texts


def _assert_value_free(exc: BaseException, canary: str) -> None:
    assert exc.__cause__ is None and exc.__context__ is None, type(exc.__context__)
    leaked = [t for t in _surfaces(exc) if canary in t]
    assert not leaked, leaked


# (chunk, target, canary text the value prints as)
_CAST_CASES = {
    "uint64_beyond_int64": (pa.array([_BIG], pa.uint64()), pa.int64(), str(_BIG)),
    "fractional_float_to_int": (pa.array([424_242.5]), pa.int64(), "424242"),
    "string_to_int": (pa.array(["canary-424242"]), pa.int64(), "canary-424242"),
    "int_beyond_int8": (pa.array([424_242], pa.int64()), pa.int8(), "424242"),
}


@pytest.mark.parametrize("name", [f"{n}-R2FB" for n in sorted(_CAST_CASES)])
def test_cast_fk_chunk_refusal_carries_no_key_value(name: str) -> None:
    chunk, target, canary = _CAST_CASES[name.removesuffix("-R2FB")]
    with pytest.raises(pa.ArrowException) as exc:
        join_mod.cast_fk_chunk(chunk, target)
    _assert_value_free(exc.value, canary)
    assert str(chunk.type) in str(exc.value) and str(target) in str(exc.value)


@pytest.mark.parametrize("name", [f"{n}-R2FB" for n in sorted(_CAST_CASES)])
def test_the_batch_joiner_cast_carries_no_key_value(name: str) -> None:
    chunk, target, canary = _CAST_CASES[name.removesuffix("-R2FB")]
    with pytest.raises(pa.ArrowException) as exc:
        _batch_join._cast_chunks([chunk], target)
    _assert_value_free(exc.value, canary)


def test_the_whole_child_concat_carries_no_key_value_R2FB() -> None:
    # Permissive merge of uint64 with int64 picks int64; the uint64 key beyond int64 then fails
    # the cast. Whole-column inference raises too (the pinned parity battery), so the Arrow
    # family is kept; only the value is dropped.
    chunks = [pa.array([_BIG], pa.uint64()), pa.array([-1], pa.int64())]
    with pytest.raises(pa.ArrowException) as exc:
        join_mod._concat_fk_chunks(chunks)
    _assert_value_free(exc.value, str(_BIG))


# The finding as written (LOW): `fk_output_array` lets the cast error ride as `__cause__`.
# Rejected: the coded error is raised after the `except` block, so it has no cause or context.
_SEAM_CASES = {
    "uint64_beyond_int64": ([_BIG], pa.int64(), str(_BIG)),
    "fractional_float_to_int": ([424_242.5], pa.int64(), "424242"),
    "int_beyond_int8": ([424_242], pa.int8(), "424242"),
    "out_of_range_instant": ([dt.datetime(9999, 1, 2, 3, 4, 5)], pa.timestamp("ns"), "9999"),
    "sub_unit_instant": ([dt.datetime(2020, 1, 1, 0, 0, 0, 424242)], pa.timestamp("s"), "424242"),
}


@pytest.mark.parametrize("name", [f"{n}-R2CH" for n in sorted(_SEAM_CASES)])
def test_fk_output_array_refusal_has_no_cause_context_or_value(name: str) -> None:
    values, target, canary = _SEAM_CASES[name.removesuffix("-R2CH")]
    with pytest.raises(ExecutionError) as exc:
        join_mod.fk_output_array(list(values), target)
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    _assert_value_free(exc.value, canary)


# `fk_output_array` promises exact output or one coded refusal; a cast Arrow does not implement
# escaped uncoded.
_UNIMPLEMENTED = {
    "date_to_int64": ([dt.date(2020, 1, 1)], pa.int64()),
    "bool_to_date32": ([True], pa.date32()),
    "float_to_date32": ([1.5], pa.date32()),
}


@pytest.mark.parametrize("name", [f"{n}-R2FB" for n in sorted(_UNIMPLEMENTED)])
def test_an_unimplemented_cast_is_the_coded_refusal(name: str) -> None:
    values, target = _UNIMPLEMENTED[name.removesuffix("-R2FB")]
    with pytest.raises(ExecutionError) as exc:
        join_mod.fk_output_array(list(values), target)
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    assert exc.value.__cause__ is None and exc.value.__context__ is None


# ---------------------------------------------------------------------------
# MEDIUM (whole-child `mask_child_fk` keeps value inference). It is the resident test oracle:
# no production module calls it (static: `rg mask_child_fk src` finds only its definition, the
# `mask_child_fk_fail` wrapper and the package re-export; dynamic: the sentry below). The
# difference from the batch joiners is pinned as characterized, not as a guarantee.
# ---------------------------------------------------------------------------


def _whole_child(parent_keys: pa.Array, child_keys: pa.Array, tmp: Path) -> pa.Array:
    seed = fx._seed("passthrough")
    plan = fx._plan(seed, parent="P", child="C", column="k")
    edge = RelationshipEdge(
        parent_table="P",
        parent_columns=("k",),
        child_table="C",
        child_columns=("k",),
        namespace="n",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    parent, child = pa.table({"k": parent_keys}), pa.table({"k": child_keys})
    relation = build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=mask_table(plan, "P", parent, skip_columns=frozenset()),
        edge=edge,
        temp_dir=tmp / "relation",
    )
    out, _ = join_mod.mask_child_fk(
        child=child, edge=edge, parent_relation=relation, temp_dir=tmp / "join"
    )
    return out.column("k").combine_chunks()


@pytest.mark.parametrize("unit", ["s", "ms", "us"], ids=lambda u: f"{u}-R2CH")
def test_the_whole_child_oracle_types_temporal_keys_by_inference(unit: str, tmp_path: Path) -> None:
    """Characterized: Python datetimes infer `timestamp[us]`, so `s`/`ms` keys come back
    relabelled `us` (same instants). The batch joiners publish the resolved unit."""
    keys = pa.array([dt.datetime(2020, 1, 1), dt.datetime(2020, 1, 2), None], tf.ts(unit))
    got = _whole_child(keys, keys, tmp_path)
    assert got.type == pa.timestamp("us")
    assert got.to_pylist() == keys.to_pylist()


def test_the_whole_child_oracle_truncates_nanosecond_keys_R2CH(tmp_path: Path) -> None:
    """Characterized: `as_py()` gives `pd.Timestamp`, which infers `timestamp[us]`, so two keys
    1 ns apart collapse to one value. The batch joiners keep both (the round-1 FB cells)."""
    keys = tf.ns_keys(2)
    got = _whole_child(keys, keys, tmp_path)
    assert got.type == pa.timestamp("us")
    assert len(set(got.to_pylist())) == 1
    assert len(set(keys.to_pylist())) == 2


_ROUTES = [(g, m) for g in tf.GATES for m in tf.MODES] + [("full_frame", "resident")]


@pytest.mark.parametrize(("gate", "mode"), _ROUTES, ids=[f"{g}-{m}" for g, m in _ROUTES])
def test_no_production_route_reaches_the_whole_child_oracle_R2CH(
    gate: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import decoy_engine.execution.out_of_core as ooc_pkg

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a production route called the whole-child oracle")

    monkeypatch.setattr(join_mod, "mask_child_fk", refuse)
    monkeypatch.setattr(ooc_pkg, "mask_child_fk", refuse)
    with pytest.raises(AssertionError, match="whole-child oracle"):  # the patch is live
        ooc_pkg.mask_child_fk()
    for policy in ("preserve", "warn", "remap", "fail"):
        child = tf.instants_array("ns", 3)
        config, sources = tf.job(
            tmp_path / policy, tf.instants_array("us", 3), child, policy=policy
        )
        run = tf.run(config, sources, gate=gate, mode=mode, tmp=tmp_path / policy)
        expected = "full_frame" if gate == "full_frame" else "out_of_core"
        assert run.mode[0] == expected, run.mode
        assert run.batch_joiners + run.reorder_joiners >= (0 if gate == "full_frame" else 1)
