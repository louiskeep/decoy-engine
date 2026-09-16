"""Task 4.6 slice 4: Group B/C payload strategies through the unified
coordinator's OOC dispatch, in the SHADOW test path (docs/plans/task46-
slice4-groupbc-shadow-plan.md). Slice 3 already proved the compiler assigns
`DriverId.OUT_OF_CORE` for a compiled FK job and the `ShadowCoordinator`
dispatches it through `OutOfCoreAdapter` -> `run_fk_out_of_core`; this file
widens that proof from the Group A key strategies slice 3 fixtures used to
the Group B/C PAYLOAD strategies `_compat.py` already admits (`fpe`,
`text_redact`, `categorical`, `text_mask`, `code_set`, `bucket_perturb`),
exercised across REAL batch boundaries (`batch_size_rows=1` over a multi-row
child), and proves the coordinator's `quality_metrics` forwarding fix (see
`_shadow_coordinator.py`'s `ShadowRunResult.quality_metrics`) carries
code_set's corpus-provenance evidence through the adaptation.

DOES NOT re-prove `run_fk_out_of_core`'s own Group B/C parity: `tests/
parity/test_out_of_core_group_b_parity.py` and `_group_c_parity.py` already
pin that (byte-for-byte vs the pandas oracle, including the deferred/wrong-
shape/parent-key MISS surface this file's fixtures mirror). This file rides
on that and adds the compile+dispatch+adaptation link through the shadow
coordinator, the same relationship slice 3's FK fixtures have to the FK
route itself.

SHADOW-ONLY, like every file in this package: no production caller, no
default flip.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import PhysicalPlan
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.generation.pool._events import QualityWarning
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_ooc_shadow_matches_oracle,
    run_shadow_and_oracle,
)

# ---------------------------------------------------------------------------
# Config-backed FK fixture builders (mirrors test_shadow_ooc_fk.py's _write /
# _fk_config).
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    path.chmod(0o444)  # C5 read-only discipline: an accidental in-test rewrite is impossible
    return path


def _fk_config(
    tmp_path: Path,
    tables: dict[str, tuple[pa.Table, list[dict[str, Any]]]],
    relationships: list[dict[str, Any]],
    *,
    seed: int = 20260916,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {},
        "tables": [],
        "relationships": relationships,
    }
    for name, (source, columns) in tables.items():
        path = _write(tmp_path, source, name)
        raw["sources"][name] = {"type": "file", "format": "parquet", "path": str(path)}
        raw["targets"][name] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{name}.out.parquet"),
        }
        raw["tables"].append({"name": name, "columns": columns})
    return PipelineConfig.model_validate(raw).model_dump()


_SINGLE_EDGE_RELATIONSHIP = [
    {
        "parent": {"table": "parent", "columns": ["pk"]},
        "children": [{"table": "child", "columns": ["fk"]}],
        "orphan_policy": "preserve",
        "namespace": "ns_fk",
    }
]

# `use_byte_estimate_routing=False` + `out_of_core_threshold_rows=1` forces
# OUT_OF_CORE on any nonempty, pure-mask, OOC-compatible, acyclic FK fixture
# (see test_shadow_ooc_fk.py's own `_FORCE_OOC`, Codex-confirmed there).
_FORCE_OOC: dict[str, Any] = {"out_of_core_threshold_rows": 1, "use_byte_estimate_routing": False}


def _assert_all_ooc(plan: PhysicalPlan) -> None:
    assert plan.tables, "expected at least one mask table"
    for table in plan.tables:
        assert table.driver == DriverId.OUT_OF_CORE, (
            f"{table.table}: expected OUT_OF_CORE, got {table.driver} ({table.driver_reason})"
        )


def _assert_ooc_rejected(plan: PhysicalPlan, code: str) -> None:
    """Every mask table declined OUT_OF_CORE, and the declined lane's
    `rejected_alternatives` entry names the exact coded reason -- never just
    "not OUT_OF_CORE". Deliberately does not assert which driver the job
    fell back to (SEQUENTIAL today): the bounded contract is about OOC
    admission, not the specific fallback lane."""
    assert plan.tables, "expected at least one mask table"
    for table in plan.tables:
        assert table.driver != DriverId.OUT_OF_CORE, (
            f"{table.table}: expected a non-OUT_OF_CORE driver, got {table.driver}"
        )
        ooc_alternatives = [
            alt for alt in table.rejected_alternatives if alt.driver == DriverId.OUT_OF_CORE
        ]
        assert len(ooc_alternatives) == 1, (
            f"{table.table}: expected exactly one OUT_OF_CORE rejected_alternatives entry, "
            f"got {ooc_alternatives!r}"
        )
        assert ooc_alternatives[0].reason == code, (
            f"{table.table}: expected reject reason {code!r}, got {ooc_alternatives[0].reason!r}"
        )


def _payload_edge_sources(payload_vals: list[str | None]) -> dict[str, pa.Table]:
    """Parent + child, single passthrough-keyed edge, each carrying the SAME
    strategy on its own payload column (`pay` / `cpay`, child's REVERSED so
    the null lands on a different row on each side) -- the shape `tests/
    parity/test_out_of_core_group_b_parity.py` / `_group_c_parity.py` use for
    their own payload-column fixtures."""
    n = len(payload_vals)
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "pay": pa.array(payload_vals, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "cpay": pa.array(list(reversed(payload_vals)), type=pa.string()),
        }
    )
    return {"parent": parent, "child": child}


def _payload_edge_config(
    tmp_path: Path, sources: dict[str, pa.Table], payload_col_cfg: dict[str, Any]
) -> dict[str, Any]:
    # A fresh deep copy per column: the two calls below must not share the
    # SAME nested `provider_config` dict object across parent/child (or
    # across parametrized test invocations reusing a module-level fixture).
    return _fk_config(
        tmp_path,
        {
            "parent": (
                sources["parent"],
                [
                    {"name": "pk", "strategy": "passthrough"},
                    {"name": "pay", **copy.deepcopy(payload_col_cfg)},
                ],
            ),
            "child": (
                sources["child"],
                [
                    {"name": "fk", "strategy": "passthrough"},
                    {"name": "cpay", **copy.deepcopy(payload_col_cfg)},
                ],
            ),
        },
        _SINGLE_EDGE_RELATIONSHIP,
    )


# ---------------------------------------------------------------------------
# Admitted Group B/C payload cases (mirrors the `provider_config` shapes in
# `test_out_of_core_group_b_parity.py` / `_group_c_parity.py`). Each has >= 3
# rows including a null. `fpe` carries NO `fpe_join_group` -- the one
# documented warning divergence slice 3's module docstring names.
# ---------------------------------------------------------------------------

_PAYLOAD_CASES: dict[str, tuple[dict[str, Any], list[str | None]]] = {
    "fpe": (
        {"strategy": "fpe", "namespace": "pii", "provider_config": {"charset": "digits"}},
        ["123456789", "000111222", None, "099900", "420000", "7654321"],
    ),
    "text_redact": (
        {"strategy": "text_redact", "provider_config": {"token": "[X]"}},
        [
            "call me at 415-555-1234 today",
            "ssn 123-45-6789 on file",
            None,
            "no pii here at all",
            "email a@b.com please",
            "plain text",
        ],
    ),
    "categorical": (
        {
            "strategy": "categorical",
            "namespace": "cat",
            "deterministic": True,
            "provider_config": {"categories": ["A", "B", "C"]},
        },
        ["alpha", "beta", None, "gamma", "delta", "epsilon"],
    ),
    "text_mask": (
        {"strategy": "text_mask", "provider_config": {"token": "[X]"}},
        [
            "call me at 415-555-1234 today",
            "ssn 123-45-6789 on file",
            None,
            "email a@b.com please",
            "no pii here at all",
            "reach 202-555-0147 asap",
        ],
    ),
    "code_set": (
        {"strategy": "code_set", "namespace": "cs", "provider_config": {"code_set": "mcc"}},
        ["alpha", "beta", None, "gamma", "delta", "epsilon"],
    ),
    "bucket_perturb": (
        {
            "strategy": "bucket_perturb",
            "namespace": "bp",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        },
        ["2021-03-15", "2020-11-02", None, "2019-07-28", "2022-01-09", "2023-05-19"],
    ),
}


# ---------------------------------------------------------------------------
# The core proof: one payload-parity test per strategy, ALWAYS-RUN (OOC's
# shared Arrow kernel needs no compiled companion for these strategies),
# forced through `batch_size_rows=1` so the mask genuinely crosses batch
# boundaries -- the 50,000-row helper default would run each fixture as one
# batch and leave the batch-local property untested.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(_PAYLOAD_CASES))
def test_group_bc_payload_parity_through_coordinator(tmp_path: Path, kind: str) -> None:
    payload_col_cfg, payload_vals = _PAYLOAD_CASES[kind]
    sources = _payload_edge_sources(payload_vals)
    config = _payload_edge_config(tmp_path, sources, payload_col_cfg)

    run = run_shadow_and_oracle(config, sources=sources, batch_size_rows=1, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)


# ---------------------------------------------------------------------------
# code_set corpus-provenance: the HIGH-2 regression test. Fails if
# `_adapt_ooc_result` drops `quality_metrics` (the fix `_shadow_coordinator.
# py`'s `ShadowRunResult.quality_metrics` field + forwarding line makes),
# because `run.shadow.quality_metrics` would stay at its empty default and
# the assertions below would find nothing.
# ---------------------------------------------------------------------------


def test_code_set_corpora_provenance_survives_adaptation(tmp_path: Path) -> None:
    payload_col_cfg, payload_vals = _PAYLOAD_CASES["code_set"]
    sources = _payload_edge_sources(payload_vals)
    config = _payload_edge_config(tmp_path, sources, payload_col_cfg)

    run = run_shadow_and_oracle(config, sources=sources, batch_size_rows=1, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    shadow_corpora = run.shadow.quality_metrics.get("code_set_corpora")
    oracle_corpora = run.oracle.quality_metrics.get("code_set_corpora")
    assert shadow_corpora, "expected non-empty code_set_corpora provenance on the shadow side"
    assert oracle_corpora, "sanity: the oracle itself must stamp code_set_corpora for this fixture"

    def _by_table_column(entries: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        return {(e["table"], e["column"]): e["code_set"] for e in entries}

    assert _by_table_column(shadow_corpora) == _by_table_column(oracle_corpora)
    assert _by_table_column(shadow_corpora) == {("parent", "pay"): "mcc", ("child", "cpay"): "mcc"}


# ---------------------------------------------------------------------------
# sub_floor warning forwarding: a text_mask fixture whose spans genuinely
# trip the sub_floor path (a bare 5-digit ZIP is below the FF1 minimum
# admissible domain, 10**5 < 1,000,000; mirrors `test_out_of_core_group_c_
# parity.py`'s `test_text_mask_sub_floor_warning_parity_resident_route`),
# proving the coordinator forwards a NON-EMPTY warning with multiset parity,
# not just "zero on both sides" (which the generic comparator alone cannot
# distinguish from a silently-dropped diagnostic).
# ---------------------------------------------------------------------------


def test_text_mask_sub_floor_warning_forwarded_with_parity(tmp_path: Path) -> None:
    payload_col_cfg = {"strategy": "text_mask", "provider_config": {"sub_floor_span": "redact"}}
    payload_vals: list[str | None] = ["90210", "10001", None, "no pii here", "60601"]
    sources = _payload_edge_sources(payload_vals)
    config = _payload_edge_config(tmp_path, sources, payload_col_cfg)

    run = run_shadow_and_oracle(config, sources=sources, batch_size_rows=1, **_FORCE_OOC)

    _assert_all_ooc(run.plan)
    assert_ooc_shadow_matches_oracle(run)  # includes the warnings-multiset check

    sub_floor = [
        w
        for w in run.shadow.warnings
        if isinstance(w, QualityWarning) and w.code == "text_mask_sub_floor_span_handled"
    ]
    assert sub_floor, "expected a non-empty text_mask_sub_floor_span_handled warning"


# ---------------------------------------------------------------------------
# Bounded-contract COMPILE-LEVEL rejects (no shadow run): a rejected Group
# B/C payload leaves the coordinator's non-OOC scalar loop with unbound
# Group B/C nodes, so these assert against the compiled plan directly.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload_col_cfg", "payload_vals", "code"),
    [
        (
            {"strategy": "faker", "namespace": "dns", "provider": "person_full_name"},
            ["1", "2", "3"],
            "out_of_core_faker_pool_unsupported",
        ),
        (
            {"strategy": "bucketize", "namespace": "dns", "provider_config": {"width": 10}},
            ["1", "2", "3"],
            "out_of_core_row_error_strategy_unsupported",
        ),
    ],
    ids=["faker", "bucketize"],
)
def test_unadmitted_group_bc_payload_does_not_route_ooc(
    tmp_path: Path,
    payload_col_cfg: dict[str, Any],
    payload_vals: list[str | None],
    code: str,
) -> None:
    sources = _payload_edge_sources(payload_vals)
    config = _payload_edge_config(tmp_path, sources, payload_col_cfg)

    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)

    _assert_ooc_rejected(plan, code)


@pytest.mark.parametrize(
    ("payload_col_cfg", "payload_vals", "code"),
    [
        (
            {
                "strategy": "code_set",
                "namespace": "cs",
                "provider_config": {"code_set": "mcc", "mode": "gen"},
            },
            ["a", "b", "c"],
            "out_of_core_code_set_shape_unsupported",
        ),
        (
            {
                "strategy": "bucket_perturb",
                "namespace": "bp",
                "provider_config": {"bucket": "month"},  # no date_format -> autodetect
            },
            ["2021-03-15", "2020-11-02", "2019-07-28"],
            "out_of_core_bucket_perturb_autodetect_unsupported",
        ),
    ],
    ids=["code_set_gen_mode", "bucket_perturb_no_date_format"],
)
def test_wrong_shape_group_bc_payload_does_not_route_ooc(
    tmp_path: Path,
    payload_col_cfg: dict[str, Any],
    payload_vals: list[str | None],
    code: str,
) -> None:
    sources = _payload_edge_sources(payload_vals)
    config = _payload_edge_config(tmp_path, sources, payload_col_cfg)

    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)

    _assert_ooc_rejected(plan, code)


@pytest.mark.parametrize(
    ("key_col_cfg", "code"),
    [
        (
            {
                "strategy": "categorical",
                "namespace": "keyns",
                "deterministic": True,
                "provider_config": {"categories": ["A", "B", "C"]},
            },
            "out_of_core_parent_strategy_unsupported",
        ),
        (
            {"strategy": "text_mask", "provider_config": {"token": "[X]"}},
            "out_of_core_parent_strategy_unsupported",
        ),
    ],
    ids=["categorical_group_b", "text_mask_group_c"],
)
def test_group_bc_as_fk_parent_key_does_not_route_ooc(
    tmp_path: Path, key_col_cfg: dict[str, Any], code: str
) -> None:
    parent = pa.table({"pk": pa.array(["100", "200", "300"], type=pa.string())})
    child = pa.table({"fk": pa.array(["100", "200", "300"], type=pa.string())})
    sources = {"parent": parent, "child": child}
    config = _fk_config(
        tmp_path,
        {
            "parent": (parent, [{"name": "pk", **copy.deepcopy(key_col_cfg)}]),
            "child": (child, [{"name": "fk", "strategy": "passthrough"}]),
        },
        [
            {
                "parent": {"table": "parent", "columns": ["pk"]},
                "children": [{"table": "child", "columns": ["fk"]}],
                "orphan_policy": "preserve",
                # A Group B/C key that requires a namespace (e.g. deterministic
                # categorical) must have the relationship agree with it; one
                # that doesn't (text_mask) falls back to the module default.
                "namespace": key_col_cfg.get("namespace", "ns_fk"),
            }
        ],
    )

    inputs = capture_physical_plan_inputs(
        config, sources, engine_version=ENGINE_VERSION, **_FORCE_OOC
    )
    plan = compile_physical_plan(inputs)

    _assert_ooc_rejected(plan, code)
