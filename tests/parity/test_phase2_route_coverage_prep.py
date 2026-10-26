"""Phase 2 route-coverage: prep acceptance stubs (fail-before), landed ahead of the build.

These pin the admission contract each phase-2 slice flips, at the out-of-core compat gate
(`out_of_core_admission`), the same gate `test_phase_d_fpe_key_route.py` reads for today's
declined behaviour. They are landed in "prep only" mode: the gated phase-2 plan is not yet in
the checkout (see docs/records/2026-10-26-phase2-fk-key-mask-seam-inventory.md), so the slice
contracts here are xfail(strict). They fail today and will XPASS once the slice lands, at which
point strict xfail fails the build until the implementer removes the marker and keeps the
assertion as the slice's real acceptance test. The full byte-parity oracle tests the handoff
names are authored by each slice's builder against the plan's fixtures; the parity test below
is the out-of-core-vs-full-frame shell they fill in.

The categorical guard is a standing invariant, not a stub: categorical is not injective, so it
must never become an FK key transform. It passes today and must keep passing across phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ConfigError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import edge, graph, make_plan, passthrough, table_seed
from tests.parity._d2c_fpe_data import fpe_key_shape

_PARENT_UNSUPPORTED = "out_of_core_parent_strategy_unsupported"
_ADMITTED: tuple[bool, None] = (True, None)
_HELD = (
    "Held pending the gated plan; see docs/records/2026-10-26-phase2-fk-key-mask-seam-inventory.md"
)


# --- Slice 5: FPE FK key on the out-of-core route -------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="phase-2 slice 5 unbuilt: fpe is declined as an out-of-core FK key today "
    "(the compat gate restricts FK keys to the column-independent set). Slice 5 admits it "
    "and threads the column name through the four FK key-mask callsites; remove this marker then.",
)
def test_slice5_admission_admits_fpe_parent_key_on_ooc_route() -> None:
    """The out-of-core compat gate must admit an fpe parent FK key. Today it declines it."""
    shape = fpe_key_shape(OrphanPolicy.PRESERVE)
    assert out_of_core_admission(shape.plan, registry=s.REGISTRY, graph=shape.graph) == _ADMITTED


_PARENT_KEYS = ["1000000001", "1000000002", "1000000003"]
_CHILD_KEYS = ["1000000001", "1000000002", "9999999999"]  # last row is a genuine orphan


def _fk_key_config(tmp_path: Path, strategy: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """A one-hop P.k -> C.k FK job whose key column uses `strategy` (namespaced)."""
    parent = pa.table({"k": _PARENT_KEYS})
    child = pa.table({"k": _CHILD_KEYS})
    for name, table in (("P", parent), ("C", child)):
        pq.write_table(table, tmp_path / f"{name}.parquet")
    config: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "phase2-fk-key-prep", "seed": 7},
        "sources": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "targets": {
            t: {"type": "file", "path": str(tmp_path / f"{t}.out.parquet"), "format": "parquet"}
            for t in ("P", "C")
        },
        "tables": [
            {"name": "P", "columns": [{"name": "k", "strategy": strategy, "namespace": "ns_k"}]},
            {"name": "C", "columns": [{"name": "k", "strategy": strategy, "namespace": "ns_k"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "P", "columns": ["k"]},
                "children": [{"table": "C", "columns": ["k"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_k",
            }
        ],
    }
    return config, {"P": parent, "C": child}


def _run(tmp_path: Path, config: dict[str, Any], sources: dict[str, pa.Table], **kw: Any) -> Any:
    return run_pipeline(
        config, sources, engine_version="0.1.0", out_of_core_budget_bytes=1 << 30, **kw
    )


@pytest.mark.xfail(
    strict=True,
    raises=ConfigError,
    reason="phase-2 slice 5 unbuilt: explicit out_of_core on an fpe-key job fails closed today "
    "with out_of_core_parent_strategy_unsupported. Slice 5 lets it run; remove this marker then.",
)
def test_slice5_ooc_route_matches_full_frame_oracle_for_fpe_key(tmp_path: Path) -> None:
    """The out-of-core fpe-key output must equal the full-frame oracle, byte for byte."""
    config, sources = _fk_key_config(tmp_path, "fpe")
    ooc = _run(tmp_path, config, sources, execution_mode="out_of_core")
    full = _run(tmp_path, config, sources, execution_mode="full_frame")
    for table in ("P", "C"):
        assert sc.compare_strict(full.outputs[table], ooc.outputs[table]) == dict.fromkeys(
            sc.DIMENSIONS, True
        )


# --- Standing invariant: categorical stays OFF the FK key surface ---------------------------


def _categorical_key_plan_and_graph() -> tuple[Any, Any]:
    def cat(namespace: str) -> Any:
        return column_seed("categorical", namespace=namespace)

    plan = make_plan(
        {
            "A": table_seed({"k": cat("ns_ab"), "n": passthrough()}),
            "B": table_seed({"k": cat("ns_bc")}),
            "C": table_seed({"k": cat("ns_bc")}),
        }
    )
    g = graph(
        edge("A", "k", "B", "k", "ns_ab", OrphanPolicy.PRESERVE),
        edge("B", "k", "C", "k", "ns_bc", OrphanPolicy.PRESERVE),
    )
    return plan, g


def test_categorical_stays_declined_on_ooc_fk_key_surface() -> None:
    """categorical is not injective: it must never be admitted as an FK key transform.

    This is a standing guard, not a fail-before stub. It passes today and must keep passing
    across every phase-2 slice; phase 2 must not broaden the key surface to categorical.
    """
    plan, g = _categorical_key_plan_and_graph()
    assert out_of_core_admission(plan, registry=s.REGISTRY, graph=g) == (
        False,
        _PARENT_UNSUPPORTED,
    )


# --- Held slices (design-dependent; contracts live in the gated plan) ------------------------


@pytest.mark.skip(
    reason="phase-2 slice 4 (OOC address faker): faker-key-vs-payload shape is plan-dependent "
    "and no faker shape helper exists under tests/parity/_d2*. " + _HELD
)
def test_slice4_ooc_address_faker_parity_vs_oracle() -> (
    None
): ...  # authored by the slice-4 builder against the plan's fixtures


@pytest.mark.skip(
    reason="phase-2 slice 6 (cascade, key-column-only): the quarantine/cascade bound and "
    "mechanism are the plan's. " + _HELD
)
def test_slice6_cascade_is_key_column_only() -> None: ...


@pytest.mark.skip(
    reason="phase-2 single-column resident escape hatch: prereq-gated on phase-1 B3/B4, which "
    "are not on main or any reachable branch. " + _HELD
)
def test_single_column_resident_escape_hatch() -> None: ...


@pytest.mark.skip(
    reason="phase-2 out-of-core quarantine track: depends on the slice 6 cascade design. " + _HELD
)
def test_ooc_quarantine_track() -> None: ...
