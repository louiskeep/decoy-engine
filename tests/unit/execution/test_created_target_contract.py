"""AT-E2b-4a, AT-E2b-4b, AT-E2b-6: a column the config creates, per entry and per route.

A declared column the source lacks is exempt only for an entry whose `SURFACE_DECLARATIONS`
row says it creates its own target from columns that exist. These tests execute that contract
for the five creating strategies and the `derive` transform, run every entry on every route
and pin what the route does today, and check that supplied tables are profiled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution._column_access import (
    OWN_TARGET_CREATES,
    OWN_TARGET_SOURCE,
    SURFACE_DECLARATIONS,
    ColumnAccess,
    _union,
    created_columns,
)
from decoy_engine.execution._guards import prepare_declared_execution_sources
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.physical import _shadow_batch_runner
from decoy_engine.execution.physical._shadow_diff_codes import (
    NATIVE_COMPANION_UNAVAILABLE,
    ShadowDifference,
)
from decoy_engine.profile import DeclarationRequest, profile_source
from decoy_engine.providers_v2 import get_default_registry
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER
from tests.native.test_column_access_surfaces import (
    test_g1_every_scalar_handler_has_a_declaration_and_no_more as inventory_test,
)
from tests.physical._shadow_helpers import (
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

SRC = pa.table(
    {
        "a": [1, 2, 3, 4],
        "g": ["x", "x", "y", "y"],
        "d": ["2024-01-01"] * 4,
        "b": ["p", "q", "r", "s"],
    }
)
REG = get_default_registry()
KW: dict[str, Any] = {"engine_version": "e2b-created", "key_provider": THROWAWAY_KEY_PROVIDER}

# entry -> (strategy, provider_config template, valid references); `{0}` and `{1}` are the
# required input references the descriptor must resolve.
CREATING: dict[str, tuple[str, dict[str, Any], tuple[str, ...]]] = {
    "derived": ("derived", {"expression": "{0} + 1"}, ("a",)),
    "derived_aggregate": ("derived_aggregate", {"op": "sum", "column": "{0}"}, ("a",)),
    "grouped_series": ("grouped_series", {"group_by": "{0}", "order_by": "{1}"}, ("g", "a")),
    "windowed_date": ("windowed_date", {"anchor": "{0}", "min_days": 0, "max_days": 5}, ("d",)),
    "group_key": ("group_key", {"group_by": "{0}", "length": 16}, ("g",)),
}
ENTRIES = (*CREATING, "derive")
PASSTHROUGH = [{"name": n, "strategy": "passthrough"} for n in SRC.column_names]


def _valid(entry: str) -> tuple[str, ...]:
    return ("a",) if entry == "derive" else CREATING[entry][2]


def _table(entry: str, refs: tuple[str, ...], name: str = "t") -> dict[str, Any]:
    """The table entry for `entry` with its required references set to `refs`."""
    columns = [*PASSTHROUGH]
    if entry == "derive":
        columns.append({"name": "tgt", "strategy": "passthrough"})
        op = {"op": "derive", "column": "tgt", "expression": f"{refs[0]} + 1"}
        return {"name": name, "columns": columns, "transforms": [op]}
    strategy, template, _ = CREATING[entry]
    config = {k: v.format(*refs) if isinstance(v, str) else v for k, v in template.items()}
    columns.append({"name": "tgt", "strategy": strategy, "provider_config": config})
    return {"name": name, "columns": columns}


def _created(table: dict[str, Any]) -> frozenset[str]:
    return created_columns(table, SRC.column_names, REG)


def test_created_target_descriptor_is_entry_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = {
        "name": "tgt",
        "strategy": "x",
        "provider_config": {"expression": "a", "column": "a"},
        "transforms": [{"op": "derive", "column": "tgt", "expression": "a"}],
    }
    creating = {
        key
        for key, declare in SURFACE_DECLARATIONS.items()
        if declare(probe).own_target == OWN_TARGET_CREATES
    }
    assert creating == {f"scalar:{n}" for n in CREATING} | {"surface:transforms"}

    for entry in ENTRIES:
        refs = _valid(entry)
        assert _created(_table(entry, refs)) == {"tgt"}, entry
        for slot in range(len(refs)):
            for bad in ("tgt", "zz"):  # the target itself, or a column the source lacks
                swapped = tuple(bad if i == slot else r for i, r in enumerate(refs))
                assert _created(_table(entry, swapped)) == frozenset(), (entry, slot, bad)
    for entry in ("derived", "derive"):  # an expression nobody can read
        assert _created(_table(entry, ("b'x'",))) == frozenset(), entry
    faker = {"name": "t", "columns": [{"name": "tgt", "strategy": "faker", "provider": "name"}]}
    assert _created(faker) == frozenset()  # a faker target needs its source

    # A strategy with no declaration reads its own column from the source: never exempt.
    scratch = {"name": "t", "columns": [{"name": "tgt", "strategy": "scratch"}]}
    assert _created(scratch) == frozenset()

    # A handler with no surface declaration fails the existing inventory test.
    monkeypatch.setitem(SCALAR_HANDLERS, "scratch", mock.Mock())
    with pytest.raises(AssertionError):
        inventory_test()


# --- AT-E2b-4b: every entry on every route ---------------------------------------------------

ROUTES = ("full_frame", "isolated", "chunked", "native", "out_of_core")
OOC_REASON = {"derived": "out_of_core_dynamic_output_type_unsupported"}
VALUES: dict[str, Callable[[list[Any]], bool]] = {
    "derived": lambda v: v == [2, 3, 4, 5],
    "derive": lambda v: v == [2, 3, 4, 5],
    "derived_aggregate": lambda v: set(v) == {10},
    "grouped_series": lambda v: v == [0, 1, 0, 1],
    "windowed_date": lambda v: None not in v,
    "group_key": lambda v: v[0] == v[1] != v[2] == v[3],
}


def _expected_refusal(entry: str, route: str) -> str | None:
    """The route's refusal for `entry`, or None when the route runs it."""
    if route in ("full_frame", "isolated"):
        return None
    if entry == "derive":
        return "per_table_transforms_present"
    if route == "out_of_core":
        return OOC_REASON.get(entry, "out_of_core_cross_row")
    if entry in ("windowed_date", "group_key"):
        return None  # explicit chunking admits both; the native entry hands them to it
    return "strategy_not_chunk_safe"


def _config(
    tmp_path: Path, entry: str, *, fk: bool = False, refs: tuple[str, ...] | None = None
) -> dict[str, Any]:
    table = _table(entry, refs or _valid(entry))
    tables = [table]
    if fk:  # out-of-core needs a relationship
        key = {"strategy": "hash", "namespace": "k"}
        table["columns"][0] = {"name": "a", **key}
        tables.append({"name": "p", "columns": [{"name": "a", **key}, *PASSTHROUGH[1:]]})
    sources, targets = {}, {}
    for t in tables:
        SRC.to_pandas().to_csv(tmp_path / f"{t['name']}.csv", index=False)
        path = str(tmp_path / f"{t['name']}.csv")
        sources[t["name"]] = {"type": "file", "format": "csv", "has_header": True, "path": path}
        targets[t["name"]] = {"type": "file", "format": "csv", "path": path + ".out"}
    raw: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": sources,
        "tables": tables,
        "targets": targets,
    }
    if fk:
        raw["relationships"] = [
            {
                "orphan_policy": "preserve",
                "namespace": "k",
                "parent": {"table": "p", "columns": ["a"]},
                "children": [{"table": "t", "columns": ["a"]}],
            }
        ]
    return PipelineConfig.model_validate(raw).model_dump()


def _run(route: str, config: dict[str, Any], evidence: list[Any]) -> pa.Table:
    chunks = [SRC.slice(0, 2), SRC.slice(2, 2)]
    if route == "full_frame":
        return run_pipeline(config, {"t": SRC}, execution_mode="full_frame", **KW).outputs["t"]
    if route == "isolated":
        return run_pipeline_isolated(
            config, {"t": SRC}, isolate=False, execution_mode="full_frame", **KW
        ).outputs["t"]
    if route == "chunked":
        out = run_mask_pipeline_chunked(config, chunks, table="t", source_schema=SRC.schema, **KW)
    elif route == "native":
        out = run_mask_chunked(config, chunks, table="t", route_evidence_sink=evidence, **KW)
    else:
        sources = {"t": SRC, "p": SRC}
        return run_pipeline(
            config, sources, execution_mode="out_of_core", out_of_core_threshold_rows=1, **KW
        ).outputs["t"]
    return pa.concat_tables(list(out))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("entry", ENTRIES)
def test_created_target_route_matrix(
    entry: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if (entry, route) == ("group_key", "native"):
        _native_group_key_selects_no_target(tmp_path, monkeypatch)
        return
    config = _config(tmp_path, entry, fk=route == "out_of_core")
    evidence: list[Any] = []
    refusal = _expected_refusal(entry, route)
    if refusal is not None:
        with pytest.raises(Exception) as caught:
            _run(route, config, evidence)
        assert refusal in f"{getattr(caught.value, 'code', '')} {caught.value}"
        return
    out = _run(route, config, evidence)
    assert out.column_names == [*SRC.column_names, "tgt"]
    assert VALUES[entry](out["tgt"].to_pylist())
    if route == "native":  # the companion declines the table, so the oracle route ran it
        assert evidence[0].native_admitted is False


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("entry", ENTRIES)
def test_created_target_self_reference_refused(entry: str, route: str, tmp_path: Path) -> None:
    """A target that is its own required input is not created: refused before masking."""
    if (route == "native" and _expected_refusal(entry, "chunked")) or (
        entry == "derive" and route in ("native", "out_of_core")
    ):
        pytest.skip("the route refuses the entry at admission, before the declaration check")
    refs = ("tgt",) * len(_valid(entry))
    config = _config(tmp_path, entry, fk=route == "out_of_core", refs=refs)
    with pytest.raises(PipelineValidationError) as caught:
        _run(route, config, [])
    assert caught.value.code == "source_file.declared_column_missing"


def _native_group_key_selects_no_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The native batch loop keys on the sibling, so it never selects the absent target."""
    selected: list[list[str]] = []

    class Batch:
        def __init__(self, table: pa.Table) -> None:
            self.table = table

        def __getattr__(self, name: str) -> Any:
            return getattr(self.table, name)

        def select(self, names: list[str]) -> pa.Table:
            selected.append(list(names))
            return self.table.select([n for n in names if n in self.table.column_names])

    monkeypatch.setattr(
        _shadow_batch_runner,
        "_batches",
        lambda table, size: [Batch(table.slice(i, size)) for i in range(0, table.num_rows, size)],
    )
    source = SRC.select(["g"])
    columns = [PASSTHROUGH[1], _table("group_key", ("g",))["columns"][-1]]
    path = write_read_only_fixture(tmp_path, source, "t")
    config = build_config(tmp_path, "t", path, columns)
    try:
        run = run_shadow_and_oracle(config, "t", source, key_provider=THROWAWAY_KEY_PROVIDER)
        assert_shadow_matches_oracle(run)
    except ShadowDifference as exc:
        # Only a machine without the compiled companion may skip the comparison.
        if exc.code != NATIVE_COMPANION_UNAVAILABLE:
            raise
    assert selected, "the batch loop never ran"
    assert all("tgt" not in names for names in selected)


# --- AT-E2b-6: supplied tables are profiled --------------------------------------------------


def _columns_of(profile: Any) -> dict[str, list[str]]:
    return {t.name: [c.name for c in t.columns] for t in profile.tables}


def test_supplied_sources_are_planned(tmp_path: Path) -> None:
    config = _config(tmp_path, "group_key")
    SRC.select(["a"]).to_pandas().to_csv(tmp_path / "t.csv", index=False)  # the file has `a` only
    config["tables"].append(_table("group_key", ("g",), name="s"))
    config["tables"][0] = {"name": "t", "columns": [PASSTHROUGH[0]]}

    assert _columns_of(profile_source(config, seed=1)) == {"t": ["a"]}
    supplied = profile_source(config, seed=1, supplied_sources={"t": SRC, "s": SRC})
    # `t` keeps its descriptor's profile; `s` has none and is profiled from the supplied table.
    assert _columns_of(supplied) == {"t": ["a"], "s": SRC.column_names}


# `derive` is left out: the transform adds its column from the supplied frame whatever the
# profile holds, so no profile mutation can turn that cell red.
@pytest.mark.parametrize("entry", CREATING)
def test_supplied_only_table_gets_its_created_column(entry: str, tmp_path: Path) -> None:
    raw = _config(tmp_path, "group_key")  # `t` has a file; `s` is handed in with no descriptor
    raw["tables"].append(_table(entry, _valid(entry), name="s"))
    raw["targets"]["s"] = {"type": "file", "format": "csv", "path": str(tmp_path / "s.out")}
    config = PipelineConfig.model_validate(raw).model_dump()
    sources = {"t": SRC, "s": SRC}
    out = run_pipeline(config, sources, execution_mode="full_frame", **KW).outputs["s"]
    assert out.column_names == [*SRC.column_names, "tgt"]
    assert VALUES[entry](out["tgt"].to_pylist())


def test_union_of_differing_own_targets_reads_the_source() -> None:
    creating = ColumnAccess(own_target=OWN_TARGET_CREATES)
    reading = ColumnAccess(own_target=OWN_TARGET_SOURCE)
    assert _union(creating, reading).own_target == OWN_TARGET_SOURCE
    assert _union(reading, creating).own_target == OWN_TARGET_SOURCE
    assert _union(creating, creating).own_target == OWN_TARGET_CREATES


def test_derive_reading_an_earlier_derive_target_is_not_exempt() -> None:
    ops = [
        {"op": "derive", "column": "x", "expression": "a + 1"},
        {"op": "derive", "column": "y", "expression": "x + 1"},
        {"op": "derive", "column": "z", "expression": "a + b"},
    ]
    table = {"name": "t", "columns": PASSTHROUGH, "transforms": ops}
    assert _created(table) == {"x", "z"}


def test_derive_target_written_twice_must_qualify_on_every_write() -> None:
    ops = [
        {"op": "derive", "column": "x", "expression": "a + 1"},
        {"op": "derive", "column": "x", "expression": "x + 1"},
    ]
    assert _created({"name": "t", "transforms": ops}) == frozenset()


class _FakeRegistry:
    """Calls `provider` a composite or a scalar, whatever the default registry says."""

    def __init__(self, provider: str, backend_type: str) -> None:
        self._provider, self._backend = provider, backend_type

    def has(self, name: str) -> bool:
        return name == self._provider

    def get_capabilities(self, name: str) -> Any:
        return mock.Mock(backend_type=self._backend)


def _created_through_boundary(tmp_path: Path, provider: str, registry: Any) -> frozenset[str]:
    """The `tgt` a supplied-only table's capability reports as created, under `registry`."""
    raw = _config(tmp_path, "group_key")
    raw["tables"].append(_table("group_key", _valid("group_key"), name="s"))
    raw["targets"]["s"] = {"type": "file", "format": "csv", "path": str(tmp_path / "s.out")}
    config = PipelineConfig.model_validate(raw).model_dump()
    for t in config["tables"]:
        if t["name"] == "s":
            t["columns"][-1]["provider"] = provider
    request = DeclarationRequest.for_run(config, {"t": SRC.schema, "s": SRC.schema})
    declared = prepare_declared_execution_sources(request, registry=registry)
    return declared.created("s", SRC.column_names)


def test_created_column_follows_the_resolved_registry(tmp_path: Path) -> None:
    """A provider the default registry calls scalar but the override calls composite is not a
    creating entry, and the reverse."""
    scalar_by_default = "unregistered_provider"
    composite_by_default = "composite_person"
    assert not REG.has(scalar_by_default)
    assert REG.get_capabilities(composite_by_default).backend_type == "composite"

    as_composite = _FakeRegistry(scalar_by_default, "composite")
    assert _created_through_boundary(tmp_path, scalar_by_default, None) == {"tgt"}
    assert _created_through_boundary(tmp_path, scalar_by_default, as_composite) == frozenset()

    as_scalar = _FakeRegistry("other", "scalar")
    assert _created_through_boundary(tmp_path, composite_by_default, None) == frozenset()
    assert _created_through_boundary(tmp_path, composite_by_default, as_scalar) == {"tgt"}


def test_run_pipeline_hands_its_registry_to_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _guards

    seen: list[Any] = []
    real = _guards.prepare_declared_execution_sources

    def spy(request: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("registry"))
        return real(request, **kwargs)

    monkeypatch.setattr(_guards, "prepare_declared_execution_sources", spy)
    config = PipelineConfig.model_validate(_config(tmp_path, "derive")).model_dump()
    run_pipeline(config, {"t": SRC}, registry=REG, execution_mode="full_frame", **KW)
    assert seen == [REG]


def test_derive_after_dropped_input_is_not_exempt() -> None:
    ops = [
        {"op": "drop_column", "columns": ["a"]},
        {"op": "derive", "column": "x", "expression": "a + 1"},
    ]
    assert _created({"name": "t", "columns": PASSTHROUGH, "transforms": ops}) == frozenset()


def test_derive_target_shadowing_source_is_not_exempt() -> None:
    ops = [{"op": "derive", "column": "a", "expression": "b"}]
    assert _created({"name": "t", "columns": PASSTHROUGH, "transforms": ops}) == frozenset()
    dropped_first = [{"op": "drop_column", "columns": ["a"]}, *ops]
    assert _created({"name": "t", "transforms": dropped_first}) == {"a"}


@pytest.mark.parametrize("entry", ["oracle", "native"])
def test_chunked_entries_hand_their_registry_to_the_boundary(
    entry: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _chunked
    from decoy_engine.execution.native import _chunked_entry

    module = _chunked if entry == "oracle" else _chunked_entry
    seen: list[Any] = []
    real = module.prepare_declared_execution_sources

    def spy(request: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("registry"))
        return real(request, **kwargs)

    monkeypatch.setattr(module, "prepare_declared_execution_sources", spy)
    config = PipelineConfig.model_validate(_config(tmp_path, "group_key")).model_dump()
    if entry == "oracle":
        out = run_mask_pipeline_chunked(
            config, [SRC], table="t", source_schema=SRC.schema, registry=REG, **KW
        )
    else:
        out = run_mask_chunked(config, [SRC], table="t", registry=REG, **KW)
    list(out)
    assert seen and seen[0] is REG
