"""S13-rebaseline P1 (2026-06-01) regression test for the QA-8 F3
dedup-key fix.

Pre-fix the orphan_fk_policy dedup key in
`check_orphan_fk_policy_completeness` was `(parent_table, parent_cols)`.
A profile with TWO foreign keys from the same parent to different
children (the canonical nullable_fk golden invariant: `reviews.employee_id`
preserve + `reviews.reviewer_id` fail, both pointing at `employees.id`)
would fail compile with `orphan_fk_policy_duplicate` because the second
entry was rejected as a key collision.

Fix: dedup key is now
`(parent_table, parent_cols, child_table, child_cols)`. Two FKs from
one parent to two different children produce TWO lookup entries, each
carrying its own policy.

This module covers the two halves of the contract:
  - the legitimate nullable_fk pattern compiles cleanly
  - the genuine same-key-different-policy conflict still rejects
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile._types import Relationship
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    check_orphan_fk_policy_completeness,
)


def _rel(
    parent_t: str, parent_c: str, child_t: str, child_c: str, *, ns: str | None = None
) -> Relationship:
    return Relationship(
        parent_table=parent_t,
        parent_columns=(parent_c,),
        child_table=child_t,
        child_columns=(child_c,),
        namespace=ns,
    )


class TestSameParentDifferentChildrenDifferentPolicies:
    """The legitimate nullable_fk pattern: one parent feeds two distinct
    child FKs with different orphan policies."""

    def test_two_fks_same_parent_different_children_different_policies_compile(
        self,
    ) -> None:
        rels = (
            _rel("employees", "employee_id", "reviews", "employee_id"),
            _rel("employees", "employee_id", "reviews", "reviewer_id"),
        )
        config = {
            "relationships": [
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [{"table": "reviews", "columns": ["employee_id"]}],
                    "orphan_policy": "preserve",
                },
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [{"table": "reviews", "columns": ["reviewer_id"]}],
                    "orphan_policy": "fail",
                },
            ],
        }
        lookup = check_orphan_fk_policy_completeness(config, rels)
        assert lookup[("employees", ("employee_id",), "reviews", ("employee_id",))] == OrphanPolicy(
            "preserve"
        )
        assert lookup[("employees", ("employee_id",), "reviews", ("reviewer_id",))] == OrphanPolicy(
            "fail"
        )

    def test_same_parent_multiple_children_same_entry_same_policy(self) -> None:
        """One config entry naming N children all share its policy."""
        rels = (
            _rel("employees", "employee_id", "reviews", "employee_id"),
            _rel("employees", "employee_id", "reviews", "reviewer_id"),
        )
        config = {
            "relationships": [
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [
                        {"table": "reviews", "columns": ["employee_id"]},
                        {"table": "reviews", "columns": ["reviewer_id"]},
                    ],
                    "orphan_policy": "warn",
                },
            ],
        }
        lookup = check_orphan_fk_policy_completeness(config, rels)
        assert lookup[("employees", ("employee_id",), "reviews", ("employee_id",))] == OrphanPolicy(
            "warn"
        )
        assert lookup[("employees", ("employee_id",), "reviews", ("reviewer_id",))] == OrphanPolicy(
            "warn"
        )


class TestGenuineDuplicateStillRejects:
    """The post-fix conflict definition: two entries naming the SAME
    (parent, child) with different policies remains an error."""

    def test_same_parent_same_child_different_policies_raises(self) -> None:
        rels = (_rel("parent", "id", "child", "fk"),)
        config = {
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "preserve",
                },
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "fail",
                },
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_orphan_fk_policy_completeness(config, rels)
        assert exc.value.code == "orphan_fk_policy_duplicate"
        assert "preserve" in exc.value.message
        assert "fail" in exc.value.message

    def test_same_parent_same_child_same_policy_tolerated(self) -> None:
        """Same-policy duplicates are tolerated; the second entry
        carries no new information."""
        rels = (_rel("parent", "id", "child", "fk"),)
        config = {
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "remap",
                },
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["fk"]}],
                    "orphan_policy": "remap",
                },
            ],
        }
        lookup = check_orphan_fk_policy_completeness(config, rels)
        assert lookup[("parent", ("id",), "child", ("fk",))] == OrphanPolicy("remap")
