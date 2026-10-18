"""Phase F FR3: the generator-to-algorithm inventory is true, complete and rendered.

`_fr3_inventory.SITES` classifies every generation selection site. This file proves the
classification is not prose: each row's claimed backend is what the executed-backend witness (or a
direct probe of the stream) observes, the rows the slice left alone have unmoved goldens, the
cardinality modes the table declines are unreachable from the generation core, and the doc lists
every row with the same decision.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.generation import _column_streams, _faker_pool, _kernel_select, _streamed
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation.pool import _sampler
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.providers import register_faker_provider, unregister_faker_provider
from tests.parity import _f0_fixtures as fx
from tests.parity import _f0_witness as w
from tests.parity import _fr0_refreeze as rf
from tests.parity import _fr3_inventory as inv
from tests.parity._fr0_support import prepared_config, run_resident

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "plans" / "2026-10-18-phase-f-fr3-selection-inventory.md"
_THRESHOLD = 90
_ROWS = 100
_TINY = 1e-12


def _companion() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


HOST_KERNEL = w.COMPILED_INDEX if _companion() else w.PYTHON_INDEX_REFERENCE


def _core_config() -> dict[str, Any]:
    t_cols: list[dict[str, Any]] = [
        {"name": "seq", "type": "sequence", "start": 1},
        {"name": "cat_uniform", "type": "categorical", "categories": ["a", "b", "c"]},
        {
            "name": "cat_weighted",
            "type": "categorical",
            "categories": ["a", "b"],
            "weights": [9, 1],
        },
        {
            "name": "cat_declined",
            "type": "categorical",
            "categories": ["x", "y", "z"],
            "weights": [1, _TINY, 2],
        },
        {"name": "pooled", "type": "faker", "faker_type": "city"},
        {"name": "live", "type": "faker", "faker_type": "email"},
        {
            "name": "masked",
            "type": "categorical",
            "categories": ["u", "v"],
            "null_probability": 0.3,
        },
    ]

    def ref(name: str, dist: str, **extra: Any) -> dict[str, Any]:
        return {
            "name": name,
            "type": "reference",
            "reference_table": "par",
            "reference_column": "pk",
            "distribution": dist,
            **extra,
        }

    child = [
        ref("ref_random", "random"),
        ref("ref_weighted", "weighted", weights=[1, 2, 3]),
        ref("ref_declined", "weighted", weights=[1, _TINY, 2]),
    ]
    parent = [{"name": "pk", "type": "sequence", "start": 1, "prefix": "K"}]
    return fx.config(
        [fx.table("t", _ROWS, t_cols), fx.table("child", 40, child), fx.table("par", 3, parent)]
    )


def _legacy_config() -> dict[str, Any]:
    """A formula column makes the whole plan ineligible, so the legacy path runs."""
    cols: list[dict[str, Any]] = [
        {"name": "cat", "type": "categorical", "categories": ["a", "b"], "weights": [3, 1]},
        {"name": "pooled", "type": "faker", "faker_type": "city"},
        {"name": "fm", "type": "formula", "formula": "randint(1, 1000)"},
    ]
    return fx.config([fx.table("lg", _ROWS, cols)])


def _observed(
    monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any]
) -> dict[tuple[str, str], w.ColumnReport]:
    monkeypatch.setattr(_faker_pool, "N_THRESHOLD", _THRESHOLD)
    return w.classify_columns(monkeypatch, lambda: fx.run_generate_tables(cfg))


def _label(report: w.ColumnReport) -> str:
    (only,) = report.selection
    return HOST_KERNEL_NAME if only == HOST_KERNEL else only


HOST_KERNEL_NAME = inv.KERNEL


# (site id) -> (table, column, expected sub-paths) probed through the core witness.
CORE_PROBES: dict[str, tuple[str, str]] = {
    "categorical.uniform": ("t", "cat_uniform"),
    "categorical.weighted": ("t", "cat_weighted"),
    "categorical.weighted_unrepresentable": ("t", "cat_declined"),
    "faker.pooled.reuse": ("t", "pooled"),
    "faker.per_row": ("t", "live"),
    "reference.random": ("child", "ref_random"),
    "reference.weighted": ("child", "ref_weighted"),
    "reference.weighted_unrepresentable": ("child", "ref_declined"),
}
LEGACY_PROBES: dict[str, tuple[str, str]] = {
    "legacy.whole_table": ("lg", "cat"),
    "python_expression_families": ("lg", "fm"),
}


def test_core_probes_observe_the_backend_each_row_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    reports = _observed(monkeypatch, _core_config())
    for sid, key in CORE_PROBES.items():
        assert _label(reports[key]) == inv.SITES_BY_ID[sid].backend, sid
    assert w.POOL_CONSTRUCTION in reports[("t", "pooled")].subpaths
    assert reports[("t", "seq")].selection == frozenset({w.NO_SELECTION})
    assert inv.SITES_BY_ID["sequence.arithmetic"].backend == inv.NO_SELECTION
    assert w.NULL_MASK in reports[("t", "masked")].subpaths
    assert inv.SITES_BY_ID["null_probability.mask"].backend == "null_mask"


def test_legacy_probes_keep_the_python_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not classify_plan(prepared_config(_legacy_config())).eligible
    reports = _observed(monkeypatch, _legacy_config())
    assert reports[("lg", "cat")].selection == frozenset({w.PYTHON_RNG})
    assert reports[("lg", "pooled")].selection == frozenset({w.NUMPY_SELECTION})
    assert reports[("lg", "fm")].selection == frozenset({w.FORMULA_EXPRESSION})
    assert inv.SITES_BY_ID["legacy.whole_table"].backend == inv.PYTHON_RNG
    assert inv.SITES_BY_ID["python_expression_families"].backend == inv.FORMULA


def test_custom_provider_declines_pooling_and_stays_a_live_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = fx.config([fx.table("cp", _ROWS, [{"name": "c", "type": "faker", "faker_type": "city"}])])
    register_faker_provider("city", lambda fake: "CUSTOM_CITY")
    try:
        reports = _observed(monkeypatch, cfg)
    finally:
        unregister_faker_provider("city")
    assert reports[("cp", "c")].selection == frozenset({w.LIVE_PROVIDER})
    assert inv.SITES_BY_ID["faker.custom_provider"].backend == inv.LIVE


def test_reference_sequential_is_index_arithmetic_not_a_draw() -> None:
    ctx = GenDeriveContext.for_column(None, {"name": "r", "type": "reference"}, 1)
    col = {"name": "r", "distribution": "sequential"}
    stream = _column_streams.ReferenceStream(col, ctx, ["a", "b", "c"])
    assert stream._indexer is None and stream._rng is None
    assert stream.next_batch(2, 5) == ["c", "a", "b", "c", "a"]
    assert inv.SITES_BY_ID["reference.sequential"].decision == inv.UNCHANGED


def test_weighted_reference_with_mismatched_weights_falls_back_to_the_uniform_kernel() -> None:
    ctx = GenDeriveContext.for_column(None, {"name": "r", "type": "reference"}, 1)
    col = {"name": "r", "distribution": "weighted", "weights": [1, 2]}
    stream = _column_streams.ReferenceStream(col, ctx, ["a", "b", "c"])
    assert isinstance(stream._indexer, _kernel_select.PositionalIndexer)


def test_cardinality_repair_and_expression_families_are_ineligible_for_the_core() -> None:
    cfg = fx.FIXTURES_BY_ID["reference"].run.builder()  # type: ignore[attr-defined]
    admission = classify_plan(prepared_config(cfg))
    assert not admission.eligible
    assert inv.SITES_BY_ID["reference.cardinality_repair"].decision == inv.DECLINE


# ---- the rows the slice left alone have unmoved goldens -----------------------------------------


UNMOVED_FIXTURES = {
    "sequence.arithmetic": ["sequence"],
    "reference.cardinality_repair": ["reference"],
    "windowed_date.offset": ["grouped_windowed"],
    "python_expression_families": [
        "formula",
        "referenced_formula",
        "derived",
        "derived_aggregate",
        "statistical",
        "conditional",
        "grouped_windowed",
    ],
    "faker.per_row": ["live_faker", "live_faker_default_locale"],
    "composite.deterministic_multi_slot": [
        "composite_person_deterministic",
        "composite_name_email_deterministic",
    ],
    "composite.nondeterministic_bundle": [
        "composite_person_seeded_pool",
        "composite_name_email_seeded_pool",
    ],
    "legacy.whole_table": ["keyed_derive"],
}


@pytest.mark.parametrize("sid", sorted(UNMOVED_FIXTURES))
def test_declined_and_unchanged_rows_did_not_move_their_golden(sid: str) -> None:
    assert inv.SITES_BY_ID[sid].decision in (inv.DECLINE, inv.UNCHANGED)
    for fid in UNMOVED_FIXTURES[sid]:
        assert fid not in rf.REFROZEN, f"{sid}: fixture {fid} was re-frozen"
        assert fid not in rf.REFROZEN_PIPELINE


def test_only_the_adopted_selection_fixtures_were_refrozen() -> None:
    assert set(rf.REFROZEN) == {"categorical", "pooled_faker", "nulls"}
    assert set(rf.REFROZEN_PIPELINE) == {"mixed_fk_generated_parent"}
    assert set(rf.ADDED) == {"reference_core"}


# ---- cardinality modes: unreachable from the generation core -------------------------------------


def _names_in(module: Any) -> set[str]:
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(n, (ast.Name, ast.Attribute))
    }


@pytest.mark.parametrize("module", [_column_streams, _kernel_select, _streamed])
def test_the_core_never_reaches_a_pool_sampler_cardinality_mode(module: Any) -> None:
    names = _names_in(module)
    assert not names & {"PoolSampler", "CardinalityMode", "UNIQUE", "MATCH_SOURCE_CARDINALITY"}


def test_the_legacy_pool_bridge_only_ever_requests_reuse() -> None:
    modes = {
        n.attr
        for n in ast.walk(ast.parse(inspect.getsource(_faker_pool)))
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "CardinalityMode"
    }
    assert modes == {"REUSE"}
    for sid in (
        "faker.pooled.cardinality.unique",
        "faker.pooled.cardinality.match_scale",
    ):
        assert inv.SITES_BY_ID[sid].decision == inv.DECLINE


def test_deterministic_pool_reuse_already_rides_the_shared_index_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The masking-side sampler is the pre-existing consumer; this slice did not touch it."""
    from decoy_engine.generation.pool._cardinality import CardinalityMode
    from decoy_engine.generation.pool._sampler import PoolSampler

    calls: list[int] = []
    real = _sampler._derive_pool_indices

    def spy(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(_sampler, "_derive_pool_indices", spy)
    PoolSampler().sample(
        w.marked_value_pool(),
        4,
        mode=CardinalityMode.REUSE,
        seed=b"\x01" * 8,
        source=pd.Series(["a", "b", "c", "d"]),
        namespace="ns",
        deterministic=True,
    )
    assert calls == [1]
    assert inv.SITES_BY_ID["faker.pooled.cardinality.reuse_deterministic"].decision == inv.UNCHANGED


def test_city_state_zip_deterministic_rides_the_shared_kernel_and_the_others_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.generation.composite import composite_city_state_zip, composite_person

    calls: list[int] = []
    real = _sampler._derive_pool_indices

    def spy(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(_sampler, "_derive_pool_indices", spy)
    source = pd.Series(["a", "b", "c"])
    spec = fx._composite_spec(deterministic=True)
    composite_city_state_zip(coherent_namespace="ns").generate_bundle(
        spec, 3, source=source, deterministic=True
    )
    assert calls, "city_state_zip deterministic stopped riding the shared index kernel"
    calls.clear()
    composite_person(coherent_namespace="ns").generate_bundle(
        spec, 3, source=source, deterministic=True
    )
    assert calls == [], "the multi-slot composite unexpectedly used the single-index kernel"
    assert inv.SITES_BY_ID["composite.deterministic_single_index"].decision == inv.UNCHANGED
    assert inv.SITES_BY_ID["composite.deterministic_multi_slot"].decision == inv.DECLINE


# ---- the data and the doc agree ------------------------------------------------------------------


def test_every_row_has_a_known_decision_and_a_nonempty_reason() -> None:
    assert len({s.sid for s in inv.SITES}) == len(inv.SITES)
    for s in inv.SITES:
        assert s.decision in {inv.ADOPT, inv.DECLINE, inv.UNCHANGED}, s.sid
        assert len(s.why) > 60, f"{s.sid}: the distribution reasoning is missing"


def test_every_adopted_row_names_the_kernel_and_every_other_row_does_not() -> None:
    for s in inv.SITES:
        assert (s.decision == inv.ADOPT) == (s.backend == inv.KERNEL), s.sid


def test_adopted_rows_are_exactly_the_probed_kernel_columns() -> None:
    adopted = {s.sid for s in inv.SITES if s.decision == inv.ADOPT}
    probed_kernel = {sid for sid in CORE_PROBES if inv.SITES_BY_ID[sid].backend == inv.KERNEL}
    assert adopted == probed_kernel


def test_the_doc_lists_every_row_with_its_decision() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert text.startswith("Status: record")
    for s in inv.SITES:
        row = next((ln for ln in text.splitlines() if ln.startswith(f"| `{s.sid}` |")), None)
        assert row is not None, f"{s.sid} is missing from the inventory doc"
        assert f"**{s.decision}**" in row, s.sid
    assert inv.render_markdown_table() in text


def test_no_probe_references_an_unknown_row() -> None:
    assert set(CORE_PROBES) <= set(inv.SITES_BY_ID)
    assert set(LEGACY_PROBES) <= set(inv.SITES_BY_ID)
    assert set(UNMOVED_FIXTURES) <= set(inv.SITES_BY_ID)


def test_resident_core_run_of_the_probe_config_is_eligible() -> None:
    assert classify_plan(prepared_config(_core_config())).eligible
    assert isinstance(run_resident(_core_config())["t"], pa.Table)
