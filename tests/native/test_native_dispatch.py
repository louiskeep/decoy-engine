"""Task 2.7: native-vs-oracle route dispatch inside the streaming coordinator.

Proves the dispatch decision itself (route tags + the compiled-kernel-executed
flag are the evidence, never job success -- Decision 10), independent of the
frozen W2 correctness/perf gate in `tests/parity/native/test_phase2_gate.py`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pyarrow as pa
import pytest

from decoy_engine.execution._chunked_fk import fk_passthrough_columns_for_table
from decoy_engine.execution.native import _dispatch
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._dispatch import (
    NativeRouteEvidence,
    plan_native_route,
    run_native_or_oracle_chunked,
)
from decoy_engine.keyprovider import SecretKeyProvider, key_provider_from_ref
from decoy_engine.profile import ColumnProfile, Profile, Relationship, TableProfile

_MASK_KEY = bytes(range(32))


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _all_admitted_config() -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 42, "post_validation": False},
        "sources": {"w": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "targets": {"w": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {"name": "h", "strategy": "hash", "namespace": "ns_h"},
                    {"name": "p", "strategy": "passthrough"},
                    {"name": "r", "strategy": "redact"},
                    {
                        "name": "t",
                        "strategy": "truncate",
                        "provider_config": {"length": 3, "keep": "head"},
                    },
                    {
                        "name": "t_legacy",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "from_end": True},
                    },
                ],
            }
        ],
    }


def _all_admitted_source() -> pa.Table:
    return pa.table(
        {
            "h": ["a@x.com", "b@x.com", None, "d@x.com"],
            "p": [1, 2, 3, 4],
            "r": ["111-22-3333", "222-33-4444", None, "444-55-6666"],
            "t": ["5551234", "5555678", "5559999", "5551111"],
            "t_legacy": ["ABCDEFGH", "12345678", "WXYZ", None],
        }
    )


def _col(name: str, dtype: str = "object") -> ColumnProfile:
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


def _profile(*names: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="w", row_count=3, columns=tuple(_col(n) for n in names)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 29, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# Route-proof: every admitted node dispatches to a native kernel.
# ---------------------------------------------------------------------------


def test_all_admitted_table_routes_every_node_to_native_kernel() -> None:
    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            [source.slice(0, 2), source.slice(2, 2)],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is True
    assert evidence.reroute_reason is None
    routed_columns = {r.column: r.route for r in evidence.node_routes}
    assert routed_columns == {
        "h": "native_kernel",
        "p": "native_kernel",
        "r": "native_kernel",
        "t": "native_kernel",
        "t_legacy": "native_kernel",
    }
    assert len(chunks) == 2


def test_job_success_alone_is_not_route_proof() -> None:
    # Decision 10: a job can succeed on EITHER route. The route tag, not the
    # fact that output came back, is what proves the native kernel ran.
    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is True
    assert all(r.route == "native_kernel" for r in evidence.node_routes)


def test_compiled_kernel_executed_flag_proves_the_compiled_kernel_ran() -> None:
    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []
    evidence_before_consumption = None
    it = run_native_or_oracle_chunked(
        config,
        [source],
        table="w",
        engine_version="test",
        key_provider=_key_provider(),
        route_evidence_sink=sink,
    )
    evidence_before_consumption = sink[0]
    # Preflight has run (native_admitted decided) but no chunk has been pulled
    # from the iterator yet, so no kernel has actually executed.
    assert evidence_before_consumption.compiled_kernel_executed is False
    assert evidence_before_consumption.kernel_calls == {}
    list(it)
    assert evidence_before_consumption.compiled_kernel_executed is True
    assert evidence_before_consumption.kernel_calls["hash"] == 1


# ---------------------------------------------------------------------------
# Whole-table reroute: a non-admitted column keeps the WHOLE table on the
# oracle, never a mix of native and oracle columns.
# ---------------------------------------------------------------------------


def test_non_admitted_column_reroutes_the_whole_table_not_just_that_column() -> None:
    config = _all_admitted_config()
    config["tables"][0]["columns"].append(
        {
            "name": "fpe_col",
            "strategy": "fpe",
            "deterministic": True,
            "namespace": "ns_fpe",
            "provider_config": {"charset": "digits"},
        }
    )
    source = _all_admitted_source().append_column(
        "fpe_col", pa.array(["12345", "67890", "00001", "11111"])
    )
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert "fallback_policy_not_native:fpe_col" in evidence.reroute_reason
    # Whole-table: every scalar column (including the admitted ones) is
    # tagged "oracle", never a per-column mix, and no kernel ran.
    assert evidence.node_routes != ()
    assert all(r.route == "oracle" for r in evidence.node_routes)
    assert evidence.kernel_calls == {}
    assert evidence.compiled_kernel_executed is False


def test_hash_over_unsupported_input_type_reroutes_whole_table() -> None:
    # Task 2.6 carry-forward: a hash column over a float input is rejected at
    # eligibility (config/type-aware), not left to fail mid-execution.
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {"name": "w", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns"}]}
        ],
    }
    profile = Profile(
        schema_version=1,
        tables=(TableProfile(name="w", row_count=3, columns=(_col("h", dtype="float64"),)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 29, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )
    decision = plan_native_route(config, profile, table="w", engine_version="test")
    assert decision.native_admitted is False
    assert "fallback_policy_not_native:h" in decision.reroute_reason
    # Cross-check the underlying Task 2.6 rejection is the specific type reason
    # (compile_native_plan's fallback_policy folds it into a coarse "python_only",
    # so this confirms the coarse reroute traces back to the right root cause).
    from decoy_engine.execution.native._plan import native_route_eligibility

    eligibility = native_route_eligibility(config, table="w", profile=profile)
    assert any("hash_input_type_not_native" in r for r in eligibility.rejections)


# ---------------------------------------------------------------------------
# FK-composite `<group>` node: capabilities alone read as native-ready (no
# kernel exists this phase), so the dispatch must exclude it independent of
# `native_route_eligibility`'s own accept/reject verdict.
# ---------------------------------------------------------------------------


def _fk_col(name: str, **flags: object) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=5,
        null_count=0,
        distinct_count=5,
        sampled=False,
        is_candidate_key_sampled=bool(flags.get("ck")),
        declared_pk=bool(flags.get("pk")),
        is_fk=bool(flags.get("fk")),
        fk_target=flags.get("target"),  # type: ignore[arg-type]
        pii_class=None,
    )


def _composite_fk_profile() -> Profile:
    enrollments = TableProfile(
        name="enrollments",
        row_count=5,
        columns=(
            _fk_col("member_id", pk=True, ck=True),
            _fk_col("plan_id", pk=True, ck=True),
            _fk_col("effective_date", pk=True, ck=True),
        ),
    )
    claims = TableProfile(
        name="claims",
        row_count=20,
        columns=(
            _fk_col("claim_id", pk=True, ck=True),
            _fk_col("member_id", fk=True, target=("enrollments", "member_id")),
            _fk_col("plan_id", fk=True, target=("enrollments", "plan_id")),
            _fk_col("effective_date", fk=True, target=("enrollments", "effective_date")),
        ),
    )
    return Profile(
        schema_version=1,
        tables=(enrollments, claims),
        relationships=(
            Relationship(
                parent_table="enrollments",
                parent_columns=("member_id", "plan_id", "effective_date"),
                child_table="claims",
                child_columns=("member_id", "plan_id", "effective_date"),
                namespace="enr",
            ),
        ),
        profiled_at=datetime(2026, 8, 27, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def _composite_fk_config() -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "enrollments",
                "columns": [{"name": "member_id", "strategy": "hash", "namespace": "enr"}],
            },
            {
                "name": "claims",
                "columns": [{"name": "claim_id", "strategy": "hash", "namespace": "c"}],
            },
        ],
        "relationships": [
            {
                "parent": {
                    "table": "enrollments",
                    "columns": ["member_id", "plan_id", "effective_date"],
                },
                "children": [
                    {"table": "claims", "columns": ["member_id", "plan_id", "effective_date"]}
                ],
                "orphan_policy": "fail",
                "namespace": "enr",
            }
        ],
        "namespaces": {"enr": {"declared_by": ["enrollments.member_id", "claims.member_id"]}},
    }


def test_fk_composite_group_node_capabilities_read_native_ready_today() -> None:
    # Locks the precondition the trap depends on: if this ever flips, the
    # dispatch's exclusion becomes redundant rather than silently wrong.
    from decoy_engine.execution.native._plan import compile_native_plan

    plan = compile_native_plan(
        _composite_fk_config(), _composite_fk_profile(), engine_version="0.1.0"
    )
    group_nodes = [n for n in plan.nodes if n.kind == "composite_fk_group"]
    assert group_nodes
    assert all(n.fallback_policy == "native" for n in group_nodes)


def test_table_with_fk_composite_group_node_reroutes_to_oracle() -> None:
    decision = plan_native_route(
        _composite_fk_config(), _composite_fk_profile(), table="claims", engine_version="0.1.0"
    )
    assert decision.native_admitted is False
    assert decision.reroute_reason == "fk_relationship_not_native_route"


def test_fk_child_reroutes_under_production_empty_relationship_profile() -> None:
    # The regression that matters: the chunked coordinator's own
    # `first_chunk_profile` always reports `relationships=()`, so the reroute MUST
    # key off the config-declared relationships, not the profile. Under this
    # production-shaped profile the composite-FK child `claims` (orphan_policy:
    # fail) would otherwise admit fully native and silently hash an orphan row
    # where the oracle fails closed with orphan_fk_violation.
    empty_rel_profile = replace(_composite_fk_profile(), relationships=())
    decision = plan_native_route(
        _composite_fk_config(), empty_rel_profile, table="claims", engine_version="0.1.0"
    )
    assert decision.native_admitted is False
    assert decision.reroute_reason == "fk_relationship_not_native_route"
    assert all(r.route == "oracle" for r in decision.node_routes)


def _single_column_fk_config() -> dict:
    return {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "members",
                "columns": [{"name": "member_id", "strategy": "hash", "namespace": "m"}],
            },
            {
                "name": "claims",
                "columns": [{"name": "member_id", "strategy": "hash", "namespace": "m"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "members", "columns": ["member_id"]},
                "children": [{"table": "claims", "columns": ["member_id"]}],
                "orphan_policy": "fail",
                "namespace": "m",
            }
        ],
    }


def test_single_column_fk_child_reroutes_under_production_profile() -> None:
    # A single-column FK child never emits a `<group>` node, so the kind gate does
    # not catch it; the config-declared-relationship reroute must. Production
    # profile shape (no relationships populated).
    empty_rel_profile = replace(_composite_fk_profile(), relationships=())
    decision = plan_native_route(
        _single_column_fk_config(), empty_rel_profile, table="claims", engine_version="0.1.0"
    )
    assert decision.native_admitted is False
    assert decision.reroute_reason == "fk_relationship_not_native_route"


def test_fk_parent_table_also_reroutes_under_production_profile() -> None:
    # A table on the PARENT side of a declared edge reroutes too: this phase does
    # not coordinate cross-table key consistency, so masking a parent key natively
    # while the child runs on the oracle could break joinability.
    empty_rel_profile = replace(_composite_fk_profile(), relationships=())
    decision = plan_native_route(
        _single_column_fk_config(), empty_rel_profile, table="members", engine_version="0.1.0"
    )
    assert decision.native_admitted is False
    assert decision.reroute_reason == "fk_relationship_not_native_route"


# ---------------------------------------------------------------------------
# Preflight-only reroute on an absent/ABI-incompatible compiled extension: no
# mid-stream fallback, no partial native output.
# ---------------------------------------------------------------------------


def test_extension_absent_reroutes_whole_table_at_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unavailable() -> None:
        raise CryptoExtensionUnavailableError("simulated absence")

    monkeypatch.setattr(_dispatch, "load_compiled_crypto_kernel", _unavailable)

    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert evidence.reroute_reason == "crypto_extension_unavailable"
    # Zero native kernel ran, zero partial output staged: every node re-tags
    # from its would-be native route to "oracle".
    assert all(r.route == "oracle" for r in evidence.node_routes)
    assert evidence.kernel_calls == {}
    assert evidence.compiled_kernel_executed is False
    # The oracle still produced correct output (fell back cleanly, not silently).
    out = pa.concat_tables(chunks).combine_chunks()
    assert out.column("p").to_pylist() == [1, 2, 3, 4]
    assert out.column("r").to_pylist() == ["REDACTED", "REDACTED", None, "REDACTED"]


def test_extension_absent_but_no_hash_node_stays_native() -> None:
    # No hash column means the compiled crypto extension is never consulted;
    # a table of purely passthrough/redact/truncate stays native regardless.
    config = {
        "global_settings": {"seed": 42},
        "tables": [{"name": "w", "columns": [{"name": "p", "strategy": "passthrough"}]}],
    }
    profile = _profile_for("w", "p")
    # Deliberately do NOT monkeypatch the loader; it is simply never called.
    decision = plan_native_route(config, profile, table="w", engine_version="test")
    assert decision.native_admitted is True


def _profile_for(table: str, *names: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name=table, row_count=3, columns=tuple(_col(n) for n in names)),),
        relationships=(),
        profiled_at=datetime(2026, 8, 29, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# from_end -> keep resolution (carry-forward #3): matches TruncateHandler
# exactly, independent of the correctness-gate parity test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, "head"),
        ({"from_end": False}, "head"),
        ({"from_end": True}, "tail"),
        ({"from_end": True, "keep": "head"}, "head"),  # explicit keep wins
        ({"keep": "tail"}, "tail"),
    ],
)
def test_resolve_truncate_keep_matches_truncate_handler(cfg: dict, expected: str) -> None:
    assert _dispatch._resolve_truncate_keep(cfg) == expected


# ---------------------------------------------------------------------------
# Uncovered column: a chunk carrying a column the compiled plan does not know
# about reroutes to the oracle rather than guess.
# ---------------------------------------------------------------------------


def test_uncovered_column_reroutes_to_oracle() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [{"name": "w", "columns": [{"name": "p", "strategy": "passthrough"}]}],
    }
    source = pa.table({"p": [1, 2], "unconfigured": ["x", "y"]})
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config, [source], table="w", engine_version="test", route_evidence_sink=sink
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    assert "uncovered_columns" in evidence.reroute_reason


def test_empty_chunk_stream_delegates_cleanly() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [{"name": "w", "columns": [{"name": "p", "strategy": "passthrough"}]}],
    }
    sink: list[NativeRouteEvidence] = []
    out = list(
        run_native_or_oracle_chunked(
            config, [], table="w", engine_version="test", route_evidence_sink=sink
        )
    )
    assert out == []
    assert sink[0].reroute_reason == "empty_input"


# ---------------------------------------------------------------------------
# T5 (batch): `.table` is job evidence too, not just the route fields it names.
# Every NativeRouteEvidence-producing path must stamp the queried table, not
# whatever placeholder a mutant swaps in for it.
# ---------------------------------------------------------------------------


def test_evidence_table_field_is_pinned_on_every_producing_path() -> None:
    # Admitted path.
    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].table == "w"

    # FK-reroute path.
    fk_decision = plan_native_route(
        _composite_fk_config(), _composite_fk_profile(), table="claims", engine_version="0.1.0"
    )
    assert fk_decision.table == "claims"

    # no_mask_nodes path: a table the profile knows but the config never declares.
    decision = plan_native_route(
        {"global_settings": {"seed": 42}, "tables": []},
        _profile_for("ghost", "p"),
        table="ghost",
        engine_version="test",
    )
    assert decision.table == "ghost"

    # Empty-input path.
    sink3: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            {"global_settings": {"seed": 42}, "tables": [{"name": "w", "columns": []}]},
            [],
            table="w",
            engine_version="test",
            route_evidence_sink=sink3,
        )
    )
    assert sink3[0].table == "w"


def test_downgrade_to_oracle_preserves_column_and_strategy_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reroute must not lose track of WHICH column had WHICH strategy: that
    # per-column identity is the job evidence a caller audits, not just the
    # blanket "oracle" route tag every node shares.
    config = _all_admitted_config()
    source = _all_admitted_source()
    sink: list[NativeRouteEvidence] = []

    def _unavailable() -> None:
        raise CryptoExtensionUnavailableError("simulated absence")

    monkeypatch.setattr(_dispatch, "load_compiled_crypto_kernel", _unavailable)
    list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )

    assert sink[0].table == "w"
    routed = {r.column: r.strategy for r in sink[0].node_routes}
    assert routed == {
        "h": "hash",
        "p": "passthrough",
        "r": "redact",
        "t": "truncate",
        "t_legacy": "truncate",
    }


# ---------------------------------------------------------------------------
# no_mask_nodes: a table the profile carries but the config never declares
# columns for. Untested before this batch (zero WorkNodes is a distinct code
# path from every other reroute reason).
# ---------------------------------------------------------------------------


def test_table_with_zero_mask_nodes_reroutes_with_exact_reason() -> None:
    config: dict = {"global_settings": {"seed": 42}, "tables": []}
    profile = _profile_for("ghost", "p")
    decision = plan_native_route(config, profile, table="ghost", engine_version="test")
    assert decision.native_admitted is False
    assert decision.reroute_reason == "no_mask_nodes"
    assert decision.table == "ghost"
    assert decision.node_routes == ()


# ---------------------------------------------------------------------------
# A non-FK composite (generation fan-out) node: capabilities alone read as
# python_only, so this excludes independent of any declared relationship.
# TWO independent composite groups on one table prove the scan does not stop
# at the first non-scalar node (a `continue`/`break` distinction the FK-only
# composite tests above cannot reach, since an FK-participating table exits
# via `_table_in_declared_relationship` before this loop ever runs).
# ---------------------------------------------------------------------------


def test_composite_non_fk_node_is_skipped_without_stopping_the_scan() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {
                        "name": "first_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["last_name", "email"],
                    },
                    {
                        "name": "last_name",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["first_name", "email"],
                    },
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "composite_name_email",
                        "namespace": "ns",
                        "coherent_with": ["first_name", "last_name"],
                    },
                    {
                        "name": "city",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "namespace": "ns2",
                        "coherent_with": ["state", "zip"],
                    },
                    {
                        "name": "state",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "namespace": "ns2",
                        "coherent_with": ["city", "zip"],
                    },
                    {
                        "name": "zip",
                        "strategy": "faker",
                        "provider": "composite_city_state_zip",
                        "namespace": "ns2",
                        "coherent_with": ["city", "state"],
                    },
                    {"name": "p", "strategy": "passthrough"},
                ],
            }
        ],
    }
    profile = _profile_for("w", "first_name", "last_name", "email", "city", "state", "zip", "p")
    decision = plan_native_route(config, profile, table="w", engine_version="test")
    assert decision.native_admitted is False
    # Both composite groups' reasons are present: if the scan stopped at the
    # first one (a `break` bug), the second would never be recorded.
    assert "non_scalar_node:composite:email,first_name,last_name" in decision.reroute_reason
    assert "non_scalar_node:composite:city,state,zip" in decision.reroute_reason
    # The scalar column after both composite nodes is still evaluated, not
    # skipped by an early exit.
    assert any(r.column == "p" for r in decision.node_routes)


# ---------------------------------------------------------------------------
# The crypto-extension probe must run whenever a hash node is present, even
# when EVERY node is a hash node (no other strategy to make an `any(...)`
# check trivially true for the wrong reason).
# ---------------------------------------------------------------------------


def test_extension_probe_runs_for_an_all_hash_table(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unavailable() -> None:
        raise CryptoExtensionUnavailableError("simulated absence")

    monkeypatch.setattr(_dispatch, "load_compiled_crypto_kernel", _unavailable)

    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {"name": "h1", "strategy": "hash", "namespace": "ns1"},
                    {"name": "h2", "strategy": "hash", "namespace": "ns2"},
                ],
            }
        ],
    }
    profile = _profile_for("w", "h1", "h2")
    decision = plan_native_route(config, profile, table="w", engine_version="test")
    assert decision.native_admitted is False
    assert decision.reroute_reason == "crypto_extension_unavailable"


# ---------------------------------------------------------------------------
# FK reroute as a DIFFERENTIAL property against the oracle's own edge walker
# (`fk_passthrough_columns_for_table`, `_chunked_fk.py`, unmodified): any table
# that walker finds FK-participating (parent or child) must also be rerouted
# here. Covers composite, single-column, parent, child, self-referential, and
# chained edges -- every shape the plan names -- plus a non-participating
# table sharing the SAME config, so a mutant that over- or under-triggers the
# reroute (not just one that misses real participation) is caught.
# ---------------------------------------------------------------------------


def _passthrough_col(name: str) -> dict:
    return {"name": name, "strategy": "passthrough"}


@pytest.mark.parametrize(
    ("relationships", "tables", "candidate_tables"),
    [
        pytest.param(
            [
                {
                    "parent": {"table": "A", "columns": ["pk1", "pk2"]},
                    "children": [{"table": "B", "columns": ["pk1", "pk2"]}],
                    "orphan_policy": "remap",
                }
            ],
            [
                {"name": "A", "columns": [_passthrough_col("pk1"), _passthrough_col("pk2")]},
                {"name": "B", "columns": [_passthrough_col("pk1"), _passthrough_col("pk2")]},
                {"name": "unrelated", "columns": [_passthrough_col("x")]},
            ],
            ("A", "B", "unrelated"),
            id="composite",
        ),
        pytest.param(
            [
                {
                    "parent": {"table": "A", "columns": ["id"]},
                    "children": [{"table": "B", "columns": ["a_id"]}],
                    "orphan_policy": "remap",
                }
            ],
            [
                {"name": "A", "columns": [_passthrough_col("id")]},
                {"name": "B", "columns": [_passthrough_col("a_id")]},
                {"name": "unrelated", "columns": [_passthrough_col("x")]},
            ],
            ("A", "B", "unrelated"),
            id="single_column",
        ),
        pytest.param(
            [
                {
                    "parent": {"table": "employees", "columns": ["employee_id"]},
                    "children": [{"table": "employees", "columns": ["manager_id"]}],
                    "orphan_policy": "remap",
                }
            ],
            [
                {
                    "name": "employees",
                    "columns": [_passthrough_col("employee_id"), _passthrough_col("manager_id")],
                },
                {"name": "unrelated", "columns": [_passthrough_col("x")]},
            ],
            ("employees", "unrelated"),
            id="self_referential",
        ),
        pytest.param(
            [
                {
                    "parent": {"table": "A", "columns": ["id"]},
                    "children": [{"table": "B", "columns": ["a_id"]}],
                    "orphan_policy": "remap",
                },
                {
                    "parent": {"table": "B", "columns": ["id"]},
                    "children": [{"table": "C", "columns": ["b_id"]}],
                    "orphan_policy": "remap",
                },
            ],
            [
                {"name": "A", "columns": [_passthrough_col("id")]},
                {"name": "B", "columns": [_passthrough_col("a_id"), _passthrough_col("id")]},
                {"name": "C", "columns": [_passthrough_col("b_id")]},
                {"name": "D", "columns": [_passthrough_col("z")]},
            ],
            ("A", "B", "C", "D"),
            id="chained",
        ),
    ],
)
def test_fk_reroute_agrees_with_oracle_side_walker_across_edge_shapes(
    relationships: list[dict], tables: list[dict], candidate_tables: tuple[str, ...]
) -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": tables,
        "relationships": relationships,
    }
    for table in candidate_tables:
        oracle_participates = bool(fk_passthrough_columns_for_table(config, table))
        got = _dispatch._table_in_declared_relationship(config, table)
        assert got == oracle_participates, (
            f"table {table!r}: _table_in_declared_relationship={got} disagrees with "
            f"the oracle-side walker (fk_passthrough_columns_for_table)={oracle_participates}"
        )


def test_malformed_relationship_entry_does_not_short_circuit_the_scan() -> None:
    # A non-dict entry before a real one must not stop the scan early (a
    # `break` bug would miss every relationship entry after the malformed one).
    config = {
        "relationships": [
            "not_a_dict",
            {
                "parent": {"table": "A", "columns": ["id"]},
                "children": [{"table": "B", "columns": ["a_id"]}],
                "orphan_policy": "remap",
            },
        ]
    }
    assert _dispatch._table_in_declared_relationship(config, "B") is True


def test_non_participating_table_stays_admitted_despite_other_tables_relationships() -> None:
    # A table with NO relationship of its own must not be swept up just
    # because dict-typed parent/child info exists elsewhere in the same
    # config (an `and`-to-`or` corruption of either dict-type guard would
    # return True for ANY dict-typed parent/child regardless of table match).
    config = {
        "relationships": [
            {
                "parent": {"table": "A", "columns": ["id"]},
                "children": [{"table": "B", "columns": ["a_id"]}],
                "orphan_policy": "remap",
            }
        ]
    }
    assert _dispatch._table_in_declared_relationship(config, "Z") is False


def test_malformed_child_entry_mixed_with_a_matching_one_is_still_found() -> None:
    config = {
        "relationships": [
            {
                "parent": {"table": "A", "columns": ["id"]},
                "children": ["not_a_dict", {"table": "B", "columns": ["a_id"]}],
                "orphan_policy": "remap",
            }
        ]
    }
    assert _dispatch._table_in_declared_relationship(config, "B") is True


# ---------------------------------------------------------------------------
# `_mask_chunk_native` must thread each strategy's PROVIDER_CONFIG value to
# the kernel, not silently fall back to the kernel's own default: every prior
# test left `redact_with`/`mask_char`/hash `truncate` at their default value,
# so a mutant deleting or mis-keying any of them was invisible.
# ---------------------------------------------------------------------------


def test_provider_config_values_reach_the_kernels_not_just_the_defaults() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {
                        "name": "r",
                        "strategy": "redact",
                        "provider_config": {"redact_with": "CUSTOM_TAG"},
                    },
                    {
                        "name": "t",
                        "strategy": "truncate",
                        "provider_config": {"length": 4, "keep": "head", "mask_char": "#"},
                    },
                    {
                        "name": "h",
                        "strategy": "hash",
                        "namespace": "ns",
                        "provider_config": {"truncate": 6},
                    },
                ],
            }
        ],
    }
    source = pa.table({"r": ["secret1"], "t": ["ABCDEFGH"], "h": ["a@x.com"]})
    sink: list[NativeRouteEvidence] = []
    chunks = list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is True
    out = pa.concat_tables(chunks)
    # A default-falling-back mutant would emit "REDACTED", not the configured tag.
    assert out.column("r").to_pylist() == ["CUSTOM_TAG"]
    # A default mask_char (None -> no fill) would leave the dropped tail intact
    # instead of "#"-filling it.
    assert out.column("t").to_pylist() == ["ABCD####"]
    # A default (untruncated) hash would not be exactly 6 hex chars.
    assert len(out.column("h").to_pylist()[0]) == 6


# ---------------------------------------------------------------------------
# `kernel_calls` / `kernel_elapsed_s` must ACCUMULATE across chunks, not reset
# on every call (a `.get(None, ...)` corruption always misses the real prior
# value, silently resetting the running total to 1 / a single call's time).
# ---------------------------------------------------------------------------


def test_kernel_evidence_accumulates_across_multiple_chunks() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {"name": "w", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns"}]}
        ],
    }
    chunk1 = pa.table({"h": ["a@x.com"]})
    chunk2 = pa.table({"h": ["b@x.com"]})
    sink: list[NativeRouteEvidence] = []
    it = run_native_or_oracle_chunked(
        config,
        [chunk1, chunk2],
        table="w",
        engine_version="test",
        key_provider=_key_provider(),
        route_evidence_sink=sink,
    )
    next(it)
    assert sink[0].kernel_calls["hash"] == 1
    elapsed_after_one = sink[0].kernel_elapsed_s["hash"]
    # Bounded above, not just "> 0": a wrong default base (e.g. 1.0 instead of
    # 0.0 for a strategy's first call) or a `+` instead of `-` against `t0`
    # (which would add two multi-hundred-thousand-second perf_counter epoch
    # readings together) both stay positive but land far outside a single
    # kernel call's real, sub-second cost.
    assert 0 < elapsed_after_one < 1.0

    next(it)
    assert sink[0].kernel_calls["hash"] == 2
    # Strictly more elapsed time after a SECOND call proves accumulation, not
    # a reset to a single call's reading.
    assert sink[0].kernel_elapsed_s["hash"] > elapsed_after_one


# ---------------------------------------------------------------------------
# When no explicit `key_provider` is given, `_mask_native` must resolve one
# from the config's declared `mask_secret_ref` transparently -- untested
# before this batch (every existing test passes an explicit key_provider).
# ---------------------------------------------------------------------------


def test_mask_native_resolves_key_provider_from_config_mask_secret_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A weak "did it produce a non-empty string" check passes even when the
    # resolution is skipped entirely: pre-GA, `require_mask_key(plan, None)`
    # falls back to `job_seed` rather than raising, so a silently-dropped
    # `mask_secret_ref` still yields a valid-LOOKING hash -- just one derived
    # from the wrong key material. The real proof is comparing the
    # auto-resolved run's output against an EXPLICIT key_provider built from
    # the identical ref: they must be byte-for-byte equal, which only holds
    # if the config ref was actually threaded through as the mask key.
    monkeypatch.setenv("DECOY_T5_TEST_MASK_SECRET", "a1" * 32)
    config = {
        "global_settings": {"seed": 42, "mask_secret_ref": "env:DECOY_T5_TEST_MASK_SECRET"},
        "tables": [
            {"name": "w", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns"}]}
        ],
    }
    source = pa.table({"h": ["a@x.com", "b@x.com"]})

    sink: list[NativeRouteEvidence] = []
    auto_chunks = list(
        run_native_or_oracle_chunked(
            config, [source], table="w", engine_version="test", route_evidence_sink=sink
        )
    )
    assert sink[0].native_admitted is True
    assert sink[0].compiled_kernel_executed is True
    auto_out = pa.concat_tables(auto_chunks).column("h").to_pylist()

    explicit_provider = key_provider_from_ref("env:DECOY_T5_TEST_MASK_SECRET")
    explicit_chunks = list(
        run_native_or_oracle_chunked(
            config,
            [source],
            table="w",
            engine_version="test",
            key_provider=explicit_provider,
        )
    )
    explicit_out = pa.concat_tables(explicit_chunks).column("h").to_pylist()

    assert auto_out == explicit_out


def test_empty_chunk_stream_with_keyed_strategy_and_explicit_key_provider_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The empty-input short-circuit must still thread the caller's key_provider
    # into its oracle delegation. Pre-GA, `require_mask_key` falls back to
    # `job_seed` silently when no provider is resolved, so dropping the
    # explicit key_provider on truly empty input has nothing observable to
    # break -- zero rows means no masked value to compare against a wrong
    # key. Forcing GA mode (the same pattern `test_de02_keyprovider.py` uses)
    # makes the drop observable: the DE-02 gate then raises
    # `KeyedStrategyRequiresSecret` for a keyed job with no resolved secret,
    # exactly the "gate skippable by handing a keyed job zero rows" bug this
    # empty-input path was built to close.
    monkeypatch.setattr("decoy_engine.keyprovider.is_pre_ga", lambda: False)
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {"name": "w", "columns": [{"name": "h", "strategy": "hash", "namespace": "ns"}]}
        ],
    }
    sink: list[NativeRouteEvidence] = []
    out = list(
        run_native_or_oracle_chunked(
            config,
            [],
            table="w",
            engine_version="test",
            key_provider=_key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert out == []
    assert sink[0].reroute_reason == "empty_input"


# ---------------------------------------------------------------------------
# Schema drift. The plan's scope correction: "no partial output" holds ONLY
# for PREFLIGHT-detectable failures. A configured column missing from the
# FIRST chunk IS preflight-detectable (the covered/actual check below runs
# before any chunk is yielded); a later-chunk drift is NOT -- it can follow
# chunks already consumed by the caller. These cases are distinct and both
# get a test.
# ---------------------------------------------------------------------------


def test_configured_column_missing_from_first_chunk_reroutes_and_reports_both_sides() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {"name": "p", "strategy": "passthrough"},
                    {"name": "q", "strategy": "passthrough"},
                ],
            }
        ],
    }
    source = pa.table({"p": [1, 2]})  # "q" is configured but absent from this chunk.
    sink: list[NativeRouteEvidence] = []
    list(
        run_native_or_oracle_chunked(
            config, [source], table="w", engine_version="test", route_evidence_sink=sink
        )
    )
    evidence = sink[0]
    assert evidence.native_admitted is False
    # BOTH sides of the symmetric difference show up: nothing extra in the
    # chunk (empty `uncovered_columns`), but "q" is missing on the other side
    # -- the diagnostic that reported only `actual - covered` would show an
    # empty list here and silently hide the real cause.
    assert "uncovered_columns:[]" in evidence.reroute_reason
    assert "missing_configured_columns:['q']" in evidence.reroute_reason


def test_second_chunk_introducing_an_unconfigured_column_fails_after_first_chunk_consumed() -> None:
    config = {
        "global_settings": {"seed": 42},
        "tables": [{"name": "w", "columns": [{"name": "p", "strategy": "passthrough"}]}],
    }
    chunk1 = pa.table({"p": [1, 2]})
    chunk2 = pa.table({"p": [3, 4], "surprise": ["a", "b"]})
    sink: list[NativeRouteEvidence] = []
    it = run_native_or_oracle_chunked(
        config, [chunk1, chunk2], table="w", engine_version="test", route_evidence_sink=sink
    )
    # Preflight admits the table (the first chunk alone has no drift yet).
    assert sink[0].native_admitted is True
    first_out = next(it)
    assert first_out.column("p").to_pylist() == [1, 2]
    # The SECOND chunk's drift was invisible at preflight; it surfaces only
    # once consumed, after the first chunk's output already reached the
    # caller -- exactly the limit the plan's scope correction names.
    with pytest.raises(KeyError):
        next(it)


def test_second_chunk_missing_a_configured_column_silently_drops_it() -> None:
    # The mirror case: no crash, but a schema drift the caller must notice
    # itself (each chunk's own schema, not a promise of a stable one after
    # the first). Pinned as current, defined behavior -- not a claim that a
    # later-chunk drift is caught the way the first chunk's is.
    config = {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "w",
                "columns": [
                    {"name": "p", "strategy": "passthrough"},
                    {"name": "q", "strategy": "passthrough"},
                ],
            }
        ],
    }
    chunk1 = pa.table({"p": [1, 2], "q": [10, 20]})
    chunk2 = pa.table({"p": [3, 4]})  # "q" absent on this later chunk only.
    sink: list[NativeRouteEvidence] = []
    it = run_native_or_oracle_chunked(
        config, [chunk1, chunk2], table="w", engine_version="test", route_evidence_sink=sink
    )
    assert sink[0].native_admitted is True
    first_out = next(it)
    assert set(first_out.schema.names) == {"p", "q"}
    second_out = next(it)
    assert set(second_out.schema.names) == {"p"}
    assert second_out.column("p").to_pylist() == [3, 4]
