"""GEN-REL route acceptance: every reference route enforces one pool check.

Covers the relationship gate, the byte-identity near neighbors (AT-1), the empty-parent policies
on the resident, streamed, whole-table and pinned routes (AT-6), fitted precedence (AT-6f), the
weights refusal (AT-13), the registry cells (AT-14) and value-free refusals (AT-15), and the
self-reference cells of the same three: byte identity beside a tree, the pinned twins, messages.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError

from decoy_engine import reasons, relationships
from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._synthesize_derived import _reference
from decoy_engine.generation.pinned import (
    PinnedModeError,
    PinnedPlan,
    compile_pinned_plan,
    execute_pinned,
    fingerprint,
)
from decoy_engine.generation.pool import GenerationError, QualityWarning
from decoy_engine.generation.statistical import StatisticalSpecError, fanout_spec_from_snapshot
from decoy_engine.generators.derivation import with_table
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.unit.generation.relational._support import (
    CID,
    POLICIES,
    REF,
    ROUTES,
    digest,
    family,
    is_safe,
    plan_of,
    relationship,
    run_route,
)
from tests.unit.generation.test_reference_fanout import QA, _source_frame
from tests.unit.generation.unique._gu_e2_support import cfg, ref, seq_parent, table

SILENT = ("preserve", "remap", None)
# Parent keys and option values chosen so a leak into a message is unmistakable.
SENTINEL = "SENTINELKEY"
NEW_CODES = (
    ("config", "generate_per_parent_bounds_conflict"),
    ("config", "generate_per_parent_null_conflict"),
    ("config", "generate_per_parent_infeasible"),
    ("config", "generate_fitted_reference_bounds_conflict"),
    ("runtime", "generate_per_parent_infeasible"),
    ("runtime", "reference_parent_pool_empty"),
    ("runtime", "reference_weights_length_mismatch"),
    ("runtime", "reference_bounds_unsatisfied"),
    ("runtime", "generate_fitted_reference_bounds_conflict"),
    ("warning", "reference_parent_pool_empty"),
)


def _prepared(config: dict[str, Any]) -> dict[str, Any]:
    loaded = json.loads(plan_of(config).generation.config_json)
    assert isinstance(loaded, dict)
    return loaded


def _config_for(route: str, parent_rows: int, child_rows: int, **kw: Any) -> dict[str, Any]:
    config = family(parent_rows, child_rows, legacy=route == "legacy", **kw)
    assert classify_plan(_prepared(config)).eligible is (route != "legacy")
    return config


def _summary(namespace: str, code: str) -> str:
    return reasons.get(namespace, code).summary


def _pool_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    summary = _summary("warning", "reference_parent_pool_empty")
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(summary)]


# ---- gate: a relationship that names generate tables validates and compiles -------------------


@pytest.mark.parametrize("policy", POLICIES)
def test_gate_relationship_over_generate_tables_validates_and_compiles(policy: str) -> None:
    config = family(3, 6, policy=policy)
    assert PipelineConfig.model_validate(copy.deepcopy(config)).relationships
    assert _prepared(config)["relationships"] == [relationship(policy)]
    assert compile_pinned_plan(copy.deepcopy(config), REF).tables


# ---- AT-1: configs outside the intentional-change cells keep their bytes ----------------------


def _neighbor_configs() -> dict[str, tuple[dict[str, Any], str]]:
    cat_parent = table(
        "parents",
        40,
        [{"name": "id", "type": "categorical", "categories": ["a", "b", "c", "d"]}],
    )
    dates = cfg(
        [
            table(
                "events",
                30,
                [
                    {"name": "start", "type": "categorical", "categories": ["2024-01-15"]},
                    {"name": "end", "type": "windowed_date", "anchor": "start", "max_days": 30},
                    {"name": "n", "type": "sequence", "start": 1},
                    {"name": "total", "type": "derived_aggregate", "op": "sum", "column": "n"},
                ],
            )
        ]
    )
    return {
        "unbounded_random": (family(5, 40), "kids"),
        "unbounded_sequential": (family(5, 40, distribution="sequential"), "kids"),
        "weighted_sequence_parent": (
            family(4, 40, distribution="weighted", weights=[1, 2, 3, 4]),
            "kids",
        ),
        "weighted_categorical_parent": (
            family(0, 40, parent=cat_parent, distribution="weighted", weights=[4, 3, 2, 1]),
            "kids",
        ),
        "zero_bounds": (family(5, 40, min_per_parent=0, max_per_parent=0), "kids"),
        "whole_table_route": (family(5, 40, legacy=True), "kids"),
        "empty_parent_no_relationship": (family(0, 6), "kids"),
        "empty_parent_preserve": (family(0, 6, policy="preserve"), "kids"),
        "empty_parent_remap": (family(0, 6, policy="remap"), "kids"),
        "policy_fail_non_empty": (family(5, 40, policy="fail"), "kids"),
        "policy_warn_non_empty": (family(5, 40, policy="warn"), "kids"),
        "same_table_date_and_scalar_aggregate": (dates, "events"),
    }


# Canonical table digests recorded on the slice base, before any source change.
_SHARED_DIGESTS = {
    "policy_fail_non_empty": "d33e78d9eac10cbb0e60b3bf2e9327f2b67798c3a0ac8d1208820f60ad4e030c",
    "policy_warn_non_empty": "d33e78d9eac10cbb0e60b3bf2e9327f2b67798c3a0ac8d1208820f60ad4e030c",
    "same_table_date_and_scalar_aggregate": (
        "c7db0dff6e991f44f12291f466ed833777cf3eabe7b102c3dafdf1779dd37673"
    ),
    "unbounded_random": "d33e78d9eac10cbb0e60b3bf2e9327f2b67798c3a0ac8d1208820f60ad4e030c",
    "unbounded_sequential": "4ae83d29dc8c28f3a39cea7e3f09559f4202c2338c141fa92b687fe7a2e66a65",
    "weighted_categorical_parent": (
        "e8cfdaf3aeaf57dbf986f4073b4a157186f2d1f31a2c9fd09d254b39b5783de7"
    ),
    "weighted_sequence_parent": "b3b59fbec195fbba98ce08b42f30e2fff8f1708b5002d32a04597ad93ba66c09",
    "whole_table_route": "03acb96419a9d2c769b19f1d3b9e8b8039af85d9f9acb26eaa49db3f66c82061",
    "zero_bounds": "86951a60b699c7dcec996f48b6c375433fc0e9984e1f575c1c6093a867b3344c",
}
_EMPTY_RESIDENT = "29cdb3b43cf3472793abc4b520b94a6ca547c2183292ad70308cf3d8b1e4039f"
_EMPTY_PINNED = "b6245c130dedb96c741a505e093e13e490517f01e24998230e5c6cbb119eb5d9"
_EMPTY = ("empty_parent_no_relationship", "empty_parent_preserve", "empty_parent_remap")
EXPECTED_DIGESTS: dict[str, dict[str, str]] = {
    "resident": {**_SHARED_DIGESTS, **dict.fromkeys(_EMPTY, _EMPTY_RESIDENT)},
    "pinned": {**_SHARED_DIGESTS, **dict.fromkeys(_EMPTY, _EMPTY_PINNED)},
    "fitted": {"non_empty": "a2aad35a252c1184c5e4e318594d51a931ecc399439c051d1980349b6b28015c"},
}


@pytest.mark.parametrize("name", sorted(_neighbor_configs()))
@pytest.mark.parametrize("route", ["resident", "pinned"])
def test_at1_near_neighbor_bytes_do_not_move(name: str, route: str, tmp_path: Path) -> None:
    config, table_name = _neighbor_configs()[name]
    got = digest(run_route(route, config, tmp_path)[table_name])
    assert got == EXPECTED_DIGESTS[route][name], f"{name} on {route} moved"


def test_at1_a_non_empty_pinned_reference_keeps_its_bytes_under_every_policy() -> None:
    want = EXPECTED_DIGESTS["pinned"]["unbounded_random"]
    assert EXPECTED_DIGESTS["pinned"]["policy_fail_non_empty"] == want
    assert EXPECTED_DIGESTS["pinned"]["policy_warn_non_empty"] == want


@pytest.mark.parametrize("route", ROUTES)
def test_at1_a_non_empty_reference_ignores_the_policy_on_every_route(
    route: str, tmp_path: Path
) -> None:
    bare = digest(run_route(route, _config_for(route, 5, 40), tmp_path / "bare")["kids"])
    for policy in POLICIES:
        config = _config_for(route, 5, 40, policy=policy)
        assert digest(run_route(route, config, tmp_path / policy)["kids"]) == bare


# ---- AT-6: an empty parent under each policy, on each route -----------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_at6_fail_refuses_an_empty_parent(route: str, tmp_path: Path) -> None:
    config = _config_for(route, 0, 6, policy="fail")
    with pytest.raises(GenerationError) as exc:
        run_route(route, config, tmp_path)
    assert exc.value.code == reasons.runtime.REFERENCE_PARENT_POOL_EMPTY
    assert exc.value.code.namespace == "runtime"
    assert is_safe(exc.value)
    assert str(exc.value) == (
        f"[reference_parent_pool_empty] {_summary('runtime', 'reference_parent_pool_empty')}"
        " Table 'kids', column 'pid'."
    )


@pytest.mark.parametrize("route", ["resident", "streamed", "legacy"])
def test_at6_warn_keeps_nulls_and_emits_exactly_one_warning(
    route: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Twelve child rows are three streamed batches: the warning is per column, not per batch.
    config = _config_for(route, 0, 12, policy="warn")
    seen: list[QualityWarning] = []
    with caplog.at_level(logging.WARNING, logger="decoy_engine"):
        out = run_route(route, config, tmp_path, on_warning=seen.append)
    assert out["kids"].column("pid").to_pylist() == [None] * 12
    assert seen == [
        QualityWarning(
            code=str(reasons.warning.REFERENCE_PARENT_POOL_EMPTY),
            provider="reference",
            column="pid",
            detail={"table": "kids", "column": "pid"},
        )
    ]
    assert _pool_warnings(caplog) == [
        f"{_summary('warning', 'reference_parent_pool_empty')} Table 'kids', column 'pid'."
    ]


def test_at6_pinned_warn_logs_once_without_a_callback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="decoy_engine"):
        out = run_route("pinned", family(0, 12, policy="warn"), tmp_path)
    assert out["kids"].column("pid").to_pylist() == [None] * 12
    assert len(_pool_warnings(caplog)) == 1


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("policy", SILENT)
def test_at6_preserve_remap_and_no_relationship_stay_silent(
    route: str, policy: str | None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config_for(route, 0, 6, policy=policy)
    seen: list[QualityWarning] = []
    callback = None if route == "pinned" else seen.append
    with caplog.at_level(logging.WARNING, logger="decoy_engine"):
        out = run_route(route, config, tmp_path, on_warning=callback)
    assert out["kids"].column("pid").to_pylist() == [None] * 6
    assert seen == [] and _pool_warnings(caplog) == []


def test_at6_an_empty_child_under_fail_is_not_refused(tmp_path: Path) -> None:
    for route in ROUTES:
        config = _config_for(route, 0, 0, policy="fail")
        assert run_route(route, config, tmp_path / route)["kids"].num_rows == 0


def test_at6_each_child_of_one_relationship_gets_its_own_policy(tmp_path: Path) -> None:
    """Two children share an entry at `warn`; a third child of another parent is `preserve`."""
    config = cfg(
        [
            seq_parent("parents", 0),
            seq_parent("others", 0),
            table("first", 3, [CID, ref("pid", "parents")]),
            table("second", 3, [CID, ref("owner", "parents")]),
            table("third", 3, [CID, ref("pid", "others")]),
        ]
    )
    config["relationships"] = [
        relationship("warn", children=(("first", "pid"), ("second", "owner"))),
        relationship("preserve", parent="others", children=(("third", "pid"),)),
    ]
    seen: list[QualityWarning] = []
    run_route("resident", config, tmp_path, on_warning=seen.append)
    assert sorted((w.detail["table"], w.detail["column"]) for w in seen) == [
        ("first", "pid"),
        ("second", "owner"),
    ]
    failing = copy.deepcopy(config)
    failing["relationships"][0]["orphan_policy"] = "preserve"
    failing["relationships"][1]["orphan_policy"] = "fail"
    with pytest.raises(GenerationError) as exc:
        run_route("resident", failing, tmp_path)
    assert "'third'" in str(exc.value)


def test_at6_pinned_plan_carries_the_policy_and_its_fingerprint_follows_it() -> None:
    plans = {
        p: compile_pinned_plan(copy.deepcopy(family(5, 40, policy=p)), REF)
        for p in (*POLICIES, None)
    }
    for policy in POLICIES:
        assert plans[policy].reference_policies == (("kids", "pid", policy),)
    assert plans[None].reference_policies == ()
    assert "reference_policies" not in plans[None].to_dict()
    assert len({fingerprint(p) for p in plans.values()}) == len(plans)
    again = PinnedPlan.from_dict(json.loads(json.dumps(plans["fail"].to_dict())))
    assert again == plans["fail"] and fingerprint(again) == fingerprint(plans["fail"])
    # The policy is plan metadata: it never enters a column config, so seeds do not move.
    for policy in POLICIES:
        assert plans[policy].tables == plans[None].tables


def test_at6r_generation_reads_policies_through_the_relationship_normalizer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = relationships.normalize_orphan_policy_lookup
    calls: list[int] = []

    def spy(config: dict[str, Any]) -> Any:
        calls.append(1)
        return real(config)

    monkeypatch.setattr(relationships, "normalize_orphan_policy_lookup", spy)
    for route in ROUTES:
        calls.clear()
        with pytest.raises(GenerationError):
            run_route(route, _config_for(route, 0, 6, policy="fail"), tmp_path / route)
        assert calls, f"{route} did not read policies through the relationship package"


# ---- AT-6f: fitted keeps its own precedence ----------------------------------------------------


def _fitted(**extra: Any) -> tuple[dict[str, Any], Any]:
    snap = compute_distribution_snapshot(_source_frame(QA), fanout_columns=("pid",))
    col = with_table(
        {
            "name": "pid",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "k",
            "distribution": "fitted",
            **extra,
        },
        "child",
    )
    return col, fanout_spec_from_snapshot(snap, "pid", digest="ab" * 32)


def _pool(keys: list[str]) -> dict[str, pa.Table]:
    return {"par": pa.table({"k": pa.array(keys, type=pa.string())})}


@pytest.mark.parametrize("bound", ["min_per_parent", "max_per_parent"])
def test_at6f_a_bound_on_a_fitted_reference_refuses_at_run(bound: str) -> None:
    col, spec = _fitted(**{bound: 7919})
    with pytest.raises(GenerationError) as exc:
        _reference(col, 5, 1, None, _pool([f"{SENTINEL}{i}" for i in range(4)]), fanout=spec)
    assert exc.value.code == reasons.runtime.GENERATE_FITTED_REFERENCE_BOUNDS_CONFLICT
    assert exc.value.code.namespace == "runtime"
    assert is_safe(exc.value)
    assert SENTINEL not in str(exc.value) and "7919" not in str(exc.value)


@pytest.mark.parametrize("policy", [*POLICIES, None])
@pytest.mark.parametrize("bounds", [{}, {"min_per_parent": 2}, {"max_per_parent": 2}])
def test_at6f_an_empty_fitted_pool_keeps_the_plan_refusal_under_every_policy(
    policy: str | None, bounds: dict[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    col, spec = _fitted(**bounds)
    seen: list[QualityWarning] = []
    with (
        caplog.at_level(logging.WARNING, logger="decoy_engine"),
        pytest.raises(StatisticalSpecError) as exc,
    ):
        _reference(col, 5, 1, None, _pool([]), fanout=spec, policy=policy, on_warning=seen.append)
    assert exc.value.code == reasons.plan.REFERENCE_PARENT_POOL_EMPTY
    assert seen == [] and _pool_warnings(caplog) == []


def test_at6f_non_empty_fitted_output_without_bounds_does_not_move() -> None:
    col, spec = _fitted()
    keys = [f"k{i}" for i in range(10)]
    bare = _reference(col, 20, 7, None, _pool(keys), fanout=spec)
    assert digest(pa.table({"pid": bare})) == EXPECTED_DIGESTS["fitted"]["non_empty"]
    for policy in POLICIES:
        assert _reference(col, 20, 7, None, _pool(keys), fanout=spec, policy=policy) == bare


# ---- AT-13: weights are checked against the real parent pool on every route --------------------


def _weighted_mismatch(route: str) -> dict[str, Any]:
    """Four distinct parent keys against three weights; compile cannot see the real key count."""
    parent = table(
        "parents",
        60,
        [{"name": "id", "type": "categorical", "categories": [f"{SENTINEL}{i}" for i in "abcd"]}],
    )
    return _config_for(
        route, 0, 12, parent=parent, distribution="weighted", weights=[0.125, 0.25, 0.625]
    )


def test_at13_fixture_has_four_distinct_parent_keys(tmp_path: Path) -> None:
    ok = _weighted_mismatch("resident")
    ok["tables"][1]["generate_columns"][1]["weights"] = [1, 1, 1, 1]
    parents = run_route("resident", ok, tmp_path)["parents"].column("id").to_pylist()
    assert len(set(parents)) == 4


@pytest.mark.parametrize("route", ROUTES)
def test_at13_a_weights_length_mismatch_refuses_at_run(route: str, tmp_path: Path) -> None:
    with pytest.raises(GenerationError) as exc:
        run_route(route, _weighted_mismatch(route), tmp_path)
    assert exc.value.code == reasons.runtime.REFERENCE_WEIGHTS_LENGTH_MISMATCH
    assert exc.value.code.namespace == "runtime"
    assert is_safe(exc.value)
    text = str(exc.value)
    assert SENTINEL not in text and "0.125" not in text and "625" not in text
    assert not any(ch.isdigit() for ch in text)


# ---- AT-14: the registry cells -----------------------------------------------------------------


@pytest.mark.parametrize(("namespace", "code"), NEW_CODES)
def test_at14_new_codes_are_registered_unreleased_in_the_right_catalog(
    namespace: str, code: str
) -> None:
    spec = reasons.get(namespace, code)
    assert spec.since == reasons.UNRELEASED
    phases = {rule.phase for rule in spec.rules}
    if namespace == "config":
        assert phases == {reasons.Phase.CONFIGURATION}
        assert spec.owner == "PipelineValidationError"
    elif namespace == "runtime":
        assert reasons.Phase.GENERATE in phases and reasons.Phase.COMPILE not in phases
        assert spec.owner == "GenerationError"
    else:
        assert reasons.Phase.GENERATE in phases and spec.kind is reasons.Kind.WARNING
        assert spec.owner == "QualityWarning"


def test_at14_same_name_twins_share_one_fixed_summary() -> None:
    for code in ("generate_per_parent_infeasible", "generate_fitted_reference_bounds_conflict"):
        assert _summary("config", code) == _summary("runtime", code)


# ---- AT-15: refusals and warnings carry names only ---------------------------------------------


def test_at15_validate_refusals_carry_the_fixed_summary_and_no_input() -> None:
    config = family(10, 20, min_per_parent=7919, max_per_parent=7907)
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(config)
    (error,) = exc.value.errors()
    assert error["type"] == "generate_per_parent_bounds_conflict"
    assert error["msg"] == _summary("config", "generate_per_parent_bounds_conflict")
    assert error["input"] is None
    assert "7919" not in str(exc.value) and "7907" not in str(exc.value)
    assert is_safe(exc.value)


@pytest.mark.parametrize("route", ["resident", "pinned"])
def test_at15_runtime_refusal_names_the_table_and_column_only(route: str, tmp_path: Path) -> None:
    """Distinct keys fall short of `row_count`, so only the run can see the bounds are infeasible.

    A bounded reference is declined by the streaming core, so `resident` is the whole-table route.
    """
    parent = table(
        "parents",
        9,
        [{"name": "id", "type": "categorical", "categories": [f"{SENTINEL}a", f"{SENTINEL}b"]}],
    )
    config = family(0, 31, parent=parent, max_per_parent=7)
    with pytest.raises(GenerationError) as exc:
        run_route(route, config, tmp_path)
    assert exc.value.code == reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE
    assert is_safe(exc.value)
    assert str(exc.value) == (
        f"[generate_per_parent_infeasible] {_summary('runtime', 'generate_per_parent_infeasible')}"
        " Table 'kids', column 'pid'."
    )


# ---- self-reference: registry twins, value-free refusals, untouched neighbors ------------------

TREE_CODES = (
    ("config", "generate_self_reference_key_invalid", "PipelineValidationError"),
    ("config", "generate_self_reference_option_unsupported", "PipelineValidationError"),
    ("plan", "pinned_relational_option_unsupported", "PinnedModeError"),
    ("runtime", "pinned_relational_option_unsupported", "GenerationError"),
)
PINNED_UNSUPPORTED = "pinned_relational_option_unsupported"


def _tree_config(rows: int = 30, **options: Any) -> dict[str, Any]:
    key = {"name": "id", "type": "sequence", "start": 1}
    return cfg([table("employees", rows, [key, ref("manager_id", "employees", **options)])])


@pytest.mark.parametrize(("namespace", "code", "owner"), TREE_CODES)
def test_at14_self_reference_codes_are_registered_unreleased_in_the_right_catalog(
    namespace: str, code: str, owner: str
) -> None:
    spec = reasons.get(namespace, code)
    assert spec.since == reasons.UNRELEASED
    assert spec.owner == owner and spec.kind is reasons.Kind.FAILURE
    phases = {rule.phase for rule in spec.rules}
    if namespace == "config":
        assert phases == {reasons.Phase.CONFIGURATION}
    elif namespace == "plan":
        assert phases == {reasons.Phase.COMPILE}
    else:
        assert reasons.Phase.GENERATE in phases and reasons.Phase.COMPILE not in phases


def test_at14_the_pinned_twins_share_one_fixed_summary_and_differ_by_namespace() -> None:
    plan_code = reasons.plan.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    runtime_code = reasons.runtime.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    assert plan_code == runtime_code == PINNED_UNSUPPORTED
    assert (plan_code.namespace, runtime_code.namespace) == ("plan", "runtime")
    assert _summary("plan", PINNED_UNSUPPORTED) == _summary("runtime", PINNED_UNSUPPORTED)


def test_at15_pinned_compile_refusal_of_a_self_reference_names_the_table_and_column_only() -> None:
    config = _tree_config(31, max_per_parent=7919, root_count=3)
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(copy.deepcopy(config), REF)
    assert exc.value.code == reasons.plan.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    assert exc.value.code.namespace == "plan"
    assert is_safe(exc.value)
    assert str(exc.value) == (
        f"[{PINNED_UNSUPPORTED}] {_summary('plan', PINNED_UNSUPPORTED)}"
        " Table 'employees', column 'manager_id'."
    )


def test_at15_pinned_execute_refusal_of_a_self_reference_names_the_table_and_column_only() -> None:
    """A plan that never went through pinned compile: the column is rewritten after the fact."""
    plan = compile_pinned_plan(copy.deepcopy(family(5, 31)), REF)
    stored = plan.to_dict()
    kids = next(t for t in stored["tables"] if t["name"] == "kids")
    column = json.loads(kids["columns"][1]["config_json"])
    column.update(reference_table="kids", reference_column="cid", max_per_parent=7919)
    kids["columns"][1]["config_json"] = json.dumps(column, sort_keys=True)
    kids["depends_on"] = []
    with pytest.raises(GenerationError) as exc:
        execute_pinned(PinnedPlan.from_dict(stored), 7)
    assert exc.value.code == reasons.runtime.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    assert exc.value.code.namespace == "runtime"
    assert is_safe(exc.value)
    assert str(exc.value) == (
        f"[{PINNED_UNSUPPORTED}] {_summary('runtime', PINNED_UNSUPPORTED)}"
        " Table 'kids', column 'pid'."
    )


def test_at15_validate_refusals_of_a_self_reference_carry_the_fixed_summary_and_no_input() -> None:
    for options, code in (
        ({"min_per_parent": 7919}, "generate_self_reference_option_unsupported"),
        (
            {"distribution": "weighted", "weights": [7919, 7907]},
            ("generate_self_reference_option_unsupported"),
        ),
    ):
        with pytest.raises(ValidationError) as exc:
            PipelineConfig.model_validate(_tree_config(**options))
        (error,) = exc.value.errors()
        assert error["type"] == code
        assert error["msg"] == _summary("config", code)
        assert error["input"] is None
        assert "79" not in str(exc.value)
        assert is_safe(exc.value)
    bad_key = _tree_config()
    bad_key["tables"][0]["generate_columns"][0] = {
        "name": "id",
        "type": "categorical",
        "categories": [f"{SENTINEL}a", f"{SENTINEL}b"],
    }
    with pytest.raises(ValidationError) as exc:
        PipelineConfig.model_validate(bad_key)
    (error,) = exc.value.errors()
    assert error["type"] == "generate_self_reference_key_invalid"
    assert error["msg"] == _summary("config", "generate_self_reference_key_invalid")
    assert SENTINEL not in str(exc.value) and is_safe(exc.value)


def test_at1_a_tree_beside_a_family_leaves_the_familys_bytes_alone(tmp_path: Path) -> None:
    """The self-edge skip and the tree dispatch must not move a table that has neither."""
    config = family(5, 40)
    config["tables"].append(_tree_config()["tables"][0])
    config["targets"]["employees"] = {"type": "file", "format": "csv", "path": "o.csv"}
    out = run_route("resident", config, tmp_path)
    assert digest(out["kids"]) == EXPECTED_DIGESTS["resident"]["unbounded_random"]
    assert out["employees"].column("manager_id").null_count == 1
