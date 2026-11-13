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
from decoy_engine.execution._errors import ExecutionError

# Routes that return resident output: the vault is written before anything is published.
RESIDENT: dict[str, str] = {
    "decoy_engine.execution._pipeline.run_pipeline": (
        "returns resident tables; the caller writes the vault before it publishes"
    ),
    "decoy_engine.execution.physical._snapshot.capture_physical_plan_inputs": (
        "builds a routing snapshot; masks and publishes nothing"
    ),
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


def writer_routes() -> dict[str, Callable[..., Any]]:
    """Qualified name -> the exported callable, for every public route with a writer parameter."""
    found: dict[str, Callable[..., Any]] = {}
    for module in _public_modules():
        # Exported names, and any public-named function the module defines itself, so a route
        # left out of `__all__` is still found.
        names = set(getattr(module, "__all__", ())) | {
            n
            for n in dir(module)
            if not n.startswith("_")
            and getattr(getattr(module, n), "__module__", None) == module.__name__
        }
        for name in sorted(names):
            value = getattr(module, name, None)
            if not callable(value) or inspect.isclass(value):
                continue
            target = inspect.unwrap(value)
            try:
                params = inspect.signature(target).parameters
            except (TypeError, ValueError):
                continue
            if any(p in params for p in WRITER_PARAMS):
                found[f"{target.__module__}.{target.__qualname__}"] = value
    return found


class _Untouched:
    """Stands for a chunk, source or plan: any use means the route read before it refused."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"touched ({name}) before the vault writer was refused")

    def __iter__(self) -> Any:
        raise AssertionError("iterated before the vault writer was refused")

    def __len__(self) -> int:
        raise AssertionError("sized before the vault writer was refused")

    def __getitem__(self, key: object) -> Any:
        raise AssertionError("indexed before the vault writer was refused")


def refusal_problem(route: Callable[..., Any]) -> str | None:
    """Why `route` does not refuse a writer before any read, or None when it does."""
    params = inspect.signature(inspect.unwrap(route)).parameters
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == "config":
            kwargs[name] = {}
        elif name in WRITER_PARAMS or name == "sink":
            kwargs[name] = object()
        elif param.default is inspect.Parameter.empty and param.kind in (
            param.POSITIONAL_OR_KEYWORD,
            param.KEYWORD_ONLY,
        ):
            kwargs[name] = _Untouched()
    try:
        route(**kwargs)
    except ExecutionError as exc:
        return None if exc.code == CODE else f"raised {exc.code}, not {CODE}"
    except AssertionError as exc:
        return f"read before refusing: {exc}"
    return "did not refuse a vault writer"


def unplaced(routes: dict[str, Callable[..., Any]]) -> list[str]:
    return sorted(
        f"{name}: {problem}"
        for name, route in routes.items()
        if name not in RESIDENT and (problem := refusal_problem(route)) is not None
    )


def test_every_public_writer_route_is_resident_or_refuses_a_writer() -> None:
    routes = writer_routes()
    assert routes, "discovery found no route with a vault_writer parameter"
    assert unplaced(routes) == []


def test_the_resident_list_names_only_live_routes() -> None:
    assert sorted(set(RESIDENT) - set(writer_routes())) == []


def test_the_chunked_entries_are_found_by_discovery() -> None:
    names = set(writer_routes())
    assert {
        "decoy_engine.execution._chunked.run_mask_pipeline_chunked",
        "decoy_engine.execution.native._chunked_entry.run_mask_chunked",
        "decoy_engine.execution.out_of_core._runner.run_fk_out_of_core",
    } <= names


def test_a_route_that_ignores_the_writer_is_flagged() -> None:
    def new_route(config: dict[str, Any], chunks: Any, *, vault_writer: Any = None) -> Any:
        return iter(())

    def reads_first(config: dict[str, Any], chunks: Any, *, vault_writer: Any = None) -> Any:
        list(chunks)
        return iter(())

    assert unplaced({"fx.new_route": new_route}) == ["fx.new_route: did not refuse a vault writer"]
    (flagged,) = unplaced({"fx.reads_first": reads_first})
    assert "read before refusing" in flagged


def test_a_resident_route_is_not_called() -> None:
    def resident(config: dict[str, Any], *, vault_writer: Any = None) -> Any:
        raise AssertionError("a resident route is declared, not probed")

    assert unplaced({next(iter(RESIDENT)): resident}) == []


@pytest.mark.parametrize("name", sorted(RESIDENT))
def test_each_resident_route_has_a_reason(name: str) -> None:
    assert RESIDENT[name].strip()
