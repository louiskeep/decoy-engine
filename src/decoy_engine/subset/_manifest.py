"""SS5: the subset evidence manifest.

Established contract: counts-and-identifiers-only mirrors the alerts /
evidence no-raw-data convention (platform `docs/reference/alerts.md`) and the
frozen-report discipline of `validation.post._types`. NO raw key value from
`SeedSpec.keys` and NO raw predicate literal from `SeedSpec.predicates` can
appear anywhere in the serialized form: `seed_specs_public` serializes
`keys` mode as count-only, and `filter` mode's predicate `value` is redacted
to a boolean `value_redacted` flag (column + op are kept; see
`_api.py::_seed_spec_public`). The only other fields that could otherwise
leak a value are error messages (budget/orphan messages name tables/edges/
counts, never values).
"""

from __future__ import annotations

from typing import Any

from decoy_engine.subset._types import SubsetManifest, SubsetPlan


def build_manifest(plan: SubsetPlan, engine_version: str) -> SubsetManifest:
    """Assemble a `SubsetManifest` from a passing `SubsetPlan`."""
    return SubsetManifest(
        manifest_version=1,
        engine_version=engine_version,
        seed_specs_public=plan.seed_specs_public,
        tables=plan.tables,
        edges=plan.edges,
        closure_rounds=plan.closure_rounds,
        budget=plan.budget,
        budget_outcome=plan.budget_outcome,
        preflight_summary=plan.preflight.edges,
    )


def _budget_to_json(budget: Any) -> dict[str, Any]:
    return {
        "max_total_rows": budget.max_total_rows,
        "max_table_seed_multiple": budget.max_table_seed_multiple,
    }


def to_json_dict(manifest: SubsetManifest) -> dict[str, Any]:
    """Serialize a `SubsetManifest` to a JSON-safe dict: sorted, scalars only.

    LOAD-BEARING (acceptance test 6): every field here is a count, an
    identifier string, or a policy/direction literal. Nothing here is, or is
    derived from, a raw row value.
    """
    return {
        "manifest_version": manifest.manifest_version,
        "engine_version": manifest.engine_version,
        "seed_specs_public": [dict(spec) for spec in manifest.seed_specs_public],
        "tables": [
            {
                "table": t.table,
                "input_rows": t.input_rows,
                "seed_rows": t.seed_rows,
                "surviving_rows": t.surviving_rows,
                "seed_null_excluded": t.seed_null_excluded,
            }
            for t in manifest.tables
        ],
        "edges": [
            {
                "edge_id": e.edge_id,
                "direction": e.direction,
                "rows_added_downward": e.rows_added_downward,
                "rows_added_upward": e.rows_added_upward,
            }
            for e in manifest.edges
        ],
        "closure_rounds": manifest.closure_rounds,
        "budget": _budget_to_json(manifest.budget),
        "budget_outcome": manifest.budget_outcome,
        "preflight_summary": [
            {
                "relationship": r.relationship,
                "namespace": r.namespace,
                "orphan_policy": r.orphan_policy,
                "child_row_count": r.child_row_count,
                "non_null_child_key_count": r.non_null_child_key_count,
                "parent_match_count": r.parent_match_count,
                "source_orphan_count": r.source_orphan_count,
                "invalid_count": r.invalid_count,
            }
            for r in manifest.preflight_summary
        ],
    }
