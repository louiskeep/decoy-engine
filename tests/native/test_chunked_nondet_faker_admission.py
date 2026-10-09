"""C5b-ii acceptance: two-stage admission, the config veto, `when:`, FK and closed routes.

Stage A is config only (non-deterministic REUSE, an explicit `pool_size`, a provider in the C1
allowlist, no UNIQUE/MATCH/SCALE mode; the namespace is optional) and is read by four chunked
consumers: the compatibility veto, `_static_route_decision`, `plan_column_backends` and the
stage-A predicate itself, which the string pin and the preparation also use. Stage B is the
source dtype and only picks the leg: a non-string source runs the chunked-oracle leg, never a
crash (plan section 5, tests 4 to 8, 11).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._chunked import (
    _conditional_admission_failures,
    check_chunked_compatibility,
)
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native import _chunked_entry, _dispatch
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.generation.pool import PoolCapacityError
from decoy_engine.plan import compile_plan
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import run_one, with_force
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    column_values,
    force_oracle,
    hash_col,
    key_provider,
    split,
)
from tests.native._chunked_faker_support import (
    FORCE,
    make_config,
    nd_faker,
    passthrough,
    source,
)
from tests.native.test_chunked_entry_values_schema import _full_frame

VETO = "chunked_strategy_conditions_unmet"
WHEN_CODE = "chunked_faker_nondeterministic_when_not_supported"
_REG = get_default_registry()


def _check(columns: list[dict[str, Any]]) -> None:
    check_chunked_compatibility(make_config(columns), table=TABLE, registry=_REG)


def _code(columns: list[dict[str, Any]]) -> str | None:
    try:
        _check(columns)
    except PlanCompileError as exc:
        return exc.code
    return None


# Stage-A cases: (id, column, positional_admissible). Non-admissible non-deterministic REUSE
# columns fail the veto with the retained code; UNIQUE, MATCH and SCALE keep their old text.
_STAGE_A: list[tuple[str, dict[str, Any], bool]] = [
    ("with_namespace", nd_faker(namespace="ns_f"), True),
    ("without_namespace", nd_faker(), True),
    ("last_name_provider", nd_faker(provider="person_last_name"), True),
    (
        "pool_size_in_provider_config",
        nd_faker(pool_size=None, provider_config={"pool_size": 50}),
        True,
    ),
    ("missing_pool_size", nd_faker(pool_size=None), False),
    ("off_allowlist_provider", nd_faker(provider="address_city"), False),
    ("date_provider", nd_faker(provider="person_dob"), False),
    ("unique", nd_faker(namespace="ns_f", cardinality_mode="unique"), False),
    ("match", nd_faker(namespace="ns_f", cardinality_mode="match_source_cardinality"), False),
    ("scale", nd_faker(namespace="ns_f", cardinality_mode="scale_source_cardinality"), False),
]
_STAGE_A_IDS = [c[0] for c in _STAGE_A]


# ---------------------------------------------------------------------------
# 4. Stage-A agreement across the chunked consumers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "col", "admissible"), _STAGE_A, ids=_STAGE_A_IDS)
def test_the_config_veto_admits_only_stage_a_candidates(
    case: str, col: dict[str, Any], admissible: bool
) -> None:
    assert (_code([col, passthrough("p")]) is None) is admissible


def _first_chunk() -> pa.Table:
    return source(["a", "b", "c", "a"])


def _compiled(config: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    profile = first_chunk_profile(_first_chunk(), table=TABLE, engine_version=ENGINE_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION, no_profile=True)
    seeds = dict(next(ts for (n, ts) in plan.seed_envelope.per_table if n == TABLE).per_column)
    return profile, seeds


@pytest.mark.parametrize(("case", "col", "admissible"), _STAGE_A, ids=_STAGE_A_IDS)
def test_the_stage_a_consumers_return_the_identical_config_verdict(
    case: str, col: dict[str, Any], admissible: bool
) -> None:
    from decoy_engine.execution.native._chunked_schema_rule import faker_positional_pinned_columns
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_for_column,
        positional_faker_config_of_entry,
    )

    config = make_config([col, passthrough("p")])
    try:
        profile, _seeds = _compiled(config)
    except PoolCapacityError:
        # A whole-column mode cannot compile without a profile, so the route consumers never
        # run: the veto, which precedes the compile, is the only verdict that exists.
        static_admitted, evidence_native = False, False
    else:
        static = _dispatch._static_route_decision(
            config, profile, table=TABLE, engine_version=ENGINE_VERSION, registry=_REG
        )
        backends = {
            c.column: c.planned_backend
            for c in plan_column_backends(
                config, profile, table=TABLE, engine_version=ENGINE_VERSION, registry=_REG
            )
        }
        static_admitted, evidence_native = static.native_admitted, backends["f"] == "rust_companion"
    entry = next(c for c in config["tables"][0]["columns"] if c["name"] == "f")
    verdicts = {
        "veto": _code([col, passthrough("p")]) is None,
        "static_route": static_admitted,
        "evidence": evidence_native,
        "predicate": positional_faker_config_of_entry(entry) is not None,
        "by_column": positional_faker_config_for_column(config, TABLE, "f") is not None,
        "pin": "f" in faker_positional_pinned_columns({"f": entry}),
    }
    assert set(verdicts.values()) == {admissible}, verdicts


def test_the_predicate_keeps_the_configured_namespace_only() -> None:
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_of_entry,
    )

    entry = make_config([nd_faker(namespace="ns_f")])["tables"][0]["columns"][0]
    unset = make_config([nd_faker()])["tables"][0]["columns"][0]
    blank = make_config([nd_faker(namespace="")])["tables"][0]["columns"][0]
    assert positional_faker_config_of_entry(entry).namespace == "ns_f"  # type: ignore[union-attr]
    assert not positional_faker_config_of_entry(unset).namespace  # type: ignore[union-attr]
    assert not positional_faker_config_of_entry(blank).namespace  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "entry",
    [
        {
            "name": "f",
            "strategy": "faker",
            "provider": "person_first_name",
            "deterministic": True,
            "namespace": "ns",
            "pool_size": 40,
        },
        {"name": "f", "strategy": "nested", "provider": "person_first_name"},
        {
            "name": "f",
            "strategy": "faker",
            "provider": "composite_person",
            "deterministic": False,
            "pool_size": 40,
        },
        {"name": "f", "strategy": "hash", "namespace": "ns"},
    ],
    ids=["deterministic", "nested", "composite", "other_strategy"],
)
def test_the_predicate_declines_deterministic_nested_composite_and_other_strategies(
    entry: dict[str, Any],
) -> None:
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_of_entry,
    )

    assert positional_faker_config_of_entry(entry) is None


def test_the_allow_collisions_alias_is_deterministic_and_keeps_its_veto() -> None:
    """`allow_collisions` compiles to a deterministic faker, so it is not position-keyed."""
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_of_entry,
    )

    col = nd_faker(namespace="ns_f", allow_collisions=True)
    entry = make_config([col])["tables"][0]["columns"][0]
    assert positional_faker_config_of_entry(entry) is None
    assert _code([col, passthrough("p")]) == VETO


def test_the_column_lookup_matches_both_the_table_and_the_column_name() -> None:
    from decoy_engine.execution.native._faker_positional_admission import (
        positional_faker_config_for_column,
    )

    config = make_config([nd_faker(), passthrough("p")])
    assert positional_faker_config_for_column(config, TABLE, "f") is not None
    assert positional_faker_config_for_column(config, "other", "f") is None
    assert positional_faker_config_for_column(config, TABLE, "p") is None
    assert positional_faker_config_for_column(config, TABLE, "missing") is None


# ---------------------------------------------------------------------------
# 5. Fails closed at the veto: names the missing input, never reaches the oracle.
# ---------------------------------------------------------------------------


def test_a_missing_pool_size_names_the_input_and_drops_the_deferral_text() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([nd_faker(name="tier", pool_size=None), passthrough("p")])
    assert info.value.code == VETO
    assert info.value.path == f"tables.{TABLE}.columns"
    assert "tier" in info.value.message and "pool_size" in info.value.message
    assert "C5b-ii" not in info.value.message and "deferred" not in info.value.message


def test_an_off_allowlist_provider_names_the_provider_and_the_allowlist() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([nd_faker(provider="address_city"), passthrough("p")])
    assert info.value.code == VETO
    assert "address_city" in info.value.message and "allowlist" in info.value.message


@pytest.mark.parametrize("mode", ["unique", "match_source_cardinality", "scale_source_cardinality"])
def test_the_whole_column_modes_keep_their_rejection_text(mode: str) -> None:
    failures = _conditional_admission_failures(
        make_config([nd_faker(namespace="ns_f", cardinality_mode=mode)])["tables"][0]["columns"][0]
    )
    joined = " ".join(failures)
    assert "whole-column draw" in joined and "not chunk-safe" in joined
    assert "chunk-variant" not in joined


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
@pytest.mark.parametrize("case", [c for c in _STAGE_A if not c[2]], ids=lambda c: c[0])
def test_a_non_admissible_config_fails_before_any_chunk_and_never_reaches_the_oracle(
    entry: str, case: tuple[str, dict[str, Any], bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle_calls: list[int] = []
    real = _chunked_entry._oracle_route

    def counting(*a: Any, **k: Any) -> Any:
        oracle_calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(_chunked_entry, "_oracle_route", counting)
    consumed: list[int] = []

    def stream() -> Iterator[pa.Table]:
        for chunk in split(source(["a", "b", "c", "a"]), 2):
            consumed.append(1)
            yield chunk

    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                make_config([case[1], passthrough("p")]),
                stream(),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert info.value.code == VETO
    assert consumed == [] and oracle_calls == []


# ---------------------------------------------------------------------------
# 6. Leg selection does not crash (C1b-ii lesson).
# ---------------------------------------------------------------------------


def _typed_source(kind: str, n: int = 10) -> pa.Table:
    column = {
        "int64": pa.array(range(n), pa.int64()),
        "float64": pa.array([None if i % 4 == 1 else float(i) for i in range(n)], pa.float64()),
        "dictionary": pa.array([f"v{i % 3}" for i in range(n)]).dictionary_encode(),
        "null": pa.nulls(n),
    }[kind]
    return pa.table({"f": column, "p": pa.array(range(n), pa.int64())})


@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
@pytest.mark.parametrize("kind", ["dictionary", "null"])
def test_a_non_string_source_runs_the_chunked_oracle_leg_and_equals_whole_frame(
    kind: str, namespace: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle_calls: list[int] = []
    real = _chunked_entry._oracle_route

    def counting(*a: Any, **k: Any) -> Any:
        oracle_calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(_chunked_entry, "_oracle_route", counting)
    table = _typed_source(kind)
    config = make_config([nd_faker(namespace=namespace), passthrough("p")])
    run = run_one(config, split(table, 4))
    ev = run.ev[0]
    assert oracle_calls == [1], "the non-string source must take the chunked-oracle leg"
    assert ev.native_admitted is False
    assert f"faker_source_type_not_string:f:{table.schema.field('f').type}" in (
        ev.reroute_reason or ""
    )
    assert ev.compiled_kernel_executed is False and ev.pool_select_calls == 0
    executed = {
        col["executed_backend"]
        for r in run.sink
        for col in r.quality_metrics["chunked_route"]["columns"]
        if col["column"] == "f"
    }
    assert executed == {"pandas_oracle"}
    assert {o.schema.field("f").type for o in run.out} == {pa.string()}
    again = run_one(config, split(table, 4))
    assert column_values(again.out, "f") == column_values(run.out, "f")
    full = _full_frame(config, table, tmp_path)
    assert column_values(run.out, "f") == full.column("f").to_pylist()


# ---------------------------------------------------------------------------
# 6c. Non-allowlisted providers stay whole-frame (Codex round 2).
# ---------------------------------------------------------------------------


class _IntAdapter:
    """A poolable adapter whose pool values are integers, not strings."""

    backend_type = "test_int"
    backend_version = "1"

    def __init__(self, provider: str) -> None:
        self._provider = provider

    def generate(self, provider: str, *, spec: Any, source_value: bytes | None = None) -> Any:
        return 7

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[int]:
        return list(range(100, 100 + count))

    def capability_matrix(self, provider: str) -> Any:
        return get_default_registry().get_capabilities(self._provider)


def _override(provider: str) -> Any:
    default = get_default_registry()
    return default.override(provider, _IntAdapter(provider), default.get_capabilities(provider))


def _mixed_job(tmp_path: Path, provider: str) -> tuple[dict[str, Any], pa.Table]:
    from tests.unit.execution import _auto_chunk_support as support

    values = [None if i % 3 == 0 else f"v{i}" for i in range(support.ROWS)]
    src = pa.table({"f": pa.array(values, pa.string()), "p": pa.array(range(support.ROWS))})
    cfg = support.make_cfg(
        [nd_faker(provider=provider), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


@pytest.mark.parametrize(
    ("provider", "registry"),
    [("person_dob", None), ("address_zip", "int")],
    ids=["date_output", "numeric_output"],
)
def test_a_non_allowlisted_provider_stays_whole_frame_and_equals_the_forced_run(
    provider: str, registry: str | None, tmp_path: Path
) -> None:
    from decoy_engine.execution import run_pipeline
    from tests.unit.execution import _auto_chunk_support as support

    cfg, src = _mixed_job(tmp_path, provider)
    extra: dict[str, Any] = {"registry": _override(provider)} if registry else {}
    auto = run_pipeline(cfg, sources={support.TABLE: src}, **support.run_kwargs(**extra))
    full = run_pipeline(
        cfg, sources={support.TABLE: src}, **support.run_kwargs(auto_chunk=False, **extra)
    )
    block = auto.quality_metrics["auto_chunk"]
    assert block["mode"] == "full_frame"
    assert block["reason"].startswith(VETO)
    assert auto.outputs[support.TABLE].equals(full.outputs[support.TABLE], check_metadata=True)


# ---------------------------------------------------------------------------
# 6d. An allowlisted name overridden with non-string pool values fails closed on every path.
# ---------------------------------------------------------------------------

POOL_CODE = "chunked_faker_nondeterministic_pool_not_string"


def _no_index_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError

    def _raise() -> Any:
        raise CryptoExtensionUnavailableError("index kernel unavailable for the test")

    monkeypatch.setattr(_dispatch, "load_compiled_index_kernel", _raise)


def _override_run(columns: list[dict[str, Any]], chunks: list[pa.Table], **kw: Any) -> Any:
    writes: list[Any] = []
    with pytest.raises(Exception) as info:
        run = run_one(
            make_config(columns),
            chunks,
            registry=_override("person_first_name"),
            vault=True,
            **kw,
        )
        writes.append(run)
    assert not writes, "no chunk may be written"
    return info.value


_STRINGS = ["a", "b", None, "c", "d", "e"]


def _assert_pool_error(exc: BaseException) -> None:
    assert getattr(exc, "code", None) == POOL_CODE, exc
    message = str(exc)
    assert "f" in message and "person_first_name" in message
    assert "auto" in message.lower() or "provider" in message.lower()


@NEEDS_COMPANION
def test_native_admitted_override_fails_closed_before_any_write() -> None:
    exc = _override_run([nd_faker(), passthrough("p")], split(source(_STRINGS), 2))
    _assert_pool_error(exc)


@NEEDS_COMPANION
def test_override_downgraded_by_a_non_string_source_fails_closed() -> None:
    chunks = split(_typed_source("dictionary"), 4)
    exc = _override_run([nd_faker(), passthrough("p")], chunks)
    _assert_pool_error(exc)


def test_override_downgraded_by_companion_absence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_index_kernel(monkeypatch)
    exc = _override_run([nd_faker(), passthrough("p")], split(source(_STRINGS), 2))
    _assert_pool_error(exc)


def test_override_downgraded_by_another_columns_rejection_fails_closed() -> None:
    chunks = [with_force(c) for c in split(source(_STRINGS), 2)]
    exc = _override_run([nd_faker(), passthrough("p"), force_oracle(FORCE)], chunks)
    _assert_pool_error(exc)


def test_override_through_the_streamed_sink_writes_nothing(tmp_path: Path) -> None:
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _b6a_support as b6a

    src = pa.table({"f": pa.array(_STRINGS * 8, pa.string()), "p": pa.array(range(48))})
    cfg = support.make_cfg(
        [nd_faker(), support.pass_col("p")],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    spill = tmp_path / "spill"
    spill.mkdir()
    sink, _target = b6a.real_sink(spill)
    with pytest.raises(Exception) as info:
        b6a.run_streamed(cfg, src, sink, registry=_override("person_first_name"))
    _assert_pool_error(info.value)
    assert sink.count("write_batches") == 0 and sink.count("commit") == 0


@NEEDS_COMPANION
def test_a_deterministic_faker_with_the_override_keeps_its_downgrade() -> None:
    from tests.native._chunked_entry_support import faker_col

    run = run_one(
        make_config([faker_col("f")]),
        split(pa.table({"f": pa.array(_STRINGS, pa.string())}), 2),
        registry=_override("person_first_name"),
    )
    assert run.ev[0].native_admitted is False
    assert run.ev[0].reroute_reason == "faker_provider_output_not_string:f:person_first_name"


# ---------------------------------------------------------------------------
# 7. `when:` is rejected on the chunked route.
# ---------------------------------------------------------------------------


# C8-iii-d-2: a closed-grammar `when:` is no longer vetoed at config time (it runs natively, or
# the per-chunk guard rejects a non-string target/reference at runtime); only a predicate OUTSIDE
# the closed grammar, whose references cannot be checked, is still refused at config time.
_OUTSIDE_GRAMMAR = "p + 1 > 2"


def test_a_positional_faker_with_an_outside_grammar_when_is_rejected_at_config() -> None:
    assert _code([nd_faker(when=_OUTSIDE_GRAMMAR), passthrough("p")]) == WHEN_CODE


def test_a_positional_faker_with_a_closed_grammar_when_is_not_vetoed_at_config() -> None:
    # The config veto no longer fires; a numeric reference declines later, via the per-chunk guard
    # (covered in tests/native/test_c8_iii_d2_native_positional_when.py).
    assert _code([nd_faker(when="p > 1"), passthrough("p")]) is None


def test_the_when_rejection_names_the_column_and_path() -> None:
    with pytest.raises(PlanCompileError) as info:
        _check([nd_faker(name="tier", when=_OUTSIDE_GRAMMAR), passthrough("p")])
    assert info.value.code == WHEN_CODE
    assert info.value.path == f"tables.{TABLE}.columns"
    assert "tier" in info.value.message


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_the_when_rejection_fires_before_any_chunk_on_both_entries(entry: str) -> None:
    consumed: list[int] = []

    def stream() -> Iterator[pa.Table]:
        for chunk in split(source(["a", "b", "c", "a"]), 2):
            consumed.append(1)
            yield chunk

    run = run_mask_chunked if entry == "run_mask_chunked" else run_mask_pipeline_chunked
    with pytest.raises(PlanCompileError) as info:
        list(
            run(
                make_config([nd_faker(when=_OUTSIDE_GRAMMAR), passthrough("p")]),
                stream(),
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert info.value.code == WHEN_CODE and consumed == []


def test_a_deterministic_faker_with_when_is_unaffected() -> None:
    from tests.native._chunked_entry_support import faker_col

    assert _code([{**faker_col("f"), "when": "p > 1"}, passthrough("p")]) is None


def test_a_non_admissible_faker_with_when_keeps_the_retained_code() -> None:
    assert _code([nd_faker(pool_size=None, when="p > 1"), passthrough("p")]) == VETO


def test_a_blank_when_does_not_trigger_the_when_rejection() -> None:
    assert _code([nd_faker(when="  "), passthrough("p")]) is None


def test_the_when_gate_ignores_a_non_faker_column() -> None:
    from decoy_engine.execution.native._faker_positional_admission import (
        reject_nondeterministic_faker_when,
    )

    cfg = {"columns": [{"name": "c", "strategy": "hash", "when": "p > 1", "namespace": "n"}]}
    reject_nondeterministic_faker_when(cfg, table=TABLE)


# ---------------------------------------------------------------------------
# 8. FK, both orientations tested separately.
# ---------------------------------------------------------------------------


def _fk_config(*, child_key_faker: bool) -> dict[str, Any]:
    parent = {"name": "parent", "columns": [{**hash_col("id", "ns_k"), "dtype": "string"}]}
    key = (
        {**nd_faker("k", namespace="ns_k"), "dtype": "string"}
        if child_key_faker
        else {**hash_col("k", "ns_k"), "dtype": "string"}
    )
    return make_config(
        [key, nd_faker("f"), passthrough("p")],
        extra_tables=[parent],
        relationships=[
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": TABLE, "columns": ["k"]}],
                "orphan_policy": "remap",
            }
        ],
    )


def _fk_source(n: int) -> pa.Table:
    return pa.table(
        {
            "k": pa.array([f"key{i}" for i in range(n)], pa.string()),
            "f": pa.array([f"v{i % 4}" for i in range(n)], pa.string()),
            "p": pa.array(list(range(n)), pa.int64()),
        }
    )


def test_a_positional_faker_as_a_child_fk_key_is_rejected_by_the_existing_gate() -> None:
    with pytest.raises(PlanCompileError) as info:
        check_chunked_compatibility(_fk_config(child_key_faker=True), table=TABLE, registry=_REG)
    assert info.value.code.startswith("chunked_fk_"), info.value.code


def test_a_table_touched_by_a_declared_relationship_stays_off_native() -> None:
    config = _fk_config(child_key_faker=False)
    small = run_one(config, split(_fk_source(40), 7))
    large = run_one(config, split(_fk_source(40), 40))
    for run in (small, large):
        assert run.ev[0].native_admitted is False
        assert run.ev[0].reroute_reason == "fk_relationship_not_native_route"
    assert column_values(small.out, "f") == column_values(large.out, "f")


def test_a_parent_only_chunked_run_equals_the_whole_frame_run_on_the_oracle_leg(
    tmp_path: Path,
) -> None:
    parent_cols = [{**hash_col("id", "ns_k"), "dtype": "string"}, nd_faker("f"), passthrough("p")]
    child = {"name": "child", "columns": [{**hash_col("k", "ns_k"), "dtype": "string"}]}
    config = make_config(
        parent_cols,
        extra_tables=[child],
        relationships=[
            {
                "parent": {"table": TABLE, "columns": ["id"]},
                "children": [{"table": "child", "columns": ["k"]}],
                "namespace": "ns_k",
                "orphan_policy": "remap",
            }
        ],
    )
    table = pa.table(
        {
            "id": pa.array([f"key{i}" for i in range(30)], pa.string()),
            "f": pa.array([f"v{i % 4}" for i in range(30)], pa.string()),
            "p": pa.array(range(30), pa.int64()),
        }
    )
    run = run_one(config, split(table, 7))
    assert run.ev[0].native_admitted is False
    assert run.ev[0].reroute_reason == "fk_relationship_not_native_route"
    full = _full_frame_of_parent(config, table, tmp_path)
    assert column_values(run.out, "f") == full.column("f").to_pylist()


def _full_frame_of_parent(config: dict[str, Any], table: pa.Table, tmp_path: Path) -> pa.Table:
    import copy

    import pyarrow.parquet as pq

    from decoy_engine.execution import run_pipeline

    cfg = copy.deepcopy(config)
    path = str(tmp_path / "parent.parquet")
    pq.write_table(table, path)
    child_path = str(tmp_path / "child.parquet")
    pq.write_table(pa.table({"k": table.column("id")}), child_path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["sources"]["child"] = {"type": "file", "format": "parquet", "path": child_path}
    result = run_pipeline(
        cfg,
        {TABLE: table, "child": pa.table({"k": table.column("id")})},
        engine_version=ENGINE_VERSION,
        auto_chunk=False,
        key_provider=key_provider(),
        use_byte_estimate_routing=False,
        use_probe_routing=False,
    )
    return result.outputs[TABLE]


# ---------------------------------------------------------------------------
# 11. The unified binding admits it; the out-of-core route stays closed.
# ---------------------------------------------------------------------------


def test_the_unified_binding_binds_a_non_deterministic_faker_keyed_on_the_job_seed(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution.native._operator_params import FakerParams
    from decoy_engine.execution.physical._compiler import compile_physical_plan
    from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
    from tests.physical._shadow_helpers import build_config, write_read_only_fixture

    src = pa.table({"f": pa.array(["a", "b", "c"], pa.string())})
    write_read_only_fixture(tmp_path, src, "x")
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [nd_faker()])
    inputs = capture_physical_plan_inputs(config, {"t": src}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    nodes = [n for tbl in plan.tables for n in tbl.nodes if n.strategy == "faker"]
    assert len(nodes) == 1
    binding = nodes[0].execution
    assert binding is not None
    assert isinstance(binding.params, FakerParams) and binding.params.positional
    assert binding.key_binding is not None and binding.key_binding.key_source == "job_seed"
    assert binding.pool_binding is not None and binding.needs_index_kernel
    # The config-only eligibility report still describes the value-keyed operator.
    assert not native_route_eligibility(config, table="t").accepted


@NEEDS_COMPANION
def test_the_unified_shadow_operator_draws_a_positional_faker_by_job_seed_and_offset(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
    from decoy_engine.execution.physical._compiler import compile_physical_plan
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_operators import (
        OperatorCallEvidence,
        run_operator,
    )
    from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
    from tests.native._chunked_faker_support import expected_values, pool_of
    from tests.physical._shadow_helpers import build_config, write_read_only_fixture

    src = pa.table({"f": pa.array(["a", "b", "c"], pa.string())})
    write_read_only_fixture(tmp_path, src, "x")
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [nd_faker()])
    inputs = capture_physical_plan_inputs(config, {"t": src}, engine_version=ENGINE_VERSION)
    (node,) = [n for t in compile_physical_plan(inputs).tables for n in t.nodes]
    binding = node.execution
    assert binding is not None
    job_seed = inputs.plan.seed_envelope.job_seed
    # A mask key unlike the job seed: the draw must follow the job seed.
    ctx = ShadowContext(mask_key=b"\x09" * 32, job_seed=job_seed)
    evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
    values = ["x", None, "y", "z", "w", None, "v", "u"]
    out, _ = run_operator(
        pa.array(values, pa.string()),
        binding=binding,
        ctx=ctx,
        evidence=evidence,
        pool=pool_of(namespace=None, job_seed=job_seed),
        index_kernel=load_compiled_index_kernel(),
        row_offset=3,
    )
    want = expected_values(
        range(3, 3 + len(values)), config=config, namespace=None, job_seed=job_seed
    )
    assert out.to_pylist() == [None if v is None else w for v, w in zip(values, want, strict=True)]
    assert (evidence.compiled_kernel_executed, evidence.rows_seen) == (True, len(values))


def test_the_out_of_core_veto_is_unchanged() -> None:
    from decoy_engine.execution._runner import build_work_list, order_work
    from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
    from tests.unit.execution import test_faker_positional_nondet as base

    plan, graph = base._fk_plan(base._seed(namespace="ns", deterministic=False))
    work = order_work(build_work_list(plan, _REG), graph)
    compat = check_out_of_core_compatibility(plan, work, graph)
    assert not compat.accepted
    assert {r.code for r in compat.rejections} == {"out_of_core_faker_pool_unsupported"}
