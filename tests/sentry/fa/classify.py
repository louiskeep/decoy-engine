"""Classify scanned sites against the module lists and the registry."""

from __future__ import annotations

from dataclasses import dataclass

from tests.sentry.fa.rules import (
    Violation,
    check_adapter_site,
    check_capability_site,
)
from tests.sentry.fa.scan import Site


@dataclass(frozen=True)
class Roles:
    adapters: frozenset[str]
    capabilities: frozenset[str]
    local_constructors: frozenset[str]


def registry_index(
    entries: list[dict[str, object]],
) -> dict[tuple[str, str, str, int], dict[str, object]]:
    return {
        (str(e["module"]), str(e["function"]), str(e["sink"]), int(e["ordinal"])): e  # type: ignore[call-overload]
        for e in entries
    }


def classify(
    sites: list[Site], roles: Roles, registry: dict[tuple[str, str, str, int], dict[str, object]]
) -> tuple[list[Violation], set[tuple[str, str, str, int]]]:
    """Every violation, and the registry keys that matched a site."""
    violations: list[Violation] = []
    used: set[tuple[str, str, str, int]] = set()
    for site in sites:
        in_adapter = site.module in roles.adapters
        in_capability = site.module in roles.capabilities
        if site.kind in {"thread", "executor"}:
            violations.append(
                Violation(site, "a thread or pool task that does not go through run_in_context")
            )
            continue
        if site.kind == "localfa":
            if site.module not in roles.local_constructors:
                violations.append(
                    Violation(site, "LocalFileAccess constructed outside the entry module")
                )
            continue
        if site.kind == "descriptor":
            if in_capability:
                v = check_capability_site(site)
                if v:
                    violations.append(v)
            continue
        if site.sink == "duckdb.connect" and not (in_adapter and "duckdb.py" in site.module):
            violations.append(Violation(site, "a DuckDB connection outside the DuckDB adapter"))
            continue
        if in_adapter:
            v = check_adapter_site(site)
            if v:
                violations.append(v)
            continue
        if in_capability:
            v = check_capability_site(site)
            if v:
                violations.append(v)
            continue
        entry = registry.get(site.key)
        if entry is None:
            violations.append(
                Violation(site, "not allowed by an adapter, a capability module or the registry")
            )
        else:
            used.add(site.key)
    return violations, used
