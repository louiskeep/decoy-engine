"""Every reason identity that shipped in a tagged release still resolves through the registry.

The table is the v0.15.1 / v0.16.0 reason surface: every literal the route decider
(`_pipeline_routing._sequential_eligible` / `decide_execution_route`) and the chunked output
sink's `decide_output_mode` can emit, extracted by AST from `git show <tag>:src/decoy_engine/
execution/...` (`_emitted_reason_literals`), not read by eye: a reason assigned to a `*_reason`
slot or chosen in a conditional expression counts the same as one on a `return` line. Persisted
routing evidence from those releases names these codes; a renamed one must stay readable as a
deprecated tombstone that links to its replacement (registry class rule: append-only).

`SHIPPED_EMITTED` pins the per-tag extraction so the exhaustiveness check runs in a shallow
clone; where the tags are present, `test_pinned_literals_match_the_tags` re-derives it.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from decoy_engine import reasons

_REPO = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO / "tests" / "fixtures" / "f29" / "reason-manifest-v1.json"

# Tag -> commit. A re-pointed tag is a different release surface and fails the re-derivation.
SHIPPED_TAGS: dict[str, str] = {
    "v0.15.1": "fd51be10673225d81a8c000d460b0172112b26ac",
    "v0.16.0": "5da40c2c6ce67db86703996dd8cf245955f05eca",
}
_DECIDER_FILES = (
    "src/decoy_engine/execution/_pipeline_routing.py",
    "src/decoy_engine/execution/_chunked_output_sink.py",
)
# The registry's wire grammar for a code (`tests/sentry/_reason_scan.py::CODE_SHAPE`) and the
# slots a routing decision's reason is stored in (`REASON_NAME`).
_CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_.:-]*$")
_REASON_NAME = re.compile(r"(route|reroute|full_frame)_reason$", re.IGNORECASE)

_ROUTE_DECIDER_LITERALS = frozenset(
    {
        "byte_estimate_bounded_out_of_core",
        "byte_estimate_full_frame_fits",
        "cross_table_cycle",
        "fidelity_report_requested",
        "generate_plus_mask",
        "mixed_generate_mask_chunked",
        "no_relationships",
        "non_pandas_substrate_requested",
        "out_of_core_large_fk",
        "override_full_frame",
        "override_out_of_core",
        "post_validation_requested",
        "probe_recovered_full_frame",
        "pure_mask_fk",
        "run_storm_requested",
        "validators_present",
        "vault_writer_requested",
    }
)
_OUTPUT_SINK_LITERALS = frozenset(
    {
        "eligible",
        "fidelity_report",
        "legacy_lane",
        "no_sink",
        "post_validation",
        "quarantine_enabled",
        "run_storm",
        "sink_not_streaming",
        "split_extra_sources_present",
        "split_full_frame_present",
        "streaming_disabled",
        "validators_present",
    }
)
# Both tags emit the same surface; a later tag that adds a reason gets its own entry.
SHIPPED_EMITTED: dict[str, frozenset[str]] = {
    "v0.15.1": _ROUTE_DECIDER_LITERALS | _OUTPUT_SINK_LITERALS,
    "v0.16.0": _ROUTE_DECIDER_LITERALS | _OUTPUT_SINK_LITERALS,
}

# (code, replacement or None when the identity is still live under its shipped name)
V0_16_0_ROUTE_REASONS: tuple[tuple[str, str | None], ...] = (
    # _sequential_eligible
    ("fidelity_report_requested", None),
    ("generate_plus_mask", None),
    ("no_relationships", None),
    ("non_pandas_substrate_requested", None),
    ("post_validation_requested", None),
    ("pure_mask_fk", None),
    ("run_storm_requested", "post_scan_integrity_requested"),
    ("validators_present", None),
    ("vault_writer_requested", None),
    # decide_execution_route
    ("byte_estimate_bounded_out_of_core", None),
    ("byte_estimate_full_frame_fits", None),
    ("cross_table_cycle", None),
    ("mixed_generate_mask_chunked", None),
    ("out_of_core_large_fk", None),
    ("override_full_frame", None),
    ("override_out_of_core", None),
    ("probe_recovered_full_frame", None),
    # decide_output_mode (chunked output sink)
    ("eligible", None),
    ("fidelity_report", None),
    ("legacy_lane", None),
    ("no_sink", None),
    ("post_validation", None),
    ("quarantine_enabled", None),
    ("run_storm", "post_scan_integrity"),
    ("sink_not_streaming", None),
    ("split_extra_sources_present", None),
    ("split_full_frame_present", None),
    ("streaming_disabled", None),
)


@pytest.mark.parametrize(
    ("code", "replacement"), V0_16_0_ROUTE_REASONS, ids=[c for c, _ in V0_16_0_ROUTE_REASONS]
)
def test_shipped_route_reason_resolves(code: str, replacement: str | None) -> None:
    spec = reasons.find("route", code)
    assert spec is not None, f"route.{code} shipped in v0.16.0 and no longer resolves"
    if replacement is None:
        assert spec.deprecated_in is None and spec.replaced_by is None
        return
    assert spec.deprecated_in is not None, "a renamed identity is a tombstone, not a live entry"
    assert spec.replaced_by == ("route", replacement)
    live = reasons.get("route", replacement)
    assert live.deprecated_in is None, "a replacement must itself be live"
    assert live.kind is spec.kind and live.rules == spec.rules


def test_the_pinned_manifest_carries_tombstone_and_replacement() -> None:
    entries = {(e["namespace"], e["code"]): e for e in json.loads(_MANIFEST.read_text())["entries"]}
    for code, replacement in V0_16_0_ROUTE_REASONS:
        assert ("route", code) in entries
        if replacement is not None:
            assert entries[("route", code)]["replaced_by"] == ["route", replacement]
            assert ("route", replacement) in entries


def test_no_tombstone_without_a_live_replacement() -> None:
    for spec in reasons.specs():
        if spec.deprecated_in is None:
            continue
        assert spec.replaced_by is not None, f"{spec.identity} is retired with no replacement"
        assert reasons.get(*spec.replaced_by).deprecated_in is None


def _module_str_consts(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                out.update(
                    {t.id: node.value.value for t in node.targets if isinstance(t, ast.Name)}
                )
    return out


def _code_literals(expr: ast.AST, consts: dict[str, str]) -> set[str]:
    """Every code-shaped string the expression can evaluate to: literals in any branch of a
    conditional / boolean expression, and module constants that hold one."""
    found: set[str] = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.Name) and node.id in consts:
            found.add(consts[node.id])
    return {code for code in found if _CODE_SHAPE.match(code)}


def _emitted_reason_literals(source: str) -> set[str]:
    """Reason codes a decider in `source` can emit: the last element of every returned tuple
    (`(route, reason)` / `(eligible, reason)` / `(mode, reason)`) and every value stored in a
    `*_reason` slot, in any function of the module."""
    tree = ast.parse(source)
    consts = _module_str_consts(tree)
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple) and node.value.elts:
            emitted |= _code_literals(node.value.elts[-1], consts)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and _REASON_NAME.search(t.id) for t in targets):
                emitted |= _code_literals(node.value, consts)
    return emitted


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed argv (git + literals), no untrusted input
        ["git", *args],  # noqa: S607 -- the trusted `git` on PATH, as the seam sentry uses it
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_table_is_exhaustive_against_the_tags_emitted_literals() -> None:
    """Every literal either tag can emit is in the table (an omitted one would silently stop
    being guarded), and the table carries nothing the tags never emitted."""
    table = {code for code, _ in V0_16_0_ROUTE_REASONS}
    shipped = frozenset().union(*SHIPPED_EMITTED.values())
    assert table == shipped, (
        f"missing from the table: {sorted(shipped - table)}; "
        f"not emitted by any tag: {sorted(table - shipped)}"
    )
    assert len({code for code, _ in V0_16_0_ROUTE_REASONS}) == len(V0_16_0_ROUTE_REASONS)


@pytest.mark.parametrize("tag", sorted(SHIPPED_TAGS))
def test_pinned_literals_match_the_tags(tag: str) -> None:
    """Re-derive `SHIPPED_EMITTED[tag]` from the tag's decider sources. Skips only where the
    tag is not in the clone; the pinned set is what the exhaustiveness check reads either way."""
    try:
        commit = _git("rev-parse", "--verify", f"{tag}^{{commit}}").strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{tag} is not fetched in this clone; the pinned extraction stands in")
    assert commit == SHIPPED_TAGS[tag], f"{tag} no longer points at the pinned release commit"
    emitted: set[str] = set()
    for rel in _DECIDER_FILES:
        emitted |= _emitted_reason_literals(_git("show", f"{tag}:{rel}"))
    assert emitted == SHIPPED_EMITTED[tag], (
        f"{tag} emits {sorted(emitted - SHIPPED_EMITTED[tag])} beyond the pinned set; "
        f"pinned but not emitted: {sorted(SHIPPED_EMITTED[tag] - emitted)}"
    )
