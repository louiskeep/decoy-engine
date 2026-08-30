"""Config-aware and input-type-aware `native_route_eligibility` (Task 2.6).

Phase 1's eligibility query classified a column by strategy + static
capabilities only: it never checked whether a column's resolved CONFIG shape
(a truncate length of 0, a non-string redact_with) or its resolved INPUT
Arrow type (a float or naive-timestamp `hash` column) was one the native
kernels can actually honor. Left alone, that gap would only surface as a
mid-execution `StrategyError` on the native route instead of a preflight
reroute to the oracle. These tests pin the closed gap for the four admitted
strategies (`passthrough`, `redact`, `truncate`, `hash`) and the agreement
between `native_route_eligibility` and `compile_native_plan` that keeps the
two APIs from silently diverging on the same input.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pyarrow as pa
import pytest

from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._capabilities import _CAPS, capabilities_for
from decoy_engine.execution.native._plan import (
    compile_native_plan,
    native_route_eligibility,
)
from decoy_engine.execution.native._requirements import resolve_input_arrow_type
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile


def _col(name: str, dtype: str) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype=dtype,
        row_count=3,
        null_count=0,
        distinct_count=3,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _profile(name: str, dtype: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=3, columns=(_col(name, dtype),)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 28, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _config(*columns: dict) -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [{"name": "t", "columns": list(columns)}],
    }


def _hash_col(name: str = "c") -> dict:
    return {"name": name, "strategy": "hash", "namespace": "ns"}


def _rejection_codes(result) -> set[str]:
    return {r.split(":", 1)[0] for r in result.rejections}


# ---------------------------------------------------------------------------
# hash: input-type-aware rejections.
# ---------------------------------------------------------------------------


def test_hash_over_float_input_is_rejected() -> None:
    result = native_route_eligibility(
        _config(_hash_col()), table="t", profile=_profile("c", "float64")
    )
    assert result.accepted is False
    assert any(r.startswith("hash_input_type_not_native:c:") for r in result.rejections)


def test_hash_over_naive_timestamp_is_rejected() -> None:
    result = native_route_eligibility(
        _config(_hash_col()), table="t", profile=_profile("c", "datetime64[ns]")
    )
    assert result.accepted is False
    assert any(r.startswith("hash_input_type_not_native:c:") for r in result.rejections)


def test_hash_over_unresolvable_dtype_is_rejected_as_mixed_object() -> None:
    # A dtype label the resolver does not recognize at all must be rejected to
    # the oracle, never silently defaulted to native-safe Utf8. (A plain object
    # column reports "object" -> string and is admitted; this fires only for
    # labels outside the resolver's table, e.g. this synthetic "mixed".)
    result = native_route_eligibility(
        _config(_hash_col()), table="t", profile=_profile("c", "mixed")
    )
    assert result.accepted is False
    assert result.rejections == ("mixed_object_not_native:c",)


@pytest.mark.parametrize(
    "dtype",
    [
        "object",
        "string",
        # Every signed/unsigned integer width Rust `is_admitted_type` accepts,
        # in both the numpy and pandas-nullable label forms -- a hash column over
        # any of them must be admitted, not over-rejected to the oracle.
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "bool",
        "boolean",
        "datetime64[ns, UTC]",
    ],
)
def test_hash_over_admitted_types_is_accepted(dtype: str) -> None:
    result = native_route_eligibility(_config(_hash_col()), table="t", profile=_profile("c", dtype))
    assert result.accepted is True
    assert result.rejections == ()


def test_hash_type_gate_deferred_without_profile() -> None:
    # Documents the boundary (matching the existing FK-group precedent): a
    # caller that omits the profile gets no input-type verdict at all, not a
    # false accept manufactured from a guessed type. Safe only when the
    # caller separately knows the column's type is native-admitted.
    result = native_route_eligibility(_config(_hash_col()), table="t")
    assert result.accepted is True
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# truncate: compile-time-equivalent config validation at eligibility.
# ---------------------------------------------------------------------------


def _truncate_col(**provider_config: object) -> dict:
    return {"name": "c", "strategy": "truncate", "provider_config": provider_config}


def test_truncate_invalid_length_is_rejected_at_eligibility() -> None:
    result = native_route_eligibility(_config(_truncate_col(length="3")), table="t")
    assert result.accepted is False
    assert any(r.startswith("truncate_length_invalid:c") for r in result.rejections)


def test_truncate_invalid_keep_is_rejected_at_eligibility() -> None:
    result = native_route_eligibility(_config(_truncate_col(length=3, keep="sideways")), table="t")
    assert result.accepted is False
    assert any(r.startswith("truncate_keep_invalid:c") for r in result.rejections)


def test_truncate_invalid_mask_char_is_rejected_at_eligibility() -> None:
    result = native_route_eligibility(_config(_truncate_col(length=3, mask_char="**")), table="t")
    assert result.accepted is False
    assert any(r.startswith("truncate_mask_char_invalid:c") for r in result.rejections)


def test_truncate_valid_config_is_accepted() -> None:
    result = native_route_eligibility(_config(_truncate_col(length=4)), table="t")
    assert result.accepted is True
    assert result.rejections == ()


def test_truncate_legacy_from_end_resolves_like_the_handler() -> None:
    # from_end=True with no explicit keep resolves to keep="tail", matching
    # TruncateHandler.run; a valid length alongside it must not be rejected.
    result = native_route_eligibility(_config(_truncate_col(length=4, from_end=True)), table="t")
    assert result.accepted is True
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# redact: string-only redact_with invariant.
# ---------------------------------------------------------------------------


def _redact_col(**provider_config: object) -> dict:
    col: dict = {"name": "c", "strategy": "redact"}
    if provider_config:
        col["provider_config"] = provider_config
    return col


def test_redact_non_string_redact_with_is_rejected() -> None:
    result = native_route_eligibility(_config(_redact_col(redact_with=7)), table="t")
    assert result.accepted is False
    assert result.rejections == ("redact_with_not_string:c",)


def test_redact_default_redact_with_is_accepted() -> None:
    result = native_route_eligibility(_config(_redact_col()), table="t")
    assert result.accepted is True
    assert result.rejections == ()


def test_redact_explicit_string_redact_with_is_accepted() -> None:
    result = native_route_eligibility(_config(_redact_col(redact_with="XXXX")), table="t")
    assert result.accepted is True
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# passthrough: identity, no config gate; static output type only.
# ---------------------------------------------------------------------------


def test_passthrough_is_accepted_with_no_config() -> None:
    result = native_route_eligibility(_config({"name": "c", "strategy": "passthrough"}), table="t")
    assert result.accepted is True
    assert result.rejections == ()


# ---------------------------------------------------------------------------
# Compiler-vs-eligibility agreement: the two APIs must reach the same verdict
# on every one of the new config/type gates, not just the capability gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "dtype"),
    [
        (_hash_col(), "float64"),
        (_hash_col(), "datetime64[ns]"),
        (_hash_col(), "mixed"),
        (_hash_col(), "int64"),
        ({"name": "c", "strategy": "truncate", "provider_config": {"length": -1}}, "object"),
        ({"name": "c", "strategy": "truncate", "provider_config": {"length": 4}}, "object"),
        ({"name": "c", "strategy": "redact", "provider_config": {"redact_with": 5}}, "object"),
        ({"name": "c", "strategy": "redact"}, "object"),
    ],
)
def test_eligibility_agrees_with_compiler_on_config_gates(column: dict, dtype: str) -> None:
    cfg = _config(column)
    profile = _profile("c", dtype)
    try:
        plan = compile_native_plan(cfg, profile, engine_version="0.1.0")
    except PlanCompileError:
        # An invalid truncate config never reaches a NativeExecutionPlan at
        # all: compile_plan's own check_truncate_config raises first (the
        # same predicate this task's truncate_config_rejection reuses), so
        # a raise here agrees with eligibility's rejection just as surely as
        # a plan whose every node is fallback_policy != "native" would.
        plan_all_native = False
    else:
        plan_all_native = all(node.fallback_policy == "native" for node in plan.nodes)
    result = native_route_eligibility(cfg, table="t", profile=profile)
    assert result.accepted is plan_all_native, (column, dtype, result.rejections)


# ---------------------------------------------------------------------------
# Totality/drift re-check: the new gates narrow within the admitted set, but
# every live mask strategy still resolves to a coded verdict, never a raise,
# and the admitted set itself is unchanged.
# ---------------------------------------------------------------------------


def test_config_aware_gates_stay_total_over_live_registry() -> None:
    profile = _profile("c", "object")
    for strategy in SCALAR_HANDLERS:
        cfg = _config({"name": "c", "strategy": strategy, "namespace": "ns"})
        result = native_route_eligibility(cfg, table="t", profile=profile)
        if not result.accepted:
            assert result.rejections != ()


def test_the_four_admitted_strategies_still_accept_a_valid_config() -> None:
    # A config/type gate must only ever narrow: none of the four strategies
    # this task adds gates for loses its accept on a config the gate has no
    # objection to.
    profile = _profile("c", "object")
    for strategy, extra in (
        ("passthrough", {}),
        ("redact", {}),
        ("truncate", {"provider_config": {"length": 4}}),
        ("hash", {"namespace": "ns"}),
    ):
        cfg = _config({"name": "c", "strategy": strategy, **extra})
        result = native_route_eligibility(cfg, table="t", profile=profile)
        assert result.accepted is True, (strategy, result.rejections)


def test_admitted_set_is_exactly_the_four_native_kernels() -> None:
    # The drift sentry the plan's Failure-modes section names: run every
    # live mask strategy through native_route_eligibility with a config
    # each gate has no objection to and an admitted input type, then assert
    # the ACCEPTED set is exactly the four strategies with a compiled
    # native kernel this phase. A bare capability check (row-local, static
    # output type) admits several strategies with no kernel behind them yet
    # (fpe, date_shift, bucket_perturb, group_key, code_set, text_mask,
    # categorical, text_redact, geo_generalize, bucketize); this is the test
    # that would have caught that widening, and fails again the day a new
    # strategy gains kernel-friendly capabilities without a kernel landing
    # for it, or a kernel is pulled without eligibility catching up.
    profile = _profile("c", "int64")
    accepted: set[str] = set()
    for strategy in SCALAR_HANDLERS:
        col: dict[str, object] = {"name": "c", "strategy": strategy, "namespace": "ns"}
        if strategy == "truncate":
            col["provider_config"] = {"length": 4}
        result = native_route_eligibility(_config(col), table="t", profile=profile)
        if result.accepted:
            accepted.add(strategy)
    assert accepted == {"passthrough", "redact", "truncate", "hash"}


# ---------------------------------------------------------------------------
# T4: measured gaps the mutation pilot demonstrated (docs/plans/
# native-testing-T4-eligibility.md). Each test below closes a survivor the
# pilot found real (not message-text-only); the file's existing tests already
# meet the bar for every mutant not listed there as a demonstrated gap.
# ---------------------------------------------------------------------------

_FORMULA_COL = {"name": "f", "strategy": "formula", "provider_config": {"formula": "value"}}


def test_generate_columns_flag_is_rejected_with_exact_code() -> None:
    # No existing test ever sets a truthy `generate_columns`, so the coded
    # rejection and the key it reads from have nothing exercising them.
    cfg = _config(_hash_col())
    cfg["tables"][0]["generate_columns"] = ["c"]
    result = native_route_eligibility(cfg, table="t")
    assert result.accepted is False
    assert "generation_not_native_route:generate_columns" in result.rejections


def test_table_present_with_no_columns_key_is_accepted() -> None:
    # `table_cfg.get("columns", ())`'s default is never exercised; every
    # existing config always sets a "columns" list.
    result = native_route_eligibility({"tables": [{"name": "t"}]}, table="t")
    assert result.accepted is True
    assert result.rejections == ()


def test_config_with_no_tables_key_is_treated_as_no_table() -> None:
    # `_find_table`'s `.get("tables", ())` default is never exercised; every
    # existing config always sets a "tables" list.
    result = native_route_eligibility({}, table="t")
    assert result.accepted is True
    assert result.rejections == ()


def test_column_missing_both_name_and_strategy_uses_placeholder_name() -> None:
    # The "?" placeholder default (`col.get("name", "?")`) is never
    # exercised; every existing config column sets "name".
    result = native_route_eligibility(_config({}), table="t")
    assert result.rejections == ("missing_strategy:?",)


def test_output_type_indeterminate_rejection_embeds_exact_name_and_strategy() -> None:
    # Every existing assertion on this rejection is a substring check
    # ("output_type_indeterminate" in r), which cannot catch the name or
    # strategy embedded in the f-string being swapped for a different value.
    result = native_route_eligibility(_config(_FORMULA_COL), table="t")
    assert result.rejections == ("output_type_indeterminate:f:formula",)


def test_unclassified_strategy_string_is_rejected_with_exact_code() -> None:
    # native_route_eligibility reads the raw config directly (no compile_plan
    # validation runs first), so an unrecognized strategy string reaches
    # capabilities_for and must be caught here, not raised out of this
    # function.
    result = native_route_eligibility(
        _config({"name": "c", "strategy": "totally_bogus"}), table="t"
    )
    assert result.accepted is False
    assert result.rejections == ("unclassified_strategy:c:totally_bogus",)


def test_no_native_kernel_rejection_embeds_exact_name_and_strategy() -> None:
    # Same shape as above for the kernel-availability gate: bucketize passes
    # every capability check but has no compiled kernel.
    result = native_route_eligibility(_config({"name": "c", "strategy": "bucketize"}), table="t")
    assert result.rejections == ("no_native_kernel:c:bucketize",)


def test_output_schema_for_excludes_table_with_any_non_native_node() -> None:
    # `output_schema_for` is a method on a frozen dataclass; mutmut's
    # decorated-class limitation (plan section 3.6) generates zero mutants
    # for it, so this branch is graded by a direct, targeted test rather
    # than a mutation score. A table with one native node (hash) and one
    # indeterminate node (formula) must have no merged schema at all: the
    # whole table falls back to the oracle, not a schema missing one field.
    cfg = _config(_hash_col(), _FORMULA_COL)
    plan = compile_native_plan(cfg, _profile("c", "object"), engine_version="0.1.0")
    assert plan.output_schema_for("t") is None


def test_compile_native_plan_node_fields_match_the_underlying_work_node() -> None:
    # NativePlanNode.columns/.strategy feed _dispatch.py's route decision
    # directly (column = node.columns[0]; scalar_columns.append((column,
    # node.strategy))), so a mismatch here is a real route-tag bug. Checked
    # against the WorkNode compile_native_plan derives them from, not a
    # hand-typed expectation. The capabilities check additionally proves a
    # scalar node resolves ITS OWN strategy's capabilities, not the
    # composite/fk-group placeholder's (a broken kind match in the
    # compiler's own strategy resolution would silently swap them without
    # ever raising, since every kind placeholder is itself a valid
    # capabilities key).
    cfg = _config(_hash_col())
    plan = compile_native_plan(cfg, _profile("c", "object"), engine_version="0.1.0")
    assert plan.nodes and len(plan.nodes) == len(plan.work_nodes)
    for node, wn in zip(plan.nodes, plan.work_nodes, strict=True):
        assert node.columns == wn.columns
        assert node.strategy == wn.strategy
    assert plan.nodes[0].capabilities == capabilities_for("hash")


def test_passthrough_output_schema_preserves_input_arrow_type() -> None:
    # `_output_arrow_schema`'s type-preserving branch (passthrough/shuffle)
    # is unexercised by every other test in this file (all use hash/redact/
    # truncate, none type-preserving), so a broken table/column/profile
    # argument or an inverted membership check has nothing to catch it
    # except an exact type assertion against a non-string profile dtype.
    cfg = _config({"name": "c", "strategy": "passthrough"})
    plan = compile_native_plan(cfg, _profile("c", "int64"), engine_version="0.1.0")
    schema = plan.output_schema_for("t")
    assert schema is not None
    assert schema.names == ["c"]
    assert schema.field("c").type == pa.int64()


def test_hash_output_schema_is_string_typed_with_exact_field_name() -> None:
    # The non-type-preserving branch: a masked/tokenized surface is a string
    # regardless of the input type. Every existing test only checks
    # `isinstance(schema, pa.Schema)`, which cannot catch a wrong field name
    # or a dropped/renamed positional argument to `pa.field`.
    plan = compile_native_plan(_config(_hash_col()), _profile("c", "int64"), engine_version="0.1.0")
    schema = plan.output_schema_for("t")
    assert schema is not None
    assert schema.names == ["c"]
    assert schema.field("c").type == pa.string()


def test_resolve_input_arrow_type_with_no_profile_is_none_not_a_crash() -> None:
    # A public function (exported in _requirements.__all__): a caller
    # passing profile=None must get the documented "unknowable" None
    # signal, not an AttributeError/TypeError from a bare getattr with no
    # default.
    assert resolve_input_arrow_type("t", "c", None) is None


def test_resolve_input_arrow_type_skips_non_matching_tables() -> None:
    # The outer loop's `continue` (skip this table, keep scanning) is
    # unexercised by every existing profile, which has exactly one table.
    profile = Profile(
        schema_version=1,
        tables=(
            TableProfile(name="other", row_count=1, columns=(_col("c", "int64"),)),
            TableProfile(name="t", row_count=1, columns=(_col("c", "int64"),)),
        ),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    assert resolve_input_arrow_type("t", "c", profile) == pa.int64()


def test_resolve_input_arrow_type_skips_non_matching_columns() -> None:
    # The inner loop's `continue` is unexercised by every existing profile,
    # which has exactly one column per table.
    profile = Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name="t", row_count=1, columns=(_col("other_col", "int64"), _col("c", "int64"))
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    assert resolve_input_arrow_type("t", "c", profile) == pa.int64()


def test_resolve_input_arrow_type_column_absent_from_matching_table_returns_none() -> None:
    # The matching table's own column loop can run to completion without a
    # hit (falling back to the outer loop rather than returning early); every
    # existing profile's single table always has the queried column.
    profile = Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=1, columns=(_col("other_col", "int64"),)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    assert resolve_input_arrow_type("t", "c", profile) is None


def test_output_schema_for_skips_nodes_on_a_different_table() -> None:
    # `output_schema_for`'s own table filter (`if node.table != table:
    # continue`) is unexercised by every existing test: they all compile a
    # single-table config, so every node already matches the queried table. The
    # other table uses a DISTINCT column name ("d"), so dropping the filter would
    # merge "other"'s node into "t"'s schema and this assertion would see ["c",
    # "d"] instead of ["c"] -- a same-named column on both tables would de-dup on
    # merge and mask the leak.
    cfg = {
        "global_settings": {"seed": 42},
        "tables": [
            {"name": "t", "columns": [_hash_col("c")]},
            {"name": "other", "columns": [_hash_col("d")]},
        ],
    }
    profile = Profile(
        schema_version=1,
        tables=(
            TableProfile(name="t", row_count=1, columns=(_col("c", "object"),)),
            TableProfile(name="other", row_count=1, columns=(_col("d", "object"),)),
        ),
        relationships=(),
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    plan = compile_native_plan(cfg, profile, engine_version="0.1.0")
    schema = plan.output_schema_for("t")
    assert schema is not None
    assert schema.names == ["c"]  # must NOT include "other"'s "d"


def test_capability_friendly_kernel_less_strategy_is_exactly_python_only() -> None:
    # `requirements_for`'s kernel-availability gate is never checked by an
    # exact fallback_policy value anywhere in this file; only the coarse
    # `!= "native"` shape is asserted elsewhere. bucketize passes every
    # capability check (row-local, static output type) but has no compiled
    # kernel, so its fallback_policy must be exactly "python_only" via that
    # gate specifically, not merely non-"native" for some other reason.
    col = {"name": "c", "strategy": "bucketize", "provider_config": {"width": 10}}
    plan = compile_native_plan(_config(col), _profile("c", "object"), engine_version="0.1.0")
    assert plan.nodes[0].fallback_policy == "python_only"


def test_output_type_indeterminate_node_fallback_policy_is_exactly_python_only() -> None:
    # The route-tag literal itself: every existing test checks `!= "native"`
    # or `all(... == "native")`, never the exact non-native string, so a
    # typo'd "python_only" literal in `_fallback_policy`'s return has
    # nothing to catch it.
    plan = compile_native_plan(
        _config(_FORMULA_COL), _profile("f", "object"), engine_version="0.1.0"
    )
    assert plan.nodes[0].fallback_policy == "python_only"


@pytest.mark.parametrize(
    ("is_row_local", "is_global", "needs_global_row_identity", "expect_rejected"),
    [
        (True, False, False, False),
        (True, False, True, True),
        (False, False, False, True),
        (True, True, False, True),
    ],
)
def test_native_rejection_boolean_corners_via_synthetic_strategy(
    monkeypatch: pytest.MonkeyPatch,
    is_row_local: bool,
    is_global: bool,
    needs_global_row_identity: bool,
    expect_rejected: bool,
) -> None:
    # `_native_rejection`'s OR-chain (`not row_local or is_global or
    # needs_global_row_identity`) cannot be discriminated by any LIVE
    # strategy: every registered row-local strategy has is_global=False, and
    # every non-row-local strategy already has is_global or
    # needs_global_row_identity True (see _capabilities.py's registry), so
    # an `and`-typo'd mutant of this chain reaches the identical verdict for
    # every strategy actually registered. A synthetic capability entry
    # proves the boolean logic itself, independent of what the live
    # registry happens to contain.
    synthetic = replace(
        capabilities_for("hash"),
        is_row_local=is_row_local,
        is_global=is_global,
        needs_global_row_identity=needs_global_row_identity,
        output_type_is_static=True,
    )
    strategy_name = "__t4_native_rejection_probe__"
    monkeypatch.setitem(_CAPS, strategy_name, synthetic)
    result = native_route_eligibility(_config({"name": "c", "strategy": strategy_name}), table="t")
    codes = {r.split(":", 1)[0] for r in result.rejections}
    if expect_rejected:
        assert "requires_global_execution" in codes
    else:
        assert "requires_global_execution" not in codes


def test_fk_group_rejection_wiring_when_group_capabilities_are_non_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `<group>`'s capabilities are always native-ready today (locked by
    # test_fk_group_capabilities_are_native_ready_today in
    # test_native_plan.py), so no live input exercises
    # `_fk_group_rejections`' own reject branch, or the wiring around it
    # (native_route_eligibility's table_cfg-absent-but-profile-given path,
    # and passing the right table name through to it). Monkeypatch the
    # capability entry to a hypothetical non-native shape -- matching the
    # precedent's own "if these capabilities ever change" framing -- to
    # prove the WIRING, not today's capability value. Two relationships (one
    # for a different table, one for the queried table) additionally proves
    # a non-matching relationship is skipped, not treated as a reason to
    # stop scanning.
    non_native_group = replace(capabilities_for("<group>"), output_type_is_static=False)
    monkeypatch.setitem(_CAPS, "<group>", non_native_group)

    relationships = (
        Relationship(
            parent_table="unrelated_parent",
            parent_columns=("a", "b"),
            child_table="unrelated_child",
            child_columns=("a", "b"),
            namespace="ns1",
        ),
        Relationship(
            parent_table="p",
            parent_columns=("member_id", "plan_id"),
            child_table="claims",
            child_columns=("member_id", "plan_id"),
            namespace="ns2",
        ),
    )
    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=relationships,
        profiled_at=datetime(2026, 8, 30, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    # No "claims" table entry in the config at all: table_cfg is None but a
    # profile is given, the exact shape that must still evaluate the
    # FK-group check rather than short-circuit past it.
    config: dict = {"global_settings": {"seed": 42}, "tables": []}

    result = native_route_eligibility(config, table="claims", profile=profile)

    assert result.accepted is False
    assert result.rejections == ("output_type_indeterminate:member_id__plan_id:<group>",)
