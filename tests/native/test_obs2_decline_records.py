"""OBS-2b: structured route-decline records.

Each decision source yields its own enumerated code with the right column / table-wide
attribution, earlier records survive the `when:` override, and the new projection carries
schema identifiers and the closed code only (never provider, expression or exception text).
The legacy `reroute_reason` string is asserted unchanged next to it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _chunked_entry, _dispatch
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._dispatch import NodeRouteRecord, plan_native_route
from decoy_engine.execution.native._faker_deterministic_admission import (
    C5cIiAdmissionContext,
    deterministic_faker_decline,
)
from decoy_engine.execution.native._real_type_admission import real_type_decline
from decoy_engine.execution.native._route_decline_codes import (
    RouteDeclineCode as C,
)
from decoy_engine.execution.native._route_decline_codes import (
    RouteDeclineRecord,
    column_decline,
    decline_payload,
    table_decline,
)
from decoy_engine.execution.native._when_admission import first_when_decline
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import FORCE, FORCE_REASON, run_one
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    categorical,
    faker_col,
    force_oracle,
    hash_col,
    make_config,
    passthrough,
    redact,
    split,
)
from tests.native._chunked_group_key_support import GB, TARGET, gk_col, gk_source

_PAYLOAD_KEYS = {"code", "table", "column", "table_wide"}


def _profile(table: str, *names: str) -> Profile:
    cols = tuple(
        ColumnProfile(
            name=n,
            dtype="object",
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
        for n in names
    )
    return Profile(
        schema_version=1,
        tables=(TableProfile(name=table, row_count=3, columns=cols),),
        relationships=(),
        profiled_at=datetime(2026, 8, 29),
        decoy_engine_version="0.1.0",
    )


def _plan(config: dict[str, Any], source: pa.Table, **kw: Any) -> _dispatch.NativeRouteEvidence:
    profile = _profile(TABLE, *source.schema.names)
    return plan_native_route(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        first_schema=source.schema,
        registry=get_default_registry(),
        **kw,
    ).evidence


def _recs(evidence: _dispatch.NativeRouteEvidence) -> list[tuple[C, str | None, bool]]:
    return [(r.code, r.column, r.table_wide) for r in evidence.decline_records]


def _strings(n: int = 6, *names: str) -> pa.Table:
    return pa.table({name: pa.array([f"v{i}" for i in range(n)], pa.string()) for name in names})


# ---------------------------------------------------------------------------
# The vocabulary and the record shape.
# ---------------------------------------------------------------------------


def test_serialized_code_values_are_pinned() -> None:
    assert {c.name: c.value for c in C} == {
        "FK_RELATIONSHIP": "fk_relationship",
        "NO_MASK_NODES": "no_mask_nodes",
        "NON_SCALAR_NODE": "non_scalar_node",
        "ADAPTER_REQUESTED": "adapter_requested",
        "UNCOVERED_OR_MISSING_COLUMNS": "uncovered_or_missing_columns",
        "CRYPTO_EXT_UNAVAILABLE": "crypto_ext_unavailable",
        "FPE_EXT_UNAVAILABLE": "fpe_ext_unavailable",
        "INDEX_EXT_UNAVAILABLE": "index_ext_unavailable",
        "RAW_HEX_EXT_UNAVAILABLE": "raw_hex_ext_unavailable",
        "UNCONFIGURED_SET_MISMATCH": "unconfigured_set_mismatch",
        "EMPTY_INPUT": "empty_input",
        "STRATEGY_VETOED_CHUNKED": "strategy_vetoed_chunked",
        "GROUP_KEY_SELF_ANCHOR": "group_key_self_anchor",
        "GROUP_KEY_MASKED_SIBLING": "group_key_masked_sibling",
        "FALLBACK_POLICY_NOT_NATIVE": "fallback_policy_not_native",
        "NO_NATIVE_KERNEL_OR_POOL": "no_native_kernel_or_pool",
        "WHEN_PREDICATE_NOT_NATIVE": "when_predicate_not_native",
        "WHEN_OUTSIDE_SUBSET": "when_outside_subset",
        "WHEN_READS_MASKED_COLUMN": "when_reads_masked_column",
        "FAKER_SOURCE_NOT_STRING": "faker_source_not_string",
        "FAKER_SCHEMA_NOT_GUARANTEED": "faker_schema_not_guaranteed",
        "FAKER_METADATA_NOT_ALLOWLISTED": "faker_metadata_not_allowlisted",
        "FPE_CHECKSUM_NOT_NATIVE": "fpe_checksum_not_native",
        "HASH_CONFIG_REJECTED": "hash_config_rejected",
        "FAKER_PROVIDER_NOT_NATIVE": "faker_provider_not_native",
        "SOURCE_TYPE_NOT_STRING": "source_type_not_string",
        "GROUP_KEY_SIBLING_MISSING": "group_key_sibling_missing",
        "GROUP_KEY_SIBLING_TYPE_NOT_NATIVE": "group_key_sibling_type_not_native",
        "FAKER_PROVIDER_OUTPUT_NOT_STRING": "faker_provider_output_not_string",
        "OTHER": "other",
    }


def test_record_invariant_and_exact_payload_keys() -> None:
    with pytest.raises(ValueError):
        RouteDeclineRecord(C.OTHER, "t", None, False)
    with pytest.raises(ValueError):
        RouteDeclineRecord(C.OTHER, "t", "c", True)
    col = column_decline(C.FALLBACK_POLICY_NOT_NATIVE, "t", "c")
    wide = table_decline(C.NO_MASK_NODES, "t")
    assert set(col.to_payload()) == _PAYLOAD_KEYS == set(wide.to_payload())
    assert col.to_payload() == {
        "code": "fallback_policy_not_native",
        "table": "t",
        "column": "c",
        "table_wide": False,
    }
    assert wide.to_payload()["column"] is None and wide.to_payload()["table_wide"] is True
    assert decline_payload((col, wide)) == [col.to_payload(), wide.to_payload()]


def test_an_admitted_table_carries_no_records() -> None:
    ev = _plan(make_config([redact("r"), passthrough("p")]), _strings(4, "r", "p"))
    assert ev.native_admitted is True and ev.decline_records == ()


# ---------------------------------------------------------------------------
# Phase A: static admission.
# ---------------------------------------------------------------------------


def test_fk_relationship_is_table_wide() -> None:
    rel = [
        {
            "parent": {"table": "parents", "columns": ["id"]},
            "children": [{"table": TABLE, "columns": ["r"]}],
            "orphan_policy": "remap",
        }
    ]
    config = make_config(
        [redact("r")],
        extra_tables=[{"name": "parents", "columns": [redact("id")]}],
        relationships=rel,
    )
    ev = _plan(config, _strings(4, "r"))
    assert ev.reroute_reason == "fk_relationship_not_native_route"
    assert _recs(ev) == [(C.FK_RELATIONSHIP, None, True)]


def test_no_mask_nodes_is_table_wide() -> None:
    config = make_config([redact("r")])
    profile = _profile("ghost", "r")
    ev = plan_native_route(
        config,
        profile,
        table="ghost",
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    ).evidence
    assert ev.reroute_reason == "no_mask_nodes"
    assert ev.decline_records == (table_decline(C.NO_MASK_NODES, "ghost"),)


def test_composite_nodes_give_one_table_wide_record_beside_a_column_record() -> None:
    def comp(name: str, others: list[str]) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "composite_name_email",
            "namespace": "ns",
            "coherent_with": others,
        }

    cols = [
        comp("first_name", ["last_name", "email"]),
        comp("last_name", ["first_name", "email"]),
        comp("email", ["first_name", "last_name"]),
        force_oracle("c"),
    ]
    ev = _plan(make_config(cols), _strings(4, "first_name", "last_name", "email", "c"))
    assert sorted(_recs(ev), key=lambda r: r[0].value) == [
        (C.FALLBACK_POLICY_NOT_NATIVE, "c", False),
        (C.NON_SCALAR_NODE, None, True),
    ]
    assert sum(r.table_wide for r in ev.decline_records) == 1


def test_fallback_policy_not_native_names_its_column() -> None:
    ev = _plan(make_config([redact("r"), force_oracle("c")]), _strings(4, "r", "c"))
    assert ev.reroute_reason == FORCE_REASON.replace(FORCE, "c")
    assert _recs(ev) == [(C.FALLBACK_POLICY_NOT_NATIVE, "c", False)]


def test_strategy_veto_and_missing_kernel_are_column_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, src = make_config([redact("r")]), _strings(4, "r")
    monkeypatch.setattr(_dispatch, "CHUNKED_ROUTE_VETOED_STRATEGIES", frozenset({"redact"}))
    assert _recs(_plan(config, src)) == [(C.STRATEGY_VETOED_CHUNKED, "r", False)]
    monkeypatch.undo()
    monkeypatch.setattr(_dispatch, "NATIVE_KERNEL_STRATEGIES", frozenset())
    assert _recs(_plan(config, src)) == [(C.NO_NATIVE_KERNEL_OR_POOL, "r", False)]


def test_group_key_self_anchor_and_masked_sibling() -> None:
    src = gk_source(["a", "b", "a", "c"])
    own = _plan(make_config([gk_col(group_by=TARGET), passthrough("p")]), src)
    assert _recs(own) == [(C.GROUP_KEY_SELF_ANCHOR, TARGET, False)]
    masked = _plan(make_config([redact(GB), gk_col(), passthrough("p")]), src)
    assert _recs(masked) == [(C.GROUP_KEY_MASKED_SIBLING, TARGET, False)]


# ---------------------------------------------------------------------------
# `when:` (post-static, kept in decision order).
# ---------------------------------------------------------------------------


def _when(col: dict[str, Any], expr: str) -> dict[str, Any]:
    return {**col, "when": expr}


def test_when_declines_map_to_their_three_codes() -> None:
    reg = get_default_registry()
    schema = pa.schema(
        [("s", pa.string()), ("p", pa.string()), ("z", pa.string()), ("a", pa.string())]
    )

    def first(cols: list[dict[str, Any]]) -> RouteDeclineRecord:
        found = first_when_decline(make_config(cols), reg, table=TABLE, schema=schema)
        assert found is not None
        return found.record

    assert first([_when(passthrough("s"), "p == 'x'"), passthrough("p")]) == column_decline(
        C.WHEN_PREDICATE_NOT_NATIVE, TABLE, "s"
    )
    # A raw predicate outside the grammar is refused at compile, so the verdict is read from
    # the admission source directly, as the existing `when:` decline tests do.
    assert first([_when(redact("s"), "p.notnull()"), passthrough("p")]) == column_decline(
        C.WHEN_OUTSIDE_SUBSET, TABLE, "s"
    )
    reads = [_when(redact("z"), "a == 'REDACTED'"), redact("a"), passthrough("p")]
    assert first(reads) == column_decline(C.WHEN_READS_MASKED_COLUMN, TABLE, "z")
    assert _recs(_plan(make_config(reads), _strings(4, "z", "a", "p"))) == [
        (C.WHEN_READS_MASKED_COLUMN, "z", False)
    ]


def test_when_override_keeps_earlier_records_and_the_legacy_reason() -> None:
    config = make_config(
        [force_oracle("c"), _when(redact("z"), "a == 'REDACTED'"), redact("a"), passthrough("p")]
    )
    ev = _plan(config, _strings(4, "c", "z", "a", "p"))
    # The `when:` reason replaces the composite string (existing behavior) but the earlier
    # static record survives, in decision order.
    assert (ev.reroute_reason or "").startswith("when_predicate_reads_masked_column:z:a")
    assert _recs(ev) == [
        (C.FALLBACK_POLICY_NOT_NATIVE, "c", False),
        (C.WHEN_READS_MASKED_COLUMN, "z", False),
    ]


# ---------------------------------------------------------------------------
# Phase B: adapter, coverage, Faker, real types, companions.
# ---------------------------------------------------------------------------


def test_adapter_requested_is_table_wide() -> None:
    ev = _plan(make_config([redact("r")]), _strings(4, "r"), adapter=object())
    assert ev.reroute_reason == "adapter_requested"
    assert _recs(ev) == [(C.ADAPTER_REQUESTED, None, True)]


def test_uncovered_or_missing_columns_is_table_wide() -> None:
    ev = _plan(make_config([redact("r")]), _strings(4, "r", "extra"))
    assert (ev.reroute_reason or "").startswith("uncovered_columns")
    assert _recs(ev) == [(C.UNCOVERED_OR_MISSING_COLUMNS, None, True)]


def test_faker_non_string_source_schema_and_metadata_declines() -> None:
    floats = pa.table({"f": pa.array([1.5, 2.5], pa.float64())})
    assert _recs(_plan(make_config([faker_col("f")]), floats)) == [
        (C.FAKER_SOURCE_NOT_STRING, "f", False)
    ]
    ints = pa.table({"f": pa.array([1, 2], pa.int64())})
    det = {**faker_col("f"), "deterministic": True}
    no_guarantee = _plan(make_config([det]), ints)
    assert _recs(no_guarantee) == [(C.FAKER_SCHEMA_NOT_GUARANTEED, "f", False)]
    other = C5cIiAdmissionContext(source_schema=pa.schema([("zzz", pa.int64())]))
    meta = _plan(make_config([det]), ints, c5c_ii_admission=other)
    assert _recs(meta) == [(C.FAKER_METADATA_NOT_ALLOWLISTED, "f", False)]
    # The reason-only wrapper keeps its legacy string.
    direct = deterministic_faker_decline(
        "f", table="t", source_type=pa.int64(), admission=None, provider_allowlisted=True
    )
    assert direct is not None and direct.reason == "faker_conversion_schema_not_guaranteed:f"


def test_real_type_declines_carry_the_node_column() -> None:
    def node(column: str, strategy: str) -> tuple[NodeRouteRecord, ...]:
        return (NodeRouteRecord(column=column, strategy=strategy, route="native_kernel"),)

    def decline(config: dict[str, Any], nodes: Any, schema: pa.Schema) -> RouteDeclineRecord:
        found = real_type_decline(config, nodes, schema, table=TABLE, profile=None)
        assert found is not None
        return found.record

    fpe = {
        "name": "c",
        "strategy": "fpe",
        "namespace": "n",
        "provider_config": {"charset": "digits", "checksum": "luhn"},
    }
    schema = pa.schema([("c", pa.string())])
    assert decline(make_config([fpe]), node("c", "fpe"), schema) == column_decline(
        C.FPE_CHECKSUM_NOT_NATIVE, TABLE, "c"
    )
    date = pa.schema([("h", pa.date32())])
    assert decline(make_config([hash_col("h")]), node("h", "hash"), date) == column_decline(
        C.HASH_CONFIG_REJECTED, TABLE, "h"
    )
    cfg = make_config([{**faker_col("f"), "provider": "company"}])
    assert decline(cfg, node("f", "faker"), pa.schema([("f", pa.string())])) == column_decline(
        C.FAKER_PROVIDER_NOT_NATIVE, TABLE, "f"
    )
    large = pa.schema([("k", pa.large_string())])
    assert decline(make_config([categorical("k")]), node("k", "categorical"), large) == (
        column_decline(C.SOURCE_TYPE_NOT_STRING, TABLE, "k")
    )
    gk = make_config([passthrough(GB), gk_col()])
    assert decline(gk, node(TARGET, "group_key"), pa.schema([(TARGET, pa.string())])) == (
        column_decline(C.GROUP_KEY_SIBLING_MISSING, TABLE, TARGET)
    )
    wide = pa.schema([(GB, pa.int32()), (TARGET, pa.string())])
    assert decline(gk, node(TARGET, "group_key"), wide) == column_decline(
        C.GROUP_KEY_SIBLING_TYPE_NOT_NATIVE, TABLE, TARGET
    )


@pytest.mark.parametrize(
    ("loader", "config_cols", "source", "code", "reason"),
    [
        (
            "load_compiled_crypto_kernel",
            [hash_col("h")],
            _strings(4, "h"),
            C.CRYPTO_EXT_UNAVAILABLE,
            "crypto_extension_unavailable",
        ),
        (
            "load_compiled_fpe_kernel",
            [
                {
                    "name": "c",
                    "strategy": "fpe",
                    "namespace": "n",
                    "provider_config": {"charset": "digits"},
                }
            ],
            _strings(4, "c"),
            C.FPE_EXT_UNAVAILABLE,
            "fpe_extension_unavailable",
        ),
        (
            "load_compiled_index_kernel",
            [categorical("k")],
            _strings(4, "k"),
            C.INDEX_EXT_UNAVAILABLE,
            "index_extension_unavailable",
        ),
    ],
)
def test_companion_probe_declines_are_table_wide(
    monkeypatch: pytest.MonkeyPatch,
    loader: str,
    config_cols: list[dict[str, Any]],
    source: pa.Table,
    code: C,
    reason: str,
) -> None:
    def boom() -> None:
        raise CryptoExtensionUnavailableError("HOSTILE_EXC_MARKER")

    monkeypatch.setattr(_dispatch, loader, boom)
    ev = _plan(make_config(config_cols), source)
    assert ev.reroute_reason == reason
    assert _recs(ev) == [(code, None, True)]
    assert "HOSTILE_EXC_MARKER" not in json.dumps(decline_payload(ev.decline_records))


def test_raw_hex_probe_decline_is_table_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise CryptoExtensionUnavailableError("x")

    monkeypatch.setattr(_dispatch, "load_compiled_raw_hex_kernel", boom)
    ev = _plan(
        make_config([passthrough(GB), gk_col(), passthrough("p")]), gk_source(["a", "b", "a"])
    )
    assert ev.reroute_reason == "raw_hex_extension_unavailable"
    assert _recs(ev) == [(C.RAW_HEX_EXT_UNAVAILABLE, None, True)]


# ---------------------------------------------------------------------------
# Phase C (chunked entry) and the public payload.
# ---------------------------------------------------------------------------


def test_unconfigured_set_mismatch_is_table_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_chunked_entry, "known_output_columns", lambda *a, **k: frozenset())
    run = run_one(make_config([redact("r"), passthrough("p")]), split(_strings(6, "r", "p"), 3))
    assert (run.ev[0].reroute_reason or "").startswith("unconfigured_set_mismatch")
    assert _recs(run.ev[0]) == [(C.UNCONFIGURED_SET_MISMATCH, None, True)]


@NEEDS_COMPANION
def test_faker_provider_output_not_string_is_column_scoped() -> None:
    from tests.native.test_chunked_entry_gate_findings import _DateAdapter

    default = get_default_registry()
    registry = default.override(
        "person_first_name", _DateAdapter(), default.get_capabilities("person_first_name")
    )
    src = pa.table({"f": pa.array(["a", "b", None, "c", "d"], pa.string())})
    run = run_one(make_config([faker_col("f")]), split(src, 2), registry=registry)
    assert run.ev[0].reroute_reason == "faker_provider_output_not_string:f:person_first_name"
    assert _recs(run.ev[0]) == [(C.FAKER_PROVIDER_OUTPUT_NOT_STRING, "f", False)]


def test_empty_input_stays_internal() -> None:
    from decoy_engine import run_mask_chunked

    sink: list[Any] = []
    list(
        run_mask_chunked(
            make_config([redact("r")]),
            [],
            table=TABLE,
            engine_version=ENGINE_VERSION,
            route_evidence_sink=sink,
        )
    )
    assert sink[0].reroute_reason == "empty_input"
    assert sink[0].decline_records == ()


def test_records_reach_the_public_payload_on_every_chunk_and_the_fold() -> None:
    from decoy_engine.execution._chunked_output_sink import OutputEvidenceAccumulator

    config = make_config([redact("r"), force_oracle("c")])
    src = _strings(11, "r", "c")
    run = run_one(config, split(src, 4))
    want = [column_decline(C.FALLBACK_POLICY_NOT_NATIVE, TABLE, "c").to_payload()]
    assert len(run.sink) == 3
    for res in run.sink:
        assert res.quality_metrics["chunked_route"]["decline_records"] == want
    resident = aggregate_chunked_route_evidence(run.sink)
    acc = OutputEvidenceAccumulator()
    for res in run.sink:
        acc.append(res)
    assert resident["decline_records"] == want == acc.route_evidence()["decline_records"]
    assert resident == acc.route_evidence()
    assert json.loads(json.dumps(resident, allow_nan=False)) == resident
    # Removing elapsed time (as the finalizer does) keeps the field.
    from decoy_engine.execution._pipeline_auto_chunk import _without_elapsed

    assert _without_elapsed(resident)["decline_records"] == want
    assert all(set(r) == _PAYLOAD_KEYS for r in resident["decline_records"])


def test_hostile_text_is_confined_to_the_identifier_fields() -> None:
    hostile_col = "evil:'; DROP TABLE x;--"
    config = make_config(
        [
            {**faker_col(hostile_col), "provider": "HOSTILE_PROVIDER_MARKER"},
            _when(passthrough("w"), "HOSTILE_EXPR_MARKER == 'x'"),
            redact("a"),
        ]
    )
    # A provider the registry does not know cannot compile, so exercise the sources that
    # take free text directly: the real-type source (provider) and the `when:` source.
    nodes = (NodeRouteRecord(column=hostile_col, strategy="faker", route="native_kernel"),)
    found = real_type_decline(
        config, nodes, pa.schema([(hostile_col, pa.string())]), table=TABLE, profile=None
    )
    assert found is not None and "HOSTILE_PROVIDER_MARKER" in found.reason
    payload = json.dumps(decline_payload((found.record,)))
    assert "HOSTILE_PROVIDER_MARKER" not in payload
    assert found.record.column == hostile_col
    hostile_when = make_config([_when(redact("w"), "HOSTILE_EXPR_MARKER.x()"), passthrough("p")])
    schema = pa.schema([("w", pa.string()), ("p", pa.string())])
    when = first_when_decline(hostile_when, get_default_registry(), table=TABLE, schema=schema)
    assert when is not None and when.record.code is C.WHEN_OUTSIDE_SUBSET
    assert "HOSTILE_EXPR_MARKER" not in json.dumps(decline_payload((when.record,)))
