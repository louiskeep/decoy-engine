"""C6b-i acceptance: text_mask as an ARROW_PYTHON operator on the chunked native route.

Native chunked output must equal the shipped `TextMaskHandler`'s chunked output (values, Arrow
type, metadata, warnings, per chunk). Every admitted case proves the table really took the native
`arrow_python` backend, so a silent fallback to the oracle cannot pass the parity check. text_mask
is KEYED and handler-rich, so the sub-floor warning and fail-closed paths are exercised too.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native._chunked_entry import aggregate_chunked_route_evidence
from decoy_engine.execution.native._chunked_evidence import plan_column_backends
from decoy_engine.execution.native._chunked_schema_rule import build_schema_rule
from decoy_engine.execution.native._real_type_admission import string_source_type_rejection
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.providers_v2 import get_default_registry
from tests.native._b8_support import Run, assert_same_as_oracle, run_one, run_pair
from tests.native._c6b_i_support import (
    ADMITTED_CONFIGS,
    CORPUS,
    EXCLUDED_CONFIGS,
    SHAPES,
    SUB_FLOOR_TEXTS,
    tm_col,
)
from tests.native._chunked_date_shift_support import run_outcome
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    faker_col,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    split,
)


def source(values: list[str | None], *, typ: pa.DataType | None = None) -> pa.Table:
    return pa.table(
        {
            "s": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def _columns(run: Run) -> dict[str, dict[str, Any]]:
    return {c["column"]: c for c in aggregate_chunked_route_evidence(run.sink)["columns"]}


def _assert_native_arrow_python(run: Run, column: str = "s") -> None:
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    assert evidence.reroute_reason is None
    assert {n.column: n.route for n in evidence.node_routes}[column] == "native_kernel"
    col = _columns(run)[column]
    assert col["planned_backend"] == "arrow_python"
    assert col["executed_backend"] == "arrow_python"
    assert evidence.compiled_kernel_executed is False


# ---------------------------------------------------------------------------
# 1. Parity matrix against the oracle (value + Arrow type + warning multiset).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 7, 50_000])
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_native_chunked_equals_oracle_chunked(config: str, shape: str, size: int) -> None:
    chunks = split(source(SHAPES[shape]), size) or [source([])]
    native, forced = run_pair([tm_col(**ADMITTED_CONFIGS[config]), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)
    # Explicitly string-typed on every chunk, whatever the chunk holds.
    assert all(chunk.schema.field("s").type == pa.string() for chunk in native.out)


def test_empty_all_null_and_valued_chunks_interleaved_are_identical() -> None:
    chunks = [
        source(["ssn 123-45-6789", "a@b.com"]),
        source([]),
        source([None, None, None]),
        source(["", "no pii"]),
        source([]),
    ]
    native, forced = run_pair([tm_col(), passthrough("p")], chunks)
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)
    assert {chunk.schema.field("s").type for chunk in native.out} == {pa.string()}


def test_a_multi_chunk_run_over_many_rows_is_identical_on_both_legs() -> None:
    values = [None if i % 11 == 3 else CORPUS[i % len(CORPUS)] for i in range(20_007)]
    native, forced = run_pair(
        [tm_col(per_detector_strategy={"ssn": "faker"}), passthrough("p")],
        split(source(values), 5_000),
    )
    assert len(native.out) == 5
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native)


def test_two_text_mask_columns_with_different_configs_each_match_the_oracle() -> None:
    table = pa.table(
        {
            "a": pa.array(CORPUS, pa.string()),
            "b": pa.array(CORPUS, pa.string()),
            "p": pa.array(range(len(CORPUS)), pa.int64()),
        }
    )
    columns = [
        tm_col("a", detectors=["ssn"], per_detector_strategy={"ssn": "faker"}),
        tm_col("b", token="<X>"),
        passthrough("p"),
    ]
    native, forced = run_pair(columns, split(table, 9))
    assert_same_as_oracle(native, forced)
    _assert_native_arrow_python(native, "a")
    _assert_native_arrow_python(native, "b")


def test_faker_override_maps_repeated_values_consistently_across_chunks() -> None:
    # The disputed branch: a built-in detector OVERRIDDEN to faker. The same SSN in different chunks
    # synthesizes to the same value (keyed on the matched value, not position/chunk), so two
    # identical cells one row apart mask identically even when they land in different chunks.
    repeated = ["ssn 123-45-6789", "ssn 987-65-4321", "ssn 123-45-6789", "ssn 987-65-4321"]
    native, forced = run_pair(
        [tm_col(per_detector_strategy={"ssn": "faker"}), passthrough("p")],
        split(source(repeated), 1),  # one row per chunk, so the repeats span chunk boundaries
    )
    assert_same_as_oracle(native, forced)
    masked = [v for c in native.out for v in c.column("s").to_pylist()]
    assert masked[0] == masked[2]  # same SSN -> same synthetic value across chunks
    assert masked[1] == masked[3]
    assert masked[0] != masked[1]  # different SSNs -> different synthetic values


# ---------------------------------------------------------------------------
# 2. Table-level lift: one text_mask column no longer vetoes its neighbours.
# ---------------------------------------------------------------------------


def test_text_mask_beside_redact_keeps_the_table_native() -> None:
    table = pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(9)], pa.string()),
            "s": pa.array(CORPUS[:9], pa.string()),
            "p": pa.array(range(9), pa.int64()),
        }
    )
    run = run_one(make_config([redact("r"), tm_col(), passthrough("p")]), split(table, 4))
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    assert {n.column: n.route for n in evidence.node_routes} == {
        "r": "native_kernel",
        "s": "native_kernel",
        "p": "native_kernel",
    }
    assert {c: v["executed_backend"] for c, v in _columns(run).items()}["s"] == "arrow_python"


@NEEDS_COMPANION
def test_text_mask_beside_hash_and_faker_keeps_each_on_its_own_backend() -> None:
    n = 12
    table = pa.table(
        {
            "h": pa.array([f"user{i % 4}@x.com" for i in range(n)], pa.string()),
            "f": pa.array([f"first{i % 5}" for i in range(n)], pa.string()),
            "s": pa.array([CORPUS[i] for i in range(n)], pa.string()),
        }
    )
    run = run_one(make_config([hash_col("h"), faker_col("f"), tm_col()]), split(table, 5))
    evidence = run.ev[0]
    assert evidence.native_admitted is True, evidence.reroute_reason
    backends = {c: v["executed_backend"] for c, v in _columns(run).items()}
    assert backends == {"h": "rust_companion", "f": "rust_pool_select", "s": "arrow_python"}
    assert evidence.compiled_kernel_executed is True


def test_the_evidence_planner_plans_arrow_python_for_text_mask() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = make_config([tm_col(), passthrough("p")])
    profile = first_chunk_profile(source(CORPUS), table=TABLE, engine_version=ENGINE_VERSION)
    plans = plan_column_backends(
        config, profile, table=TABLE, engine_version=ENGINE_VERSION, registry=get_default_registry()
    )
    assert {p.column: p.planned_backend for p in plans}["s"] == "arrow_python"


# ---------------------------------------------------------------------------
# 3. Warnings parity: the sub-floor warning rides each chunk, native == oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["redact", "synthetic"])
@pytest.mark.parametrize("size", [2, 50_000])
def test_sub_floor_warning_parity_per_chunk(policy: str, size: int) -> None:
    native, forced = run_pair(
        [tm_col(sub_floor_span=policy, unmatched_span_policy="passthrough"), passthrough("p")],
        split(source(SUB_FLOOR_TEXTS), size),
    )
    assert_same_as_oracle(native, forced)  # compares each chunk's warnings
    emitted = [
        w for r in native.sink for w in r.warnings if w.code == "text_mask_sub_floor_span_handled"
    ]
    assert emitted  # the native leg actually produced the warning, not just matched an empty set
    assert all(w.detail["policy"] == policy for w in emitted)


# ---------------------------------------------------------------------------
# 4. Failure parity: a fail-closed span raises the same StrategyError on both chunked legs.
# ---------------------------------------------------------------------------


def _native_raises(config: dict[str, Any], chunks: list[pa.Table]) -> StrategyError:
    with pytest.raises(StrategyError) as exc:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    return exc.value


def _oracle_raises(config: dict[str, Any], chunks: list[pa.Table]) -> StrategyError:
    with pytest.raises(StrategyError) as exc:
        list(
            run_mask_pipeline_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    return exc.value


def test_fail_closed_code_matches_oracle() -> None:
    # us_zip 12345 is below the FF1 floor; the DEFAULT config (us_zip -> fpe, no sub_floor_span)
    # cannot encrypt it and no policy is set, so both chunked legs raise the canonical error.
    config = make_config([tm_col(), passthrough("p")])
    chunks = split(source(["home zip 12345 here", "123-45-6789"]), 1)
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert type(native_exc) is type(oracle_exc) is StrategyError
    assert native_exc.code == oracle_exc.code == "fpe_unencryptable_domain"
    assert native_exc.strategy == oracle_exc.strategy == "text_mask"


def test_fail_closed_first_failure_in_later_chunk() -> None:
    config = make_config([tm_col(), passthrough("p")])
    chunks = [source(["123-45-6789"]), source(["zip 12345 end"])]
    assert _native_raises(config, chunks).code == _oracle_raises(config, chunks).code


# ---------------------------------------------------------------------------
# 5. Source admission split by entrypoint.
# ---------------------------------------------------------------------------


def test_the_real_type_gate_names_text_mask_and_every_non_string_type() -> None:
    schema = pa.schema([("a", pa.string()), ("b", pa.large_string()), ("c", pa.int64())])
    assert string_source_type_rejection("text_mask", "a", schema) is None
    assert string_source_type_rejection("text_mask", "b", schema) == (
        "text_mask_source_type_not_string:b:large_string"
    )
    assert string_source_type_rejection("text_mask", "c", schema) == (
        "text_mask_source_type_not_string:c:int64"
    )


def test_a_numeric_source_raises_the_existing_coded_rejection() -> None:
    # The existing chunked guard is preserved: a non-string (numeric) source hard-raises rather
    # than silently diverging by chunk boundary.
    config = make_config([tm_col(), passthrough("p")])
    chunks = split(source([1, 22, 333, None, 5], typ=pa.int64()), 2)
    with pytest.raises(PlanCompileError) as exc:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert exc.value.code == "chunked_text_mask_source_dtype_unsupported"


def test_a_large_string_source_declines_native_and_matches_the_oracle() -> None:
    chunks = split(source(CORPUS[:9], typ=pa.large_string()), 4)
    native, forced = run_pair([tm_col(), passthrough("p")], chunks)
    assert native.ev[0].native_admitted is False
    assert "text_mask_source_type_not_string:s:large_string" in (native.ev[0].reroute_reason or "")
    assert_same_as_oracle(native, forced, expect_native=False)


def test_a_later_null_typed_chunk_is_rejected_schema_drift() -> None:
    # A string first chunk admits native; a later null-typed chunk is rejected, not cast-and-masked,
    # matching the oracle leg (C6b-i 3d). Both legs raise the same coded error.
    chunks = [source(["ssn 123-45-6789"]), source([None, None], typ=pa.null())]
    config = make_config([tm_col(), passthrough("p")])
    with pytest.raises(PlanCompileError) as native_exc:
        list(
            run_mask_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    with pytest.raises(PlanCompileError) as oracle_exc:
        list(
            run_mask_pipeline_chunked(
                config,
                chunks,
                table=TABLE,
                engine_version=ENGINE_VERSION,
                key_provider=key_provider(),
            )
        )
    assert (
        native_exc.value.code
        == oracle_exc.value.code
        == "chunked_text_mask_source_dtype_unsupported"
    )


# ---------------------------------------------------------------------------
# 6. Chunked string-pin, observable through the schema rule.
# ---------------------------------------------------------------------------


def _rule(column: dict[str, Any], first: pa.Table) -> Any:
    config = make_config([column, passthrough("p")])
    return build_schema_rule(config, table=TABLE, first=first, registry=get_default_registry())


def test_an_admitted_text_mask_config_is_string_pinned() -> None:
    assert "s" in _rule(tm_col(), source(CORPUS)).string_columns
    assert "s" in _rule(tm_col(per_detector_strategy={"ssn": "faker"}), source([])).string_columns


@pytest.mark.parametrize("name", sorted(EXCLUDED_CONFIGS))
def test_a_rejected_ner_config_is_not_string_pinned(name: str) -> None:
    cfg, _code = EXCLUDED_CONFIGS[name]
    assert "s" not in _rule(tm_col(**cfg), source(CORPUS)).string_columns


def test_a_string_source_all_null_column_is_a_string_on_the_chunked_route() -> None:
    run = run_one(make_config([tm_col(), passthrough("p")]), [source([None, None, None])])
    assert run.out[0].schema.field("s").type == pa.string()


# ---------------------------------------------------------------------------
# 8. Determinism: text_mask is KEYED, so the output DEPENDS on the mask key.
# ---------------------------------------------------------------------------


def _text_values(config: dict[str, Any], *, secret: bytes | None = None) -> list[Any]:
    outcome = run_outcome(config, split(source(CORPUS), 6), secret=secret)
    assert outcome.error is None, outcome.error
    return [v for t in outcome.out for v in t.column("s").to_pylist()]


def test_output_is_reproducible_and_keyed() -> None:
    columns = [tm_col(per_detector_strategy={"ssn": "faker"}), passthrough("p")]
    base = _text_values(make_config(columns), secret=bytes(range(32)))
    assert base == _text_values(make_config(columns), secret=bytes(range(32)))  # same key, same out
    # text_mask is keyed: a different secret changes the fpe/faker output (unlike text_redact).
    assert base != _text_values(make_config(columns), secret=bytes(range(1, 33)))


# ---------------------------------------------------------------------------
# A 1M-row routing record: the table-stays-native win at scale.
# ---------------------------------------------------------------------------


def test_one_million_row_table_stays_native_arrow_python() -> None:
    n = 1_000_000
    values = [CORPUS[i % len(CORPUS)] for i in range(n)]
    run = run_one(make_config([tm_col(), passthrough("p")]), split(source(values), 200_000))
    _assert_native_arrow_python(run)


# ---------------------------------------------------------------------------
# 9. Chunked work-order parity (C6b-i remediation, M1/M2). The chunked native route
#    used to raise fail-closed errors and assemble warnings in source-SCHEMA order; the
#    oracle uses canonical WORK order (`_runner.order_work`, sorted column name for a
#    native-admitted table). These regressions pin both legs to the same order so the
#    FIRST reported error and the warning sequence match the oracle exactly.
# ---------------------------------------------------------------------------


def _fpe_col(name: str, **pc: Any) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "fpe",
        "namespace": f"ns.{name}",
        "provider_config": {"charset": "digits", **pc},
    }


def _two_col(a: list[str | None], z: list[str | None]) -> pa.Table:
    # Schema order z,a (reversed vs work order a,z): the pre-fix schema-order iteration would
    # visit z first, the oracle visits a first, so the column a fix is observable.
    return pa.table({"z": pa.array(z, pa.string()), "a": pa.array(a, pa.string())})


def test_reversed_columns_two_text_mask_fail_closed_raise_identical_full_error() -> None:
    # z,a both default text_mask, both carry a sub-floor us_zip that fails closed with no policy.
    # The native leg used to iterate source-schema order (z first) and name column z; the oracle
    # names column a (work order). The fix iterates work order on both legs, so the FULL
    # StrategyError matches: type, code, strategy AND message (column name included).
    config = make_config([tm_col("z"), tm_col("a")])
    chunks = [_two_col(["home zip 12345 here"], ["home zip 12345 here"])]
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert type(native_exc) is type(oracle_exc) is StrategyError
    assert native_exc.code == oracle_exc.code == "fpe_unencryptable_domain"
    assert native_exc.strategy == oracle_exc.strategy == "text_mask"
    assert native_exc.message == oracle_exc.message
    assert "column 'a'" in native_exc.message  # the oracle's first-in-work-order column


@NEEDS_COMPANION
def test_reversed_columns_two_fpe_fail_closed_raise_identical_full_error() -> None:
    # The same reversed-column case for two fpe columns, proving the fix is general (work-order
    # iteration), not a text_mask special case. "123" is below the FF1 domain floor on both.
    config = make_config([_fpe_col("z"), _fpe_col("a")])
    chunks = [_two_col(["123"], ["123"])]
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert type(native_exc) is type(oracle_exc) is StrategyError
    assert native_exc.code == oracle_exc.code == "fpe_unencryptable_domain"
    assert native_exc.strategy == oracle_exc.strategy == "fpe"
    # The ordering property this fix governs: both legs name the SAME first-in-work-order column
    # (a), not z. The fpe route's fail-closed message WORDING differs from the oracle's by a
    # pre-existing gap outside this chunked-ordering remediation, so this pins the column, not the
    # full message text (unlike the text_mask case, which shares one error-builder).
    assert "column 'a'" in native_exc.message
    assert "column 'a'" in oracle_exc.message


@NEEDS_COMPANION
def test_mixed_text_mask_and_fpe_warnings_are_in_work_order() -> None:
    # a=text_mask (sub-floor redact -> one sub-floor warning) and z=fpe (out-of-charset prefix kept
    # -> one residual warning). The oracle emits the warnings in work order a,z; the native leg
    # used to group them (fpe then text_mask), reversing the pair. assert_same_as_oracle compares
    # each chunk's warning tuple in order.
    columns = [
        tm_col("a", sub_floor_span="redact", unmatched_span_policy="passthrough"),
        _fpe_col("z", preserve_separators=True),
    ]
    chunks = [
        pa.table(
            {
                "a": pa.array(["home zip 12345 here"], pa.string()),
                "z": pa.array(["M000001"], pa.string()),
            }
        )
    ]
    native, forced = run_pair(columns, chunks)
    assert_same_as_oracle(native, forced)
    codes = [w.code for r in native.sink for w in r.warnings]
    assert codes == ["text_mask_sub_floor_span_handled", "fpe_partial_plaintext_disclosure"]


def test_text_mask_operator_warning_precedes_projection_warning() -> None:
    # a=text_mask (sub-floor redact -> operator warning) beside u, an unconfigured column carried
    # under the warn policy (-> one projection warning). The oracle emits the operator warning
    # FIRST and the projection warning LAST; the native leg used to emit projection first.
    columns = [tm_col("a", sub_floor_span="redact", unmatched_span_policy="passthrough")]
    chunks = [
        pa.table(
            {
                "a": pa.array(["home zip 12345 here", "zip 67890 too"], pa.string()),
                "u": pa.array(["carry0", "carry1"], pa.string()),
            }
        )
    ]
    native, forced = run_pair(columns, chunks)
    assert_same_as_oracle(native, forced)
    codes = [w.code for r in native.sink for w in r.warnings]
    assert codes == ["text_mask_sub_floor_span_handled", "undeclared_output_columns"]
