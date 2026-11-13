"""Sentry: B0 holds by construction. A public execution route that takes a `vault_writer`
either returns resident output (so the caller writes the vault before it publishes) or refuses
the writer.

The routes are discovered, not listed: every callable exported from `decoy_engine.execution` and
its public submodules whose signature has a `vault_writer` parameter. Each one is on RESIDENT
(one line of reason) or is called with a writer, and must raise `vault_requires_resident_output`
before it touches a chunk, a source or a plan. A new public route with a `vault_writer` that is
in neither set fails here. The baseline is empty and nothing is exempt.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from typing import Any

import pytest

import decoy_engine.execution as execution
from decoy_engine.execution._errors import ExecutionError, vault_requires_resident_output

# Resident call shapes, keyed (qualified name, shape). A route is resident per shape: `writer` is
# a vault writer and no sink; `writer+sink` adds a sink, which commits output inside the call.
# Every other (route, shape) must refuse the writer. Methods are `module.Class.method`.
RESIDENT: dict[tuple[str, str], str] = {
    ("decoy_engine.execution._pipeline.run_pipeline", "writer"): (
        "returns resident tables; the caller writes the vault before it publishes"
    ),
    ("decoy_engine.execution.out_of_core._runner.run_fk_out_of_core", "writer"): (
        "without a sink it returns resident output; the caller writes the vault after it"
    ),
    ("decoy_engine.execution.physical._snapshot.capture_physical_plan_inputs", "writer"): (
        "builds a routing snapshot; masks and publishes nothing"
    ),
    ("decoy_engine.execution.physical._snapshot.capture_physical_plan_inputs", "writer+sink"): (
        "builds a routing snapshot; masks and publishes nothing"
    ),
    (
        "decoy_engine.execution.physical.drivers._chunked.ResidentChunkedAggregatorAdapter.run",
        "writer",
    ): "the resident aggregator behind run_pipeline's auto-chunk lane; returns resident output",
    ("decoy_engine.execution._adapter.StrategyContext.__init__", "writer"): (
        "a data holder that hands the writer to the tokenize strategy of a resident run"
    ),
    ("decoy_engine.execution.physical._shadow_context.ShadowContext.from_key_provider", "writer"): (
        "builds a shadow-run context; masks and publishes nothing"
    ),
    (
        "decoy_engine.execution.physical._shadow_context.ShadowContext.from_key_provider",
        "writer+sink",
    ): "builds a shadow-run context; masks and publishes nothing",
}
WRITER_PARAMS = ("vault_writer",)
CODE = "vault_requires_resident_output"


def _public_modules() -> list[Any]:
    modules = [execution]
    for info in pkgutil.walk_packages(execution.__path__, execution.__name__ + "."):
        if any(part.startswith("_") for part in info.name.split(".")[2:]):
            continue
        modules.append(importlib.import_module(info.name))
    return modules


def _exported(module: Any) -> list[Any]:
    """Exported names plus public names the module defines itself, so a route left out of
    `__all__` is still found."""
    names = set(getattr(module, "__all__", ())) | {
        n
        for n in dir(module)
        if not n.startswith("_")
        and getattr(getattr(module, n), "__module__", None) == module.__name__
    }
    return [getattr(module, n, None) for n in sorted(names)]


Route = Callable[[], Callable[..., Any]]  # resolves the callable a caller would invoke


def _has_writer(fn: Any) -> bool:
    try:
        params = inspect.signature(inspect.unwrap(fn)).parameters
    except (TypeError, ValueError):
        return False
    return any(p in params for p in WRITER_PARAMS)


def _constant(fn: Callable[..., Any]) -> Route:
    return lambda: fn


def _method_route(cls: type, name: str) -> Route:
    """The method as a caller reaches it: on an instance, unless it is a class or static method."""
    if name == "__init__":
        return lambda: cls
    if isinstance(inspect.getattr_static(cls, name), classmethod | staticmethod):
        return lambda: getattr(cls, name)
    return lambda: getattr(cls(), name)


def writer_routes() -> dict[str, Route]:
    """Qualified name -> route, for every public function, and every public method of a public
    class, whose signature has a `vault_writer` parameter."""
    found: dict[str, Route] = {}
    for module in _public_modules():
        for value in _exported(module):
            if inspect.isclass(value):
                for name in vars(value):
                    if name.startswith("_") and name != "__init__":
                        continue
                    member = getattr(value, name, None)
                    if callable(member) and _has_writer(member):
                        found[f"{value.__module__}.{value.__qualname__}.{name}"] = _method_route(
                            value, name
                        )
            elif callable(value) and _has_writer(value):
                target = inspect.unwrap(value)
                found[f"{target.__module__}.{target.__qualname__}"] = _constant(value)
    return found


class _Untouched:
    """Stands for a config, chunk, source or plan: any use means the route read before it refused."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"touched ({name}) before the vault writer was refused")

    def __iter__(self) -> Any:
        raise AssertionError("iterated before the vault writer was refused")

    def __len__(self) -> int:
        raise AssertionError("sized before the vault writer was refused")

    def __getitem__(self, key: object) -> Any:
        raise AssertionError("indexed before the vault writer was refused")

    def __contains__(self, key: object) -> bool:
        raise AssertionError("searched before the vault writer was refused")

    def __bool__(self) -> bool:
        raise AssertionError("tested before the vault writer was refused")


def shapes(call: Callable[..., Any]) -> list[str]:
    params = inspect.signature(inspect.unwrap(call)).parameters
    return ["writer", "writer+sink"] if "sink" in params else ["writer"]


def refusal_problem(call: Callable[..., Any], shape: str) -> str | None:
    """Why `call` does not refuse a writer in `shape` before any read, or None when it does.
    Every argument but the writer and the sink is untouchable, config included."""
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(call).parameters.items():
        if name in WRITER_PARAMS:
            kwargs[name] = object()
        elif name == "sink":
            kwargs[name] = object() if shape == "writer+sink" else None
        elif param.default is inspect.Parameter.empty and param.kind in (
            param.POSITIONAL_OR_KEYWORD,
            param.KEYWORD_ONLY,
        ):
            kwargs[name] = _Untouched()
    try:
        call(**kwargs)
    except ExecutionError as exc:
        return None if exc.code == CODE else f"raised {exc.code}, not {CODE}"
    except AssertionError as exc:
        return f"read before refusing: {exc}"
    return "did not refuse a vault writer"


def unplaced(routes: dict[str, Route]) -> list[str]:
    found = []
    for name, route in routes.items():
        todo = [s for s in ("writer", "writer+sink") if (name, s) not in RESIDENT]
        if not todo:
            continue
        call = route()
        for shape in shapes(call):
            if shape in todo and (problem := refusal_problem(call, shape)) is not None:
                found.append(f"{name} [{shape}]: {problem}")
    return sorted(found)


def test_every_public_writer_route_is_resident_or_refuses_a_writer() -> None:
    routes = writer_routes()
    assert routes, "discovery found no route with a vault_writer parameter"
    assert unplaced(routes) == []


def test_the_resident_list_names_only_live_routes() -> None:
    assert sorted({name for name, _ in RESIDENT} - set(writer_routes())) == []


def test_the_chunked_entries_and_adapters_are_found_by_discovery() -> None:
    names = set(writer_routes())
    drivers = "decoy_engine.execution.physical.drivers._chunked"
    assert {
        "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
        "decoy_engine.execution.native._chunked_entry.run_mask_chunked",
        "decoy_engine.execution.out_of_core._runner.run_fk_out_of_core",
        "decoy_engine.execution._pipeline.run_pipeline",
        f"{drivers}.MaskPipelineChunkedAdapter.run",
        f"{drivers}.NativeOrOracleChunkedAdapter.run",
        f"{drivers}.ResidentChunkedAggregatorAdapter.run",
    } <= names


def test_run_pipeline_is_resident_without_a_sink_only() -> None:
    assert ("decoy_engine.execution._pipeline.run_pipeline", "writer+sink") not in RESIDENT


def test_a_route_that_ignores_the_writer_is_flagged() -> None:
    def new_route(config: dict[str, Any], chunks: Any, *, vault_writer: Any = None) -> Any:
        return iter(())

    def reads_first(config: dict[str, Any], chunks: Any, *, vault_writer: Any = None) -> Any:
        list(chunks)
        return iter(())

    def reads_config(config: dict[str, Any], *, vault_writer: Any = None) -> Any:
        config.get("x")

    assert unplaced({"fx.new_route": lambda: new_route}) == [
        "fx.new_route [writer]: did not refuse a vault writer"
    ]
    (flagged,) = unplaced({"fx.reads_first": lambda: reads_first})
    assert "read before refusing" in flagged
    (flagged,) = unplaced({"fx.reads_config": lambda: reads_config})
    assert "read before refusing" in flagged


def test_a_sink_shape_that_does_not_refuse_is_flagged() -> None:
    def route(config: dict[str, Any], *, vault_writer: Any = None, sink: Any = None) -> Any:
        if sink is None and vault_writer is not None:
            return iter(())
        raise vault_requires_resident_output()

    assert unplaced({"fx.route": lambda: route}) == [
        "fx.route [writer]: did not refuse a vault writer"
    ]
    resident = {("fx.route", "writer"): "resident without a sink"}
    RESIDENT.update(resident)
    try:
        assert unplaced({"fx.route": lambda: route}) == []
    finally:
        for key in resident:
            del RESIDENT[key]


@pytest.mark.parametrize(("name", "shape"), sorted(RESIDENT))
def test_each_resident_shape_has_a_reason(name: str, shape: str) -> None:
    assert RESIDENT[(name, shape)].strip()
