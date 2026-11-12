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
from decoy_engine.relationships import _graph
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    check_orphan_fk_policy_completeness,
)

SENTINEL = "SENTINEL-POLICY-TOKEN"


def _entry(policy: object, child_col: str = "fk") -> dict:
    return {
        "parent": {"table": "parent", "columns": ["id"]},
        "children": [{"table": "child", "columns": [child_col]}],
        "orphan_policy": policy,
    }


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
        assert exc.value.path == "relationships[1]"
        # The message names the two ends; a submitted policy token never reaches it.
        assert "parent.id" in exc.value.message and "child.fk" in exc.value.message
        for token in ("preserve", "fail"):
            assert token not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__context__ is None

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


class TestCentralNormalizer:
    """GEN-REL AT-6r: one relationship-policy parser, exported for generation, with safe text."""

    def test_the_normalizer_is_public_and_returns_the_four_part_lookup(self) -> None:
        from decoy_engine.relationships import normalize_orphan_policy_lookup

        config = {"relationships": [_entry("warn"), _entry("fail", "other")]}
        assert normalize_orphan_policy_lookup(config) == {
            ("parent", ("id",), "child", ("fk",)): OrphanPolicy.WARN,
            ("parent", ("id",), "child", ("other",)): OrphanPolicy.FAIL,
        }
        assert normalize_orphan_policy_lookup({}) == {}
        assert normalize_orphan_policy_lookup({"relationships": "not-a-list"}) == {}

    def test_the_completeness_check_consumes_the_normalizer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict] = []
        real = _graph.normalize_orphan_policy_lookup

        def spy(config: dict) -> dict:
            calls.append(config)
            return real(config)

        monkeypatch.setattr(_graph, "normalize_orphan_policy_lookup", spy)
        config = {"relationships": [_entry("remap")]}
        lookup = check_orphan_fk_policy_completeness(config, (_rel("parent", "id", "child", "fk"),))
        assert calls == [config]
        assert lookup == {("parent", ("id",), "child", ("fk",)): OrphanPolicy.REMAP}

    @pytest.mark.parametrize("policy", [SENTINEL, 7919, ["fail"]])
    def test_an_invalid_policy_is_refused_without_echoing_it(self, policy: object) -> None:
        with pytest.raises(PlanCompileError) as exc:
            check_orphan_fk_policy_completeness({"relationships": [_entry(policy)]}, ())
        assert exc.value.code == "orphan_fk_policy_invalid"
        assert exc.value.path == "relationships[0].orphan_policy"
        assert "parent.id" in exc.value.message
        for token in (SENTINEL, "7919", "fail"):
            assert token not in str(exc.value)
        assert exc.value.__cause__ is None and exc.value.__context__ is None

    def test_a_duplicate_names_neither_policy(self) -> None:
        config = {"relationships": [_entry("warn"), _entry("remap")]}
        with pytest.raises(PlanCompileError) as exc:
            check_orphan_fk_policy_completeness(config, ())
        assert exc.value.code == "orphan_fk_policy_duplicate"
        for token in ("warn", "remap"):
            assert token not in str(exc.value)
