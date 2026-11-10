"""The `timestamp[ns]` range under the identity temporal promotion (round 2 of the typed seam).

`_pipeline_routing_fk_dtype._identity_temporal_promotion` keeps a `s|ms|us` identity parent key
(passthrough or unseeded) on out-of-core when the child key is `timestamp[ns]`. `ns` holds only
1677-09-21 00:12:43.145224192 .. 2262-04-11 23:47:16.854775807, while the parent unit holds
years 0001..9999. These cells pin why the promotion is still exact: under an identity parent
every published child value is a child instant (PRESERVE/WARN orphans, REMAP minted from the
child keys) or a parent instant equal to one (a match on the exact epoch-ns token), so it is in
`ns` range by construction; an out-of-range parent key has no child and is published only in
the parent table, at its own unit.

Cells: parent key at year 0001 and 9999, and at each edge of the `ns` range and one parent-unit
step outside it, on every orphan policy, gate and mode, against the full-frame route. Labels:
R2CH = passes at the round-1 head 6bb18850 and after this round (characterization, not red
evidence).
"""

from __future__ import annotations

import datetime as dt
import functools
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as d2b
from tests.parity import _dcomp_ooc_shapes as dco
from tests.parity import _ooc_temporal_fk as tf

PARENT_UNITS = ("s", "ms", "us")
POLICIES = ("preserve", "warn", "remap", "fail")
MATCH = dt.datetime(2020, 1, 1)
ORPHAN = dt.datetime(2030, 1, 1)

# Per parent unit: (label, instant, whether `timestamp[ns]` holds it). The edge values are the
# first and last instants of the parent unit inside the ns range, and the next step outside.
_EDGES: dict[str, tuple[tuple[str, dt.datetime, bool], ...]] = {
    "s": (
        ("ns_min", dt.datetime(1677, 9, 21, 0, 12, 44), True),
        ("below_ns_min", dt.datetime(1677, 9, 21, 0, 12, 43), False),
        ("ns_max", dt.datetime(2262, 4, 11, 23, 47, 16), True),
        ("above_ns_max", dt.datetime(2262, 4, 11, 23, 47, 17), False),
    ),
    "ms": (
        ("ns_min", dt.datetime(1677, 9, 21, 0, 12, 43, 146000), True),
        ("below_ns_min", dt.datetime(1677, 9, 21, 0, 12, 43, 145000), False),
        ("ns_max", dt.datetime(2262, 4, 11, 23, 47, 16, 854000), True),
        ("above_ns_max", dt.datetime(2262, 4, 11, 23, 47, 16, 855000), False),
    ),
    "us": (
        ("ns_min", dt.datetime(1677, 9, 21, 0, 12, 43, 145225), True),
        ("below_ns_min", dt.datetime(1677, 9, 21, 0, 12, 43, 145224), False),
        ("ns_max", dt.datetime(2262, 4, 11, 23, 47, 16, 854775), True),
        ("above_ns_max", dt.datetime(2262, 4, 11, 23, 47, 16, 854776), False),
    ),
}
_FAR = (
    ("y0001", dt.datetime(1, 1, 1), False),
    ("y9999", dt.datetime(9999, 12, 31, 23, 59, 59), False),
)


def _cells() -> list[tuple[str, str, dt.datetime, bool]]:
    return [
        (unit, label, value, in_ns)
        for unit in PARENT_UNITS
        for label, value, in_ns in _FAR + _EDGES[unit]
    ]


CELLS = _cells()
CELL_IDS = [f"{unit}->ns-{label}" for unit, label, _, _ in CELLS]


def test_the_edge_table_is_the_ns_range() -> None:
    """Non-vacuity for the edges: each `in_ns` flag is what Arrow itself says."""
    for unit, label, value, in_ns in CELLS:
        parent = pa.array([value], tf.ts(unit))
        try:
            parent.cast(tf.ts("ns"))
            held = True
        except pa.ArrowInvalid:
            held = False
        assert held is in_ns, (unit, label)


def _keys(unit: str, value: dt.datetime, in_ns: bool, policy: str) -> tuple[pa.Array, pa.Array]:
    """Parent: the cell instant plus a common match. Child: the cell instant when `ns` can hold
    it (a matched value at the edge), the common match, an orphan (not under FAIL), a null."""
    parent = pa.array([value, MATCH], tf.ts(unit))
    child: list[Any] = [value if in_ns else MATCH, MATCH]
    if policy != "fail":
        child.append(ORPHAN)
    child.append(None)
    return parent, pa.array(child, tf.ts("ns"))


def _outcome(run: Any) -> tuple[Any, ...]:
    child = run.outputs["child"].column("parent_id").combine_chunks()
    parent = run.outputs["parent"].column("id").combine_chunks()
    return ("ok", child, parent)


def _attempt(fn: Any) -> tuple[Any, ...]:
    try:
        return _outcome(fn())
    except ExecutionError as exc:
        return ("refused", exc.code)


@functools.cache
def _reference(unit: str, label: str, policy: str, tmp_root: str) -> tuple[Any, ...]:
    value, in_ns = next((v, f) for u, lab, v, f in CELLS if u == unit and lab == label)
    parent, child = _keys(unit, value, in_ns, policy)
    tmp = Path(tmp_root) / f"ref-{unit}-{label}-{policy}"
    config, sources = tf.job(tmp, parent, child, policy=policy)
    return _attempt(lambda: tf.run(config, sources, gate="full_frame"))


@pytest.fixture(scope="module")
def ref_root(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("ns-range-ref"))


def _fixed_type(unit: str, policy: str) -> pa.DataType:
    # FAIL types the column from the masked-parent relation; the other policies unify to `ns`.
    return tf.ts(tf.RELATION_UNIT[unit]) if policy == "fail" else tf.ts("ns")


@pytest.mark.parametrize("mode", tf.MODES)
@pytest.mark.parametrize("gate", tf.GATES)
@pytest.mark.parametrize("policy", POLICIES, ids=[f"{p}-R2CH" for p in POLICIES])
@pytest.mark.parametrize(("unit", "label", "value", "in_ns"), CELLS, ids=CELL_IDS)
def test_passthrough_parent_outside_ns_matches_full_frame(
    unit: str,
    label: str,
    value: dt.datetime,
    in_ns: bool,
    policy: str,
    gate: str,
    mode: str,
    tmp_path: Path,
    ref_root: str,
) -> None:
    reference = _reference(unit, label, policy, ref_root)
    parent, child = _keys(unit, value, in_ns, policy)
    config, sources = tf.job(tmp_path, parent, child, policy=policy)
    got: tuple[Any, ...]
    try:
        run = tf.run(config, sources, gate=gate, mode=mode, tmp=tmp_path)
    except ExecutionError as exc:
        got = ("refused", exc.code)
    else:
        # The identity promotion keeps this job out-of-core on every gate; a diversion would
        # make the parity below vacuous for the route under review.
        assert run.mode[0] == "out_of_core", run.mode
        got = _outcome(run)
    assert reference[0] == "ok", reference
    assert got[0] == "ok", got
    tf.assert_matches_reference(reference[1], got[1], _fixed_type(unit, policy))
    # The far key reached the route and is published in the parent table, instant for instant
    # (a Parquet sink stores a `timestamp[s]` column as `ms`, so compare instants, not units).
    assert got[2].to_pylist() == reference[2].to_pylist()
    assert value in got[2].to_pylist()


def _unseeded_shape(unit: str, value: dt.datetime, in_ns: bool, policy: str) -> d2b.Shape:
    parent, child = _keys(unit, value, in_ns, policy)
    plan = d2b.make_plan({"P": d2b.table_seed({}), "C": d2b.table_seed({})})
    return d2b.Shape(
        plan,
        d2b.graph(d2b.edge("P", "n", "C", "n", "ns_n", OrphanPolicy(policy))),
        {"P": pa.table({"n": parent}), "C": pa.table({"n": child})},
    )


@pytest.mark.parametrize("mode", dco.MODES)
@pytest.mark.parametrize("policy", POLICIES, ids=[f"{p}-R2CH" for p in POLICIES])
@pytest.mark.parametrize(("unit", "label", "value", "in_ns"), CELLS, ids=CELL_IDS)
def test_unseeded_parent_never_reaches_the_out_of_core_join(
    unit: str,
    label: str,
    value: dt.datetime,
    in_ns: bool,
    policy: str,
    mode: str,
    tmp_path: Path,
) -> None:
    """The exemption's unseeded arm decides only the router's verdict: the out-of-core runner
    itself refuses an unseeded parent key, coded and before any output, so no unseeded key can
    reach `fk_output_array`. Full-frame completes the same job."""
    shape = _unseeded_shape(unit, value, in_ns, policy)
    reference = d2b.run_full(shape)
    assert value in reference.outputs["P"].column("n").to_pylist()
    with pytest.raises(ExecutionError) as exc:
        dco.run_ooc(shape, mode, tmp_path=tmp_path)
    assert exc.value.code == "out_of_core_parent_seed_missing"
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*.parquet"))


@pytest.mark.parametrize("mode", tf.MODES)
@pytest.mark.parametrize("gate", tf.GATES)
@pytest.mark.parametrize(
    ("unit", "label", "value", "in_ns"), CELLS, ids=[f"{i}-R2CH" for i in CELL_IDS]
)
def test_unseeded_parent_through_the_pipeline_matches_full_frame(
    unit: str,
    label: str,
    value: dt.datetime,
    in_ns: bool,
    gate: str,
    mode: str,
    tmp_path: Path,
) -> None:
    """A parent key left out of the config is no longer a reachable state: the declaration gate
    (CONFIG-EXPLICIT E2c) refuses it on every gate, mode and value before any route is chosen."""
    parent, child = _keys(unit, value, in_ns, "warn")
    config, sources = tf.job(tmp_path, parent, child, policy="warn")
    parent_table = config["tables"][0]
    parent_table["columns"] = [c for c in parent_table["columns"] if c["name"] != "id"]
    with pytest.raises(PipelineValidationError) as refused:
        tf.run(config, sources, gate=gate, mode=mode, tmp=tmp_path)
    assert refused.value.code == "source_file.undeclared_column"
