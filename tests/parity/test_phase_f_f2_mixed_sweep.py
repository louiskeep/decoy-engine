"""F2 sweep: a wide generate x mask combination matrix for the mixed route.

The fixed F2 parity suite pins one config shape. This module varies the shape: every
generation family (as an independent table, as a non-key column on a coupled parent, and
as the coupled FK parent key), every mask strategy (independent and coupled, int and
string keys), every orphan policy, nullable and big-int keys, the dirty generation shapes
the route must refuse, the fail-closed sibling cases, multi-mask jobs and the real
transactional sink.

Core invariant, checked for every case by `_compare`: the SAME job runs twice in this
process, once with `execution_mode="full_frame"` (the oracle) and once with `"auto"`. The
two must agree byte for byte on every output table (Arrow IPC stream bytes plus schema),
on the warnings, and, when the oracle raises, on the error code and text. Because both
legs run in the same process, wall-clock providers (faker dates) cancel out.

Each case also records the route it took (mixed or declined:<reason>). When the
`DECOY_F2_SWEEP_OUT` environment variable names a file, the session writes the full
route matrix there as JSON. Tables stay small and the chunk size is forced to 7 rows, so
every mixed case runs several chunks plus a partial final chunk.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, _mixed_static  # type: ignore[attr-defined]
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.generation import synthesize
from tests.parity._f0_fixtures import SNAPSHOT_PATH
from tests.parity._f2_mixed_support import (
    CHILD,
    COMPANION_PRESENT,
    FK,
    PARENT,
    RecordingSink,
    assert_outputs_identical,
    child_table,
    ipc_bytes,
    mask_evidence,
    mixed_config,
    oracle,
    run,
    warning_signature,
)

N = 41  # 5 full 7-row chunks plus a partial final chunk of 6
# The generated parent draws its `id` from a four-key categorical. The coupled topologies need the
# parent to hold EVERY key the child references, which a small draw only does by seed luck (the FR3
# kernel selection moved which keys 12 rows hit). 48 rows miss a key with probability ~4e-5 under any
# selection, so the precondition no longer rides one golden; `test_parent_covers_the_child_keys`
# asserts it.
PARENT_ROWS = 48
INT_PARENT = [{"name": "id", "type": "categorical", "categories": [10, 11, 12, 13]}]
STR_PARENT = [{"name": "id", "type": "categorical", "categories": ["k0", "k1", "k2", "k3"]}]
INT_KEYS = [10 + i % 4 for i in range(N)]
STR_KEYS = [f"k{i % 4}" for i in range(N)]
MATRIX: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _write_matrix() -> Iterator[None]:
    yield
    out = os.environ.get("DECOY_F2_SWEEP_OUT")
    if out:
        Path(out).write_text(json.dumps(MATRIX, indent=1, sort_keys=True))


@pytest.fixture
def static_reasons(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = _mixed_static.admit_mixed_static

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        seen.append(result[1])
        return result

    monkeypatch.setattr(_mixed_static, "admit_mixed_static", spy)
    return seen


@pytest.fixture
def generate_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = synthesize.generate_tables

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(synthesize, "generate_tables", spy)
    return calls


@dataclass
class Outcome:
    result: Any  # ExecutionResult, or None when the job raised
    error: ExecutionError | None
    route: str
    calls: int


def _route(result: Any, reasons: list[str]) -> str:
    qm = result.quality_metrics
    if "mixed_route" in qm and qm["execution"]["execution_mode"] == "mixed":
        return "mixed"
    reason = qm["execution"].get("route_reason", "")
    static = reasons[-1] if reasons else ""
    return f"declined:{reason if reason.startswith('mixed_runtime') else static or reason}"


def _attempt(
    config: dict[str, Any],
    sources: dict[str, Any],
    *,
    mode: str,
    reasons: list[str],
    calls: list[int],
    **kw: Any,
) -> Outcome:
    reasons.clear()
    calls.clear()
    try:
        result = run(config, sources, mode=mode, **kw)
    except ExecutionError as exc:
        return Outcome(None, exc, f"error:{exc.code}", len(calls))
    return Outcome(result, None, _route(result, reasons), len(calls))


def _compare(
    case: str,
    config: dict[str, Any],
    sources: dict[str, Any],
    reasons: list[str],
    calls: list[int],
    *,
    expect: str | None = None,
    **kw: Any,
) -> tuple[Outcome, Outcome]:
    """Run oracle and auto, assert they agree exactly, record the route."""
    want = _attempt(config, sources, mode="full_frame", reasons=reasons, calls=calls, **kw)
    got = _attempt(config, sources, mode="auto", reasons=reasons, calls=calls, **kw)
    if want.error is not None:
        assert got.error is not None, f"{case}: oracle raised but auto did not"
        assert got.error.code == want.error.code
        assert str(got.error) == str(want.error)
        identical = True
    else:
        assert got.error is None, f"{case}: auto raised {got.error!r} but the oracle did not"
        assert want.result.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert_outputs_identical(got.result.outputs, want.result.outputs)
        identical = True
    assert got.calls == want.calls, f"{case}: generation ran {got.calls}x vs oracle {want.calls}x"
    warnings_match = want.error is not None or (
        warning_signature(got.result) == warning_signature(want.result)
    )
    MATRIX.append(
        {
            "case": case,
            "route": got.route,
            "byte_identical": identical,
            "warnings_match": warnings_match,
        }
    )
    assert warnings_match, f"{case}: warnings differ from the oracle"
    if expect is not None:
        assert got.route == expect or got.route.startswith(expect), f"{case}: {got.route}"
    return want, got


def _backends(result: Any, table: str = CHILD) -> dict[str, str]:
    return {c["column"]: c["executed_backend"] for c in mask_evidence(result, table)["columns"]}


def _assert_native_witness(case: str, got: Outcome, table: str = CHILD) -> None:
    """The case routed mixed (always asserted: a silent full-frame fallback is byte-identical,
    so only the route can catch it) and its mask half ran on the compiled backend, not the
    pandas oracle leg (asserted when the companion is installed)."""
    assert got.route == "mixed", f"{case}: expected the mixed route, got {got.route}"
    if not COMPANION_PRESENT:
        return
    evidence = mask_evidence(got.result, table)
    assert evidence["native_admitted"] is True, case
    backends = _backends(got.result, table)
    assert backends and all(b != "pandas_oracle" for b in backends.values()), (case, backends)
    MATRIX[-1]["backends"] = sorted(set(backends.values()))


def _strs(result: Any, table: str, column: str) -> list[Any]:
    return result.outputs[table].column(column).to_pylist()


def _no_raw_survives(
    case: str,
    got: Outcome,
    source: list[Any],
    column: str,
    *,
    must_change: Callable[[Any], bool] = lambda v: True,
) -> None:
    """Rows the strategy is obliged to change must differ from the raw source value."""
    if got.result is None or CHILD not in got.result.outputs:
        return
    out = _strs(got.result, CHILD, column)
    assert len(out) == len(source)
    leaked = [s for s, o in zip(source, out, strict=True) if must_change(s) and str(o) == str(s)]
    assert not leaked, f"{case}: {len(leaked)} raw {column} values survived, e.g. {leaked[:3]}"
    MATRIX[-1]["raw_leaks"] = 0


def _int_ids(tmp_path: Path, parent_columns: list[dict[str, Any]]) -> list[Any]:
    """The generated parent keys (via the oracle), so child keys can hit real parents."""
    child = child_table(pa.array(["x"] * 7, pa.string()))
    config = mixed_config(tmp_path, child, parent_columns=parent_columns, coupled=False)
    return oracle(config, {CHILD: child}).outputs[PARENT].column("id").to_pylist()


def _key_array(keys: list[Any]) -> pa.Array:
    return pa.array(keys)


# ---------------------------------------------------------------------------
# Generation families
# ---------------------------------------------------------------------------

_GRP: list[dict[str, Any]] = [
    {"name": "g", "type": "categorical", "categories": ["a", "b"]},
    {"name": "o", "type": "formula", "formula": "i"},
]
FAMILIES: dict[str, list[dict[str, Any]]] = {
    "sequence": [{"name": "c", "type": "sequence", "start": 5, "step": 2}],
    "categorical": [{"name": "c", "type": "categorical", "categories": list("abcd")}],
    "faker_string": [{"name": "c", "type": "faker", "faker_type": "first_name"}],
    "faker_email": [{"name": "c", "type": "faker", "faker_type": "email"}],
    "faker_date_of_birth": [{"name": "c", "type": "faker", "faker_type": "date_of_birth"}],
    "faker_pyint": [{"name": "c", "type": "faker", "faker_type": "pyint"}],
    "formula": [{"name": "c", "type": "formula", "formula": "randint(1, 999)"}],
    "reference": [
        {
            "name": "c",
            "type": "reference",
            "reference_table": "lookup",
            "reference_column": "code",
            "distribution": "random",
        }
    ],
    "derived": [
        {"name": "v", "type": "formula", "formula": "i"},
        {"name": "c", "type": "derived", "expression": "v + 1"},
    ],
    "derived_aggregate": [
        {"name": "v", "type": "formula", "formula": "i"},
        {"name": "c", "type": "derived_aggregate", "op": "sum", "column": "v"},
    ],
    "grouped_cumcount": [
        *_GRP,
        {"name": "c", "type": "grouped_series", "group_by": "g", "order_by": "o"},
    ],
    "grouped_walk": [
        *_GRP,
        {
            "name": "c",
            "type": "grouped_series",
            "group_by": "g",
            "order_by": "o",
            "generator": "monotone_walk",
            "start": 1,
            "max_step": 5,
        },
    ],
    "group_key": [_GRP[0], {"name": "c", "type": "group_key", "group_by": "g"}],
    "statistical": [{"name": "amount", "type": "statistical", "snapshot_file": str(SNAPSHOT_PATH)}],
}


def _family_job(
    tmp_path: Path,
    parent_columns: list[dict[str, Any]],
    *,
    coupled: bool,
    keys: pa.Array | None = None,
    **kw: Any,
) -> tuple[dict[str, Any], pa.Table]:
    child = child_table(keys if keys is not None else pa.array(STR_KEYS, pa.string()))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=parent_columns,
        parent_rows=PARENT_ROWS,
        coupled=coupled,
        **kw,
    )
    return config, child


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_family_on_an_independent_generate_table(
    tmp_path: Path, family: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _family_job(
        tmp_path, FAMILIES[family], coupled=False, extra_generate=family == "reference"
    )
    _, got = _compare(f"gen-indep:{family}", config, {CHILD: child}, static_reasons, generate_calls)
    assert got.error is None and got.route == "mixed"
    _assert_native_witness(f"gen-indep:{family}", got)
    assert sorted(got.result.outputs) == sorted(
        [PARENT, CHILD, *(["lookup"] if family == "reference" else [])]
    )


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_family_as_a_non_key_column_on_a_coupled_parent(
    tmp_path: Path, family: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    parent = [{"name": "id", "type": "sequence", "start": 0, "step": 1}, *FAMILIES[family]]
    keys = pa.array([i % PARENT_ROWS for i in range(N)], pa.int64())
    config, child = _family_job(
        tmp_path, parent, coupled=True, keys=keys, extra_generate=family == "reference"
    )
    _, got = _compare(
        f"gen-coupled-nonkey:{family}", config, {CHILD: child}, static_reasons, generate_calls
    )
    assert got.route == "mixed"
    _assert_native_witness(f"gen-coupled-nonkey:{family}", got)
    ids = {str(v) for v in _strs(got.result, PARENT, "id")}
    assert {str(v) for v in _strs(got.result, CHILD, FK)} <= ids  # FK integrity


# family used AS the parent key: (id column, expected route).
KEY_FAMILIES: dict[str, tuple[dict[str, Any], str]] = {
    "sequence_int": ({"name": "id", "type": "sequence", "start": 100, "step": 3}, "mixed"),
    "sequence_prefixed_str": (
        {"name": "id", "type": "sequence", "start": 1, "prefix": "P", "pad_length": 5},
        "mixed",
    ),
    "categorical_str": (STR_PARENT[0], "mixed"),
    "categorical_int": (INT_PARENT[0], "mixed"),
    "faker_uuid_str": ({"name": "id", "type": "faker", "faker_type": "uuid4"}, "mixed"),
    "faker_pyint": ({"name": "id", "type": "faker", "faker_type": "pyint"}, "mixed"),
    "formula_int": ({"name": "id", "type": "formula", "formula": "i * 3"}, "mixed"),
    "faker_date_of_birth": (
        {"name": "id", "type": "faker", "faker_type": "date_of_birth"},
        "mixed",
    ),
}


@pytest.mark.parametrize("family", sorted(KEY_FAMILIES))
def test_family_as_the_coupled_fk_parent_key(
    tmp_path: Path, family: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    column, expect = KEY_FAMILIES[family]
    ids = _int_ids(tmp_path, [column])
    keys = pa.array([ids[i % len(ids)] for i in range(N)])
    config, child = _family_job(tmp_path, [column], coupled=True, keys=keys)
    _, got = _compare(
        f"gen-coupled-key:{family}",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect=expect,
    )
    if got.route == "mixed":
        _assert_native_witness(f"gen-coupled-key:{family}", got)
        assert set(_strs(got.result, CHILD, FK)) <= set(_strs(got.result, PARENT, "id"))


# ---------------------------------------------------------------------------
# Dirty generation shapes: clean fallback, no regeneration
# ---------------------------------------------------------------------------

DIRTY: dict[str, list[dict[str, Any]]] = {
    "null_probability": [
        {"name": "tier", "type": "categorical", "categories": ["A", "B"], "null_probability": 0.5}
    ],
    "all_null": [
        {"name": "tier", "type": "categorical", "categories": ["A", "B"], "null_probability": 1.0}
    ],
    "none_category": [
        {"name": "tier", "type": "categorical", "categories": ["A", None], "weights": [0, 1]}
    ],
    "nan_category": [
        {"name": "amt", "type": "categorical", "categories": [float("nan"), 1.0], "weights": [1, 0]}
    ],
    "nested_category": [{"name": "tags", "type": "categorical", "categories": [[1, 2], [3, 4]]}],
    "faker_with_nulls": [
        {"name": "who", "type": "faker", "faker_type": "last_name", "null_probability": 0.3}
    ],
    "sequence_with_nulls": [{"name": "n", "type": "sequence", "start": 1, "null_probability": 0.4}],
}


@pytest.mark.parametrize("dirt", sorted(DIRTY))
@pytest.mark.parametrize("topology", ["independent", "coupled_parent"])
def test_dirty_non_key_column_falls_back_to_full_frame_without_regenerating(
    tmp_path: Path,
    topology: str,
    dirt: str,
    static_reasons: list[str],
    generate_calls: list[int],
) -> None:
    if topology == "independent":
        config, child = _family_job(tmp_path, DIRTY[dirt], coupled=False)
    else:
        parent = [{"name": "id", "type": "sequence", "start": 0, "step": 1}, *DIRTY[dirt]]
        keys = pa.array([i % PARENT_ROWS for i in range(N)], pa.int64())
        config, child = _family_job(tmp_path, parent, coupled=True, keys=keys)
    want, got = _compare(
        f"dirty-{topology}:{dirt}", config, {CHILD: child}, static_reasons, generate_calls
    )
    assert got.route.startswith("declined:mixed_runtime_declined:"), got.route
    assert got.calls == 1  # the fallback reuses the generated outputs
    # A decline happens before any sink write.
    sink = RecordingSink()
    run(config, {CHILD: child}, sink=sink)
    assert sink.calls == []


# ---------------------------------------------------------------------------
# Mask strategies
# ---------------------------------------------------------------------------

DATES = [f"19{60 + i % 30}-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(N)]
GROUPS = [f"g{i % 5}" for i in range(N)]


@dataclass(frozen=True)
class MaskSpec:
    columns: list[dict[str, Any]]  # the strategy column is always named `v`
    data: dict[str, list[Any]]
    must_change: Callable[[Any], bool] | None = lambda v: True
    permutation: bool = False  # shuffle: same multiset, no per-row change guarantee
    native: bool = True  # False: the column has no compiled kernel and runs on the oracle leg
    # True: its warnings hold whole-table content (counts, row indices) that a chunk-by-chunk run
    # would compute chunk-locally, so mixed admission declines and the full-frame path answers.
    declines: bool = False
    # True: the planner's chunk gate refuses the strategy (not value-keyed, so its output is not
    # chunk-invariant), independent of warnings. It runs full-frame and equals the oracle.
    chunk_unsafe: bool = False


def _col(strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": "v", "strategy": strategy, **extra}


def _pass(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough"}


MASKS: dict[str, MaskSpec] = {
    "hash": MaskSpec(
        [_col("hash", namespace="ns_h")], {"v": [f"user{i}@example.com" for i in range(N)]}
    ),
    "redact": MaskSpec([_col("redact")], {"v": [f"secret-{i}" for i in range(N)]}),
    "truncate": MaskSpec(
        [_col("truncate", provider_config={"length": 3})], {"v": [f"abcdef{i}" for i in range(N)]}
    ),
    "bucketize": MaskSpec(
        [_col("bucketize", provider_config={"width": 10})],
        {"v": [i * 7 + 3 for i in range(N)]},
        must_change=lambda v: v % 10 != 0,  # a bucket's lower bound is its own label
        native=False,
    ),
    "top_code": MaskSpec(
        [_col("top_code", provider_config={"preset": "hipaa_age"})],
        {"v": [60 + i % 45 for i in range(N)]},
        must_change=lambda v: v >= 90,
        native=False,
        declines=True,
    ),
    "fpe": MaskSpec(
        [_col("fpe", namespace="ns_f", provider_config={"charset": "digits"})],
        {"v": [f"{100000 + i * 977:09d}" for i in range(N)]},
        declines=True,
    ),
    "text_mask": MaskSpec(
        [_col("text_mask", provider_config={"detectors": ["ssn"]})],
        {"v": [f"call, SSN {100 + i:03d}-45-{1000 + i:04d} thanks" for i in range(N)]},
        declines=True,
    ),
    "text_redact": MaskSpec(
        [_col("text_redact")],
        {"v": [f"mail user{i}@example.com or call 555-01{i % 100:02d}" for i in range(N)]},
        must_change=None,
    ),
    "geo_generalize": MaskSpec(
        [
            _col(
                "geo_generalize",
                provider_config={
                    "type": "zip",
                    "cascade": ["zip5", "zip3", "state", "suppress"],
                    "k_threshold": 20000,
                },
            )
        ],
        {"v": [f"{10000 + i * 137:05d}" for i in range(N)]},
        declines=True,
    ),
    "categorical": MaskSpec(
        [
            _col(
                "categorical",
                deterministic=True,
                namespace="ns_c",
                provider_config={"categories": ["x", "y", "z"]},
            )
        ],
        {"v": [["red", "green", "blue"][i % 3] for i in range(N)]},
    ),
    "shuffle": MaskSpec(
        [_col("shuffle", namespace="ns_s", deterministic=True)],
        {"v": [f"item-{i}" for i in range(N)]},
        must_change=None,
        permutation=True,
        chunk_unsafe=True,
    ),
    "code_set": MaskSpec(
        [_col("code_set", namespace="ns_cs", provider_config={"code_set": "icd10"})],
        {"v": [["E11.9", "I10", "J45.909", "M54.5"][i % 4] for i in range(N)]},
        must_change=None,
        native=False,
    ),
    "bucket_perturb": MaskSpec(
        [
            _col(
                "bucket_perturb",
                namespace="ns_bp",
                provider_config={"date_format": "%Y-%m-%d", "bucket": "month"},
            )
        ],
        {"v": DATES},
        must_change=None,
    ),
    "formula": MaskSpec(
        [_col("formula", provider_config={"formula": "value"})],
        {"v": [f"f{i}" for i in range(N)]},
        must_change=None,
        chunk_unsafe=True,
    ),
    "date_shift": MaskSpec(
        [
            _col(
                "date_shift",
                namespace="ns_ds",
                provider_config={"min_days": 30, "max_days": 60, "date_format": "%Y-%m-%d"},
            )
        ],
        {"v": DATES},
    ),
    "group_key": MaskSpec(
        [_col("group_key", provider_config={"group_by": "g"}), _pass("g")],
        {"v": ["a"] * N, "g": GROUPS},
        must_change=None,
    ),
}

TOPOLOGIES = ["independent", "coupled_int", "coupled_str"]


def _mask_job(
    tmp_path: Path, spec: MaskSpec, topology: str, *, policy: str = "preserve"
) -> tuple[dict[str, Any], pa.Table]:
    keys_by_topology: dict[str, list[Any]] = {
        "independent": STR_KEYS,
        "coupled_int": INT_KEYS,
        "coupled_str": STR_KEYS,
    }
    keys = keys_by_topology[topology]
    arrays = {FK: _key_array(keys)}
    arrays.update({name: pa.array(values) for name, values in spec.data.items()})
    child = pa.table(arrays)
    parent = {"independent": None, "coupled_int": INT_PARENT, "coupled_str": STR_PARENT}[topology]
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=parent,
        parent_rows=PARENT_ROWS,
        child_columns=spec.columns,
        coupled=topology != "independent",
        policy=policy,
    )
    return config, child


@pytest.mark.parametrize("topology", TOPOLOGIES)
@pytest.mark.parametrize("strategy", sorted(MASKS))
def test_mask_strategy_matches_the_oracle_in_every_topology(
    tmp_path: Path,
    strategy: str,
    topology: str,
    static_reasons: list[str],
    generate_calls: list[int],
) -> None:
    spec = MASKS[strategy]
    config, child = _mask_job(tmp_path, spec, topology)
    case = f"mask:{strategy}/{topology}"
    _, got = _compare(case, config, {CHILD: child}, static_reasons, generate_calls)
    assert got.error is None
    if spec.declines:
        # Fail closed: warnings that depend on the whole table cannot be reproduced chunk by
        # chunk, so the column runs full-frame. Output AND warnings equal the oracle's (the
        # `_compare` call above asserted both); the route proves it was the decline.
        reason = f"declined:mask_strategy_warnings_not_chunk_stable:{CHILD}.v:{strategy}"
        assert got.route == reason, got.route
    elif spec.chunk_unsafe:
        want_prefix = f"declined:mask_table_not_chunk_admissible:{CHILD}:strategy_not_chunk_safe"
        assert got.route.startswith(want_prefix), got.route
    elif spec.native:
        _assert_native_witness(case, got)  # asserts the route is mixed
    else:
        # No compiled kernel: the table is admitted and routes mixed, but the mask half runs on
        # the pandas oracle leg. Pin both, so a fallback to full-frame cannot pass silently.
        assert got.route == "mixed", f"{case}: expected the mixed route, got {got.route}"
        backends = _backends(got.result)
        assert "pandas_oracle" in backends.values(), (case, backends)
        MATRIX[-1]["backends"] = sorted(set(backends.values()))
    source = child.column("v").to_pylist()
    if spec.permutation:
        assert sorted(map(str, _strs(got.result, CHILD, "v"))) == sorted(map(str, source))
    elif spec.must_change is not None:
        _no_raw_survives(case, got, source, "v", must_change=spec.must_change)
    else:
        # No per-row guarantee, but a masking strategy that changes nothing is a leak.
        out = _strs(got.result, CHILD, "v")
        assert any(str(o) != str(s) for o, s in zip(out, source, strict=True)) or (
            strategy == "formula"
        ), f"{case}: output equals raw source for every row"
    if topology != "independent":
        parent_ids = set(_strs(got.result, PARENT, "id"))
        assert set(_strs(got.result, CHILD, FK)) <= parent_ids  # FK integrity (no orphans)


def test_parent_covers_the_child_keys(tmp_path: Path) -> None:
    """The coupled topologies assume a parent holding every referenced key (see PARENT_ROWS)."""
    from decoy_engine.generation._plan_entry import generate_tables
    from tests.parity._fr0_support import plan_of

    for parent, keys in ((STR_PARENT, STR_KEYS), (INT_PARENT, INT_KEYS)):
        child = child_table(_key_array(keys))
        config = mixed_config(tmp_path, child, parent_columns=parent, parent_rows=PARENT_ROWS)
        ids = set(generate_tables(plan_of(config))[PARENT].column("id").to_pylist())
        assert set(keys) <= ids


# ---------------------------------------------------------------------------
# Count-carrying warnings: declined to full-frame (cross-chunk aggregation is deferred)
# ---------------------------------------------------------------------------

PREFIXED = [f"M{100000 + i * 977:06d}" for i in range(N)]
ZIP_TEXT = [f"home zip {10000 + i * 7:05d} on file" for i in range(N)]
COUNT_WARNINGS = {
    "fpe": (
        {
            "name": "v",
            "strategy": "fpe",
            "namespace": "ns_f",
            "provider_config": {"charset": "digits", "preserve_separators": True},
        },
        PREFIXED,
        "fpe_partial_plaintext_disclosure",
    ),
    "text_mask": (
        {
            "name": "v",
            "strategy": "text_mask",
            "provider_config": {
                "detectors": ["us_zip"],
                "sub_floor_span": "redact",
                "unmatched_span_policy": "passthrough",
            },
        },
        ZIP_TEXT,
        "text_mask_sub_floor_span_handled",
    ),
}
# A hash sibling keeps the table on the compiled route; a bucketize sibling (no operator)
# sends the whole table to the per-chunk oracle leg. Both decline: the warning counts are
# per-frame, so a chunked run cannot reproduce the oracle's one global warning.
SIBLINGS = {
    "native_table": {"name": "w", "strategy": "hash", "namespace": "ns_w"},
    "oracle_leg_table": {
        "name": "w",
        "strategy": "bucketize",
        "provider_config": {"width": 10},
    },
}


@pytest.mark.parametrize("topology", TOPOLOGIES)
@pytest.mark.parametrize("sibling", sorted(SIBLINGS))
@pytest.mark.parametrize("strategy", sorted(COUNT_WARNINGS))
def test_count_warning_strategies_decline_with_the_oracles_full_frame_warnings(
    tmp_path: Path,
    strategy: str,
    sibling: str,
    topology: str,
    static_reasons: list[str],
    generate_calls: list[int],
) -> None:
    column, values, code = COUNT_WARNINGS[strategy]
    w_values: list[Any] = (
        [f"x{i}@example.com" for i in range(N)] if sibling == "native_table" else list(range(N))
    )
    spec = MaskSpec([column, SIBLINGS[sibling]], {"v": values, "w": w_values})
    config, child = _mask_job(tmp_path, spec, topology)
    case = f"count-warning:{strategy}/{sibling}/{topology}"
    want, got = _compare(case, config, {CHILD: child}, static_reasons, generate_calls)
    assert got.route == (
        f"declined:mask_strategy_warnings_not_chunk_stable:{CHILD}.v:{strategy}"
    ), got.route
    oracle_warnings = [w for w in want.result.warnings if w.code == code]
    assert len(oracle_warnings) == 1  # the data really triggers it, once, over the whole table
    assert [w for w in got.result.warnings if w.code == code] == oracle_warnings


# ---------------------------------------------------------------------------
# Fail-closed siblings and shapes
# ---------------------------------------------------------------------------


def _email_note_job(
    tmp_path: Path, columns: list[dict[str, Any]], extra: dict[str, list[Any]], topology: str
) -> tuple[dict[str, Any], pa.Table]:
    keys = STR_KEYS if topology == "coupled_str" else INT_KEYS
    arrays = {
        FK: _key_array(keys),
        "email": pa.array([f"u{i}@example.com" for i in range(N)]),
        "note": pa.array([f"note-{i}" for i in range(N)]),
        **{k: pa.array(v) for k, v in extra.items()},
    }
    child = pa.table(arrays)
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=STR_PARENT if topology == "coupled_str" else INT_PARENT,
        parent_rows=PARENT_ROWS,
        child_columns=columns,
    )
    return config, child


TIER = ["gold" if i % 3 == 0 else "basic" for i in range(N)]


@pytest.mark.parametrize("topology", ["coupled_int", "coupled_str"])
def test_when_predicate_on_a_non_key_sibling_routes_mixed_and_masks_the_gated_rows(
    tmp_path: Path, topology: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    columns = [
        {"name": "email", "strategy": "hash", "namespace": "ns_e", "when": "tier == 'gold'"},
        {"name": "note", "strategy": "redact"},
        _pass("tier"),
    ]
    config, child = _email_note_job(tmp_path, columns, {"tier": TIER}, topology)
    case = f"sibling-when:non-fk/{topology}"
    _, got = _compare(case, config, {CHILD: child}, static_reasons, generate_calls)
    out = _strs(got.result, CHILD, "email")
    src = child.column("email").to_pylist()
    for tier, o, s in zip(TIER, out, src, strict=True):
        assert (o != s) if tier == "gold" else (o == s)  # only the gated rows change
    MATRIX[-1]["raw_leaks"] = 0


@pytest.mark.parametrize("topology", ["coupled_int", "coupled_str"])
def test_when_predicate_reading_the_fk_key_declines_and_masks_every_gated_row(
    tmp_path: Path, topology: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    gate = f"{FK} == 10" if topology == "coupled_int" else f"{FK} == 'k0'"
    columns = [
        {"name": "email", "strategy": "hash", "namespace": "ns_e", "when": gate},
        {"name": "note", "strategy": "redact"},
    ]
    config, child = _email_note_job(tmp_path, columns, {}, topology)
    case = f"sibling-when:reads-fk/{topology}"
    _, got = _compare(
        case,
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="declined:fk_key_column_read_by_sibling",
    )
    keys = child.column(FK).to_pylist()
    want_gate = 10 if topology == "coupled_int" else "k0"
    out = _strs(got.result, CHILD, "email")
    src = child.column("email").to_pylist()
    for k, o, s in zip(keys, out, src, strict=True):
        if k == want_gate:
            assert o != s, "a gated row kept its raw email"
    MATRIX[-1]["raw_leaks"] = 0


@pytest.mark.parametrize("topology", ["independent", "coupled_str"])
def test_coherent_with_composite_group(
    tmp_path: Path, topology: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    names = ("first_name", "last_name", "email")

    def member(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "composite_name_email",
            "namespace": "ns_person",
            "coherent_with": [n for n in names if n != name],
        }

    child = pa.table(
        {
            FK: pa.array(STR_KEYS),
            "first_name": pa.array([f"First{i}" for i in range(N)]),
            "last_name": pa.array([f"Last{i}" for i in range(N)]),
            "email": pa.array([f"first{i}.last{i}@example.com" for i in range(N)]),
        }
    )
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=None if topology == "independent" else STR_PARENT,
        parent_rows=PARENT_ROWS,
        child_columns=[member(n) for n in names],
        coupled=topology != "independent",
    )
    case = f"coherent_with-composite/{topology}"
    _, got = _compare(case, config, {CHILD: child}, static_reasons, generate_calls)
    _no_raw_survives(case, got, child.column("email").to_pylist(), "email")


def test_composite_fk_declines_and_matches_the_oracle(
    tmp_path: Path, static_reasons: list[str], generate_calls: list[int]
) -> None:
    child = pa.table(
        {
            FK: pa.array(STR_KEYS),
            "person_id2": pa.array([str(i % 3) for i in range(N)]),
            "email": pa.array([f"u{i}@x.com" for i in range(N)]),
        }
    )
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[
            {"name": "id", "type": "categorical", "categories": ["k0", "k1", "k2", "k3"]},
            {"name": "id2", "type": "categorical", "categories": ["0", "1", "2"]},
        ],
        child_columns=[
            {"name": "person_id2", "strategy": "passthrough"},
            {"name": "email", "strategy": "hash", "namespace": "ns_e"},
        ],
    )
    config["relationships"][0]["parent"]["columns"] = ["id", "id2"]
    config["relationships"][0]["children"][0]["columns"] = [FK, "person_id2"]
    config = PipelineConfig.model_validate(config).model_dump()
    _compare(
        "composite-key",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="declined:mixed-fk-cross-generate-unsupported",
    )


# ---------------------------------------------------------------------------
# Orphan policies, nullability, big ints
# ---------------------------------------------------------------------------


def _orphan_keys(kind: str, orphans: int) -> pa.Array:
    base = INT_KEYS if kind == "int" else STR_KEYS
    bad = [99, 98, 97, 96] if kind == "int" else ["zz0", "zz1", "zz2", "zz3"]
    keys = [*base[: N - orphans], *bad[:orphans]]
    return pa.array(keys, pa.int64() if kind == "int" else pa.string())


def _policy_job(tmp_path: Path, kind: str, policy: str, orphans: int) -> tuple[dict[str, Any], Any]:
    child = child_table(_orphan_keys(kind, orphans))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=INT_PARENT if kind == "int" else STR_PARENT,
        parent_rows=PARENT_ROWS,
        policy=policy,
    )
    return config, child


@pytest.mark.parametrize("kind", ["int", "str"])
def test_preserve_keeps_orphans_as_is(
    tmp_path: Path, kind: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _policy_job(tmp_path, kind, "preserve", 4)
    _, got = _compare(
        f"policy:preserve+orphans/{kind}",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="mixed",
    )
    _assert_native_witness(f"policy:preserve+orphans/{kind}", got)
    out = _strs(got.result, CHILD, FK)
    assert out[-4:] == _orphan_keys(kind, 4).to_pylist()[-4:]


@pytest.mark.parametrize("kind", ["int", "str"])
def test_warn_emits_exactly_one_edge_warning_with_the_oracle_count(
    tmp_path: Path, kind: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _policy_job(tmp_path, kind, "warn", 4)
    want, got = _compare(
        f"policy:warn/{kind}",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="mixed",
    )
    orphan = [w for w in got.result.warnings if w.code == "orphan_fk"]
    assert len(orphan) == 1
    assert orphan[0].detail["orphan_rows"] == 4
    assert [w.detail for w in orphan] == [
        w.detail for w in want.result.warnings if w.code == "orphan_fk"
    ]


@pytest.mark.parametrize("kind", ["int", "str"])
def test_fail_raises_the_oracle_error_and_publishes_nothing(
    tmp_path: Path, kind: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _policy_job(tmp_path, kind, "fail", 1)
    want, got = _compare(
        f"policy:fail/{kind}", config, {CHILD: child}, static_reasons, generate_calls
    )
    assert got.error is not None and got.error.code == "orphan_fk_violation"
    sink = RecordingSink()
    with pytest.raises(ExecutionError) as err:
        run(config, {CHILD: child}, sink=sink)
    assert err.value.code == "orphan_fk_violation"
    assert sink.aborted == 1 and sink.committed == 0 and sink.published is None
    assert not [c for c in sink.calls if c[0].startswith("write")]


@pytest.mark.parametrize("kind", ["int", "str"])
def test_remap_with_only_matched_keys_routes_mixed(
    tmp_path: Path, kind: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _policy_job(tmp_path, kind, "remap", 0)
    _, got = _compare(
        f"policy:remap-matched/{kind}",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="mixed",
    )
    _assert_native_witness(f"policy:remap-matched/{kind}", got)


@pytest.mark.parametrize("kind", ["int", "str"])
def test_remap_with_an_orphan_raises_on_both_sides(
    tmp_path: Path, kind: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, child = _policy_job(tmp_path, kind, "remap", 1)
    want, got = _compare(
        f"policy:remap+orphan/{kind}", config, {CHILD: child}, static_reasons, generate_calls
    )
    assert want.error is not None and want.error.code == "orphan_remap_parent_missing"
    assert got.error is not None and got.error.code == "orphan_remap_parent_missing"


def test_nullable_string_fk_key_routes_mixed(
    tmp_path: Path, static_reasons: list[str], generate_calls: list[int]
) -> None:
    keys = [None if i % 9 == 4 else STR_KEYS[i] for i in range(N)]
    child = child_table(pa.array(keys, pa.string()))
    config = mixed_config(tmp_path, child, parent_columns=STR_PARENT, parent_rows=PARENT_ROWS)
    _, got = _compare(
        "nullable-key:string",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="mixed",
    )
    _assert_native_witness("nullable-key:string", got)
    out = _strs(got.result, CHILD, FK)
    assert [i for i, k in enumerate(out) if k is None] == [i for i in range(N) if i % 9 == 4]


def test_nullable_int_fk_key_declines_to_full_frame(
    tmp_path: Path, static_reasons: list[str], generate_calls: list[int]
) -> None:
    keys = [None if i % 7 == 3 else INT_KEYS[i] for i in range(N)]
    child = child_table(pa.array(keys, pa.int64()))
    config = mixed_config(tmp_path, child, parent_columns=INT_PARENT, parent_rows=PARENT_ROWS)
    _compare(
        "nullable-key:int",
        config,
        {CHILD: child},
        static_reasons,
        generate_calls,
        expect="declined:",
    )
    assert "not_chunk_admissible" in MATRIX[-1]["route"]


@pytest.mark.parametrize("policy", ["preserve", "warn"])
def test_big_int_child_keys_above_two_to_the_53_stay_exact(
    tmp_path: Path, policy: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    big = [2**53 + 1, 2**53 + 2, 2**53 + 3, 2**62 + 7]
    parent = [{"name": "id", "type": "categorical", "categories": big}]
    keys = pa.array([big[i % 4] for i in range(N - 2)] + [2**53 + 99, 2**63 - 1], pa.int64())
    child = child_table(keys)
    config = mixed_config(
        tmp_path, child, parent_columns=parent, parent_rows=PARENT_ROWS, policy=policy
    )
    case = f"bigint-keys:{policy}"
    _, got = _compare(case, config, {CHILD: child}, static_reasons, generate_calls, expect="mixed")
    _assert_native_witness(case, got)
    out = _strs(got.result, CHILD, FK)
    assert out[-2:] == [2**53 + 99, 2**63 - 1]  # orphans keep their exact value
    assert set(out[:-2]) <= set(_strs(got.result, PARENT, "id"))


# ---------------------------------------------------------------------------
# Multi-table jobs
# ---------------------------------------------------------------------------


def _add_mask_table(
    tmp_path: Path,
    config: dict[str, Any],
    name: str,
    columns: list[dict[str, Any]],
    table: pa.Table,
) -> dict[str, Any]:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    config["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
    config["targets"][name] = {"type": "file", "format": "csv", "path": f"{name}.out.csv"}
    config["tables"].append({"name": name, "columns": columns})
    return PipelineConfig.model_validate(config).model_dump()


def _ledger() -> pa.Table:
    return pa.table(
        {
            "ref": pa.array([f"r{i}" for i in range(33)]),
            "ssn": pa.array([f"{100 + i:03d}-45-{1000 + i:04d}" for i in range(33)]),
            "amt": pa.array([f"{i * 7 + 3}.00" for i in range(33)]),
        }
    )


LEDGER_COLUMNS: list[dict[str, Any]] = [
    {"name": "ref", "strategy": "hash", "namespace": "ns_ref"},
    {"name": "ssn", "strategy": "hash", "namespace": "ns_ssn"},
    {"name": "amt", "strategy": "redact"},
]


def test_two_independent_mask_tables_with_different_strategies(
    tmp_path: Path, static_reasons: list[str], generate_calls: list[int]
) -> None:
    child = child_table(pa.array(STR_KEYS, pa.string()))
    config = mixed_config(tmp_path, child, coupled=False)
    ledger = _ledger()
    config = _add_mask_table(tmp_path, config, "ledger", LEDGER_COLUMNS, ledger)
    sources = {CHILD: child, "ledger": ledger}
    _, got = _compare("multi-mask:two-independent", config, sources, static_reasons, generate_calls)
    assert got.route == "mixed"
    _assert_native_witness("multi-mask:two-independent", got, CHILD)
    _assert_native_witness("multi-mask:two-independent", got, "ledger")
    assert not set(_strs(got.result, "ledger", "ref")) & set(ledger.column("ref").to_pylist())
    assert not set(_strs(got.result, "ledger", "ssn")) & set(ledger.column("ssn").to_pylist())


def test_coupled_and_independent_mask_tables_plus_a_second_generate_table(
    tmp_path: Path, static_reasons: list[str], generate_calls: list[int]
) -> None:
    child = child_table(pa.array(STR_KEYS, pa.string()))
    config = mixed_config(
        tmp_path, child, parent_columns=STR_PARENT, parent_rows=PARENT_ROWS, extra_generate=True
    )
    ledger = _ledger()
    config = _add_mask_table(tmp_path, config, "ledger", LEDGER_COLUMNS, ledger)
    sources = {CHILD: child, "ledger": ledger}
    case = "multi-mask:coupled+independent+2nd-generate"
    _, got = _compare(case, config, sources, static_reasons, generate_calls)
    assert got.route == "mixed"
    _assert_native_witness(case, got, CHILD)
    _assert_native_witness(case, got, "ledger")
    assert sorted(got.result.outputs) == sorted([PARENT, CHILD, "lookup", "ledger"])
    assert set(_strs(got.result, CHILD, FK)) <= set(_strs(got.result, PARENT, "id"))
    assert not set(_strs(got.result, "ledger", "ref")) & set(ledger.column("ref").to_pylist())


# ---------------------------------------------------------------------------
# Real transactional sink
# ---------------------------------------------------------------------------


def _sink_jobs(tmp_path: Path) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    jobs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for topology in TOPOLOGIES:
        for strategy in ("hash", "bucket_perturb", "date_shift"):
            sub = tmp_path / f"{strategy}-{topology}"
            sub.mkdir()
            config, child = _mask_job(sub, MASKS[strategy], topology)
            jobs[f"{strategy}/{topology}"] = (config, {CHILD: child})
    sub = tmp_path / "dirty"
    sub.mkdir()
    config, child = _family_job(sub, DIRTY["null_probability"], coupled=False)
    jobs["dirty-null/independent"] = (config, {CHILD: child})
    sub = tmp_path / "families"
    sub.mkdir()
    parent = [
        {"name": "id", "type": "sequence", "start": 0, "step": 1},
        *FAMILIES["faker_date_of_birth"],
        {"name": "n", "type": "formula", "formula": "randint(1, 50)"},
    ]
    keys = pa.array([i % PARENT_ROWS for i in range(N)], pa.int64())
    config, child = _family_job(sub, parent, coupled=True, keys=keys)
    jobs["faker-dob+formula/coupled"] = (config, {CHILD: child})
    sub = tmp_path / "multi"
    sub.mkdir()
    child = child_table(pa.array(STR_KEYS, pa.string()))
    config = mixed_config(sub, child, parent_columns=STR_PARENT, parent_rows=PARENT_ROWS)
    config = _add_mask_table(sub, config, "ledger", LEDGER_COLUMNS, _ledger())
    jobs["multi-mask/coupled"] = (config, {CHILD: child, "ledger": _ledger()})
    return jobs


SINK_CASES = [f"{s}/{t}" for t in TOPOLOGIES for s in ("hash", "bucket_perturb", "date_shift")] + [
    "dirty-null/independent",
    "faker-dob+formula/coupled",
    "multi-mask/coupled",
]


@pytest.mark.parametrize("case", SINK_CASES)
def test_real_parquet_transactional_sink_publishes_everything_atomically(
    tmp_path: Path, case: str, static_reasons: list[str], generate_calls: list[int]
) -> None:
    config, sources = _sink_jobs(tmp_path)[case]
    want = oracle(config, sources)
    target = tmp_path / "published"
    static_reasons.clear()
    got = run(config, sources, sink=ParquetTransactionalSink(target))
    route = _route(got, static_reasons)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("_decoy_stage_")]
    if route == "mixed":
        published = {p.stem: pq.read_table(p) for p in sorted(target.glob("*.parquet"))}
        assert sorted(published) == sorted(want.outputs), (sorted(published), sorted(want.outputs))
        assert_outputs_identical(published, want.outputs)
        for name, table in want.outputs.items():
            assert ipc_bytes(published[name]) == ipc_bytes(table), name
        assert got.outputs == {}
    else:
        # A declined job returns its outputs in memory and publishes nothing itself.
        assert not target.exists()
        assert_outputs_identical(got.outputs, want.outputs)
    MATRIX.append({"case": f"sink:{case}", "route": route, "byte_identical": True})
    if case.startswith("dirty"):
        assert route.startswith("declined:mixed_runtime_declined:")
    else:
        assert route == "mixed", route
