"""GAP8: an integer FK KEY under `fpe` masks natively on the SEQ route, byte-identical to Python.

Every positive case builds the same job twice (SEQ under the real compiled FF1 kernel, and the
full-frame oracle) and compares them on the strict per-executor contract. A real-kernel spy
counts the calls per tweak column, so a native case is proven by calls > 0 and a declined case
by calls == 0 (the fpe disposition has no `native_calls` field). The consumed-output witness
swaps in a kernel that tampers with the real result and shows the tampered text reaches the
published tables, for the parent pass and the REMAP mint.

Needs the abi-3 companion for the native cases (skipped, not weakened, without it); the
decline cases run on either venv because a declined column never calls the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import DecoyError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._sequential_native import SeqNativeRun
from decoy_engine.execution._sequential_native_ops import (
    _fpe_admit,
    _fpe_key_admit,
    _fpe_remap_input_reason,
)
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    FpeConfig,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d2a_seq_data import RecordingSink
from tests.parity._d2c_fpe_data import fpe_seed, install_fpe

REMAP = OrphanPolicy.REMAP
_ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
INT64, UINT64 = pa.int64(), pa.uint64()
PARENT_KEYS = [1000000001, 1000000002, 1000000003, 1000000004]
TAMPER = "~"


def _companions_load() -> bool:
    try:
        load_compiled_crypto_kernel()
        load_compiled_fpe_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


needs_companion = pytest.mark.skipif(
    not _companions_load(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)


@dataclass
class SpyCall:
    tweak_column: str
    rows: int
    input_type: pa.DataType
    inputs: list[Any]
    mask_key: bytes


@dataclass
class SpyFpe:
    """The REAL compiled kernel behind a call counter.

    `mode`: "real" delegates; "tamper" appends TAMPER to every ciphertext (the witness that a
    native result is what gets published); "capture" returns the input untouched (it shows
    exactly what text the production path feeds the kernel)."""

    mode: str = "real"
    calls: list[SpyCall] = field(default_factory=list)

    def run(self, values: Any, **kwargs: Any) -> Any:
        self.calls.append(
            SpyCall(
                kwargs["tweak_column"],
                len(values),
                values.type,
                values.to_pylist(),
                kwargs["mask_key"],
            )
        )
        real = load_compiled_fpe_kernel()
        if self.mode == "capture":
            return type(real.run(pa.array(["1234567890"]), **kwargs))(
                values=values, errors=(), warnings=()
            )
        result = real.run(values, **kwargs)
        if self.mode == "tamper":
            marked = [None if v is None else v + TAMPER for v in result.values.to_pylist()]
            return type(result)(
                values=pa.array(marked, pa.string()), errors=result.errors, warnings=()
            )
        return result

    def count(self, column: str) -> int:
        return sum(1 for c in self.calls if c.tweak_column == column)


def int_shape(
    parent: list[Any],
    child: list[Any],
    *,
    ptype: pa.DataType = INT64,
    ctype: pa.DataType | None = None,
    pname: str = "pid",
    cname: str = "cid",
    pcfg: dict[str, Any] | None = None,
    ccfg: dict[str, Any] | None = None,
    policy: OrphanPolicy = REMAP,
    payload: pa.Array | None = None,
    payload_cfg: dict[str, Any] | None = None,
) -> s.Shape:
    pcols = {pname: fpe_seed("ns_p", **(pcfg or {}))}
    pdata = {pname: pa.array(parent, ptype)}
    if payload is not None:
        pcols["pay"] = fpe_seed("ns_pay", **(payload_cfg or {}))
        pdata["pay"] = payload
    plan = s.make_plan(
        {
            "P": s.table_seed(pcols),
            "C": s.table_seed({cname: fpe_seed("ns_c", **(ccfg or {}))}),
        }
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", pname, "C", cname, "ns_p", policy)),
        {"P": pa.table(pdata), "C": pa.table({cname: pa.array(child, ctype or ptype)})},
    )


def fpe_entries(result: Any, column: str) -> list[dict[str, Any]]:
    return [e for e in result.quality_metrics["sequential_fpe"] if e["column"] == column]


def assert_strict(oracle: dict[str, pa.Table], native: dict[str, pa.Table]) -> None:
    assert set(oracle) == set(native)
    for table in oracle:
        assert sc.compare_strict(oracle[table], native[table]) == _ALL_TRUE, table


def run_native(
    mp: pytest.MonkeyPatch, shape: s.Shape, mode: str = "real", sink: Any = None
) -> tuple[Any, SpyFpe]:
    spy = install_fpe(mp, SpyFpe(mode))
    return s.run_seq(shape, sink=sink), spy


def assert_native_matches_oracle(mp: pytest.MonkeyPatch, shape: s.Shape) -> tuple[Any, SpyFpe]:
    oracle = s.run_full(shape).outputs
    native, spy = run_native(mp, shape)
    assert_strict(oracle, native.outputs)
    return native, spy


def error_code(exc: DecoyError | ExecutionError) -> str:
    return str(exc.code)


# T1: FK-key byte parity, referential integrity and the consumed-output witness.


@needs_companion
@pytest.mark.parametrize(
    ("ptype", "parent", "child"),
    [
        (INT64, PARENT_KEYS, [1000000001, 1000000001, 1000000003, 5555555555, None]),
        (pa.int32(), [100000001, 100000002, 100000003], [100000001, 100000003, 555555555]),
        (pa.uint32(), [3000000001, 3000000002], [3000000001, 3000000009]),
        (UINT64, [2**63 + 5, 2**63 + 6, 2**64 - 1], [2**63 + 5, 2**64 - 1, 2**63 + 99]),
    ],
    ids=["int64", "int32", "uint32", "uint64-above-int64"],
)
def test_t1_an_integer_fk_key_is_byte_identical_and_really_native(
    ptype: pa.DataType, parent: list[int], child: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = int_shape(parent, child, ptype=ptype)
    native, spy = assert_native_matches_oracle(monkeypatch, shape)
    assert spy.count("pid") >= 2  # the parent pass and the REMAP mint both hit the kernel
    assert [e["executed_backend"] for e in fpe_entries(native, "pid")] == ["rust_companion"] * 2
    out_p = native.outputs["P"].column("pid").to_pylist()
    out_c = native.outputs["C"].column("cid").to_pylist()
    assert native.outputs["P"].schema.field("pid").type == pa.string()
    # RI, matched children only: a matched child holds its parent's masked key.
    for value, got in zip(child, out_c, strict=True):
        if value in parent:
            assert got == out_p[parent.index(value)]
    # A genuine orphan is minted, not a parent row: it is neither cleartext nor any parent key.
    orphans = [g for v, g in zip(child, out_c, strict=True) if v is not None and v not in parent]
    assert orphans
    for orphan in orphans:
        assert orphan not in out_p and orphan not in {str(v) for v in child if v is not None}


@needs_companion
def test_t1_the_published_tables_are_the_kernel_output_for_parent_and_remap_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = int_shape(PARENT_KEYS, [1000000001, 5555555555, 1000000002])
    oracle = s.run_full(shape).outputs
    tampered, spy = run_native(monkeypatch, shape, "tamper")
    p = tampered.outputs["P"].column("pid").to_pylist()
    c = tampered.outputs["C"].column("cid").to_pylist()
    assert all(v.endswith(TAMPER) for v in p)  # the parent decision was consumed
    assert c[1].endswith(TAMPER)  # the REMAP mint was consumed
    assert c[0] == p[0] and c[2] == p[1]  # matched children still inherit it
    # Negative control: the tampered run is NOT the oracle, so the equality in the other T1
    # cases cannot pass vacuously on a Python recompute.
    assert sc.compare_strict(oracle["P"], tampered.outputs["P"]) != _ALL_TRUE
    assert spy.calls


# T2: the int -> string boundary on the production path.


def _expected_text(values: list[Any]) -> list[str | None]:
    return [None if v is None else str(v) for v in values]


@pytest.mark.parametrize(
    ("ptype", "values"),
    [
        (pa.int8(), [0, 1, -1, 127, -128, None]),
        (pa.int16(), [0, 32767, -32768, 12345, None]),
        (pa.int32(), [0, 2**31 - 1, -(2**31), 123456789, None]),
        (INT64, [0, 2**63 - 1, -(2**63), -1, 1000000007, None]),
        (pa.uint8(), [0, 255, 7, None]),
        (pa.uint16(), [0, 65535, 4242, None]),
        (pa.uint32(), [0, 2**32 - 1, 4000000000, None]),
        (UINT64, [0, 2**64 - 1, 2**63, 9999999999999999999, None]),
    ],
    ids=["int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"],
)
def test_t2_the_cast_in_the_production_path_feeds_the_kernel_str_of_each_value(
    ptype: pa.DataType, values: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = int_shape(values, [], ptype=ptype)
    _, spy = run_native(monkeypatch, shape, "capture")
    (call,) = [c for c in spy.calls if c.tweak_column == "pid"]
    assert call.input_type == pa.string()
    assert call.inputs == _expected_text(values)


@needs_companion
def test_t2_a_random_integer_sweep_matches_the_oracle_through_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import random

    rng = random.Random(20261014)
    parent = sorted({rng.randrange(10**6, 10**18) for _ in range(400)})
    child = [rng.choice(parent) for _ in range(300)] + [rng.randrange(10**6, 10**18)]
    native, spy = assert_native_matches_oracle(monkeypatch, int_shape(parent, child))
    assert spy.count("pid") >= 2 and native.outputs["P"].num_rows == len(parent)


# T3: the FF1 domain floor, 5 digits fail closed and 6 digits mask and round-trip.


@needs_companion
def test_t3_a_five_digit_key_fails_closed_like_the_oracle_and_commits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = int_shape([10001, 10002, 10003], [10001])
    with pytest.raises(DecoyError) as oracle:
        s.run_full(shape)
    sink = RecordingSink()
    spy = install_fpe(monkeypatch, SpyFpe())
    with pytest.raises(DecoyError) as native:
        s.run_seq(shape, sink=sink)
    assert error_code(native.value) == error_code(oracle.value) == "fpe_unencryptable_domain"
    assert sink.commits == 0 and sink.committed == {}
    assert spy.count("pid") == 1  # it failed in the native pass, not in a Python fallback


@needs_companion
def test_t3_a_six_digit_key_masks_and_decrypts_back_to_the_integer_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [100001, 100002, 999999]
    native, spy = assert_native_matches_oracle(monkeypatch, int_shape(keys, [100001]))
    assert spy.count("pid") >= 1
    ciphertext = native.outputs["P"].column("pid")
    real = load_compiled_fpe_kernel()
    plain = real.run(
        pa.array(ciphertext.to_pylist(), pa.string()),
        mask_key=spy.calls[0].mask_key,
        namespace="ns_p",
        tweak_column="pid",
        config=FpeConfig(),
        forward=False,
    )
    assert plain.values.to_pylist() == [str(k) for k in keys]


# T4: negatives, out-of-charset and luhn fail closed (or succeed) exactly like the oracle.


def _compare_run(
    monkeypatch: pytest.MonkeyPatch, shape: s.Shape
) -> tuple[Any, SpyFpe] | tuple[str, SpyFpe]:
    """Native output vs oracle on success, or the shared error code on failure."""
    try:
        oracle = s.run_full(shape).outputs
    except DecoyError as exc:
        spy = install_fpe(monkeypatch, SpyFpe())
        sink = RecordingSink()
        with pytest.raises(DecoyError) as native:
            s.run_seq(shape, sink=sink)
        assert error_code(native.value) == error_code(exc)
        assert sink.commits == 0 and sink.committed == {}
        return error_code(exc), spy
    native, spy = run_native(monkeypatch, shape)
    assert_strict(oracle, native.outputs)
    return native, spy


@needs_companion
def test_t4_a_long_negative_key_masks_with_separators_and_fails_closed_without(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = [-1000000001, -1000000002]
    kept, spy_ok = _compare_run(monkeypatch, int_shape(keys, [keys[0]]))
    assert not isinstance(kept, str) and spy_ok.count("pid") >= 1
    off = {"preserve_separators": False}
    code, spy_bad = _compare_run(monkeypatch, int_shape(keys, [keys[0]], pcfg=off, ccfg=off))
    assert code == "fpe_unencryptable_value" and spy_bad.count("pid") == 1


@needs_companion
def test_t4_a_key_wholly_outside_the_charset_fails_even_with_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    letters = {"charset": "alpha"}
    code, spy = _compare_run(
        monkeypatch, int_shape([1000000001, 1000000002], [1000000001], pcfg=letters, ccfg=letters)
    )
    assert code == "fpe_unencryptable_value" and spy.count("pid") == 1


@needs_companion
def test_t4_luhn_validation_matches_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    luhn = {"validate_luhn": True}
    keys = [4539578763621486, 4539578763621494]  # both luhn-valid
    native, spy = _compare_run(monkeypatch, int_shape(keys, [keys[0]], pcfg=luhn, ccfg=luhn))
    assert not isinstance(native, str) and spy.count("pid") >= 1
    # A luhn-invalid body is not an error under validate_luhn; the check digit is recomputed.
    bad, spy_bad = _compare_run(
        monkeypatch, int_shape([4539578763621487, 4539578763621494], [], pcfg=luhn, ccfg=luhn)
    )
    assert not isinstance(bad, str) and spy_bad.count("pid") == 1


# T5: REMAP orphans of integer type.


@needs_companion
@pytest.mark.parametrize("orphan", [12345, -5555555555])
def test_t5_an_invalid_integer_orphan_fails_closed_after_the_parent_was_staged(
    orphan: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = {"preserve_separators": False}
    shape = int_shape(PARENT_KEYS, [1000000001, orphan], pcfg=cfg, ccfg=cfg)
    code, spy = _compare_run(monkeypatch, shape)
    assert isinstance(code, str)
    assert code == ("fpe_unencryptable_domain" if orphan == 12345 else "fpe_unencryptable_value")
    assert spy.count("pid") == 2  # parent pass ok, then the REMAP mint raised


@needs_companion
def test_t5_a_mixed_orphan_batch_reports_the_first_failure_like_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"preserve_separators": False}
    first_short = int_shape(PARENT_KEYS, [12345, -5555555555, 5555555555], pcfg=cfg, ccfg=cfg)
    first_neg = int_shape(PARENT_KEYS, [-5555555555, 12345, 5555555555], pcfg=cfg, ccfg=cfg)
    a, _ = _compare_run(monkeypatch, first_short)
    b, _ = _compare_run(monkeypatch, first_neg)
    assert (a, b) == ("fpe_unencryptable_domain", "fpe_unencryptable_value")  # order is real


@needs_companion
def test_t5_names_inherited_parents_nulls_duplicates_and_unsigned_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Different parent/child names, nulls and duplicate child keys, an unsigned orphan above
    # 2**63, and an orphan wider than the int32 parent's own width (typed by the child column).
    shape = int_shape(
        [100000001, 100000002, 100000003],
        [100000001, 100000001, None, 9999999999, 9999999999],
        ptype=pa.int32(),
        ctype=INT64,
        pname="parent_id",
        cname="child_fk",
    )
    native, spy = assert_native_matches_oracle(monkeypatch, shape)
    assert spy.count("parent_id") >= 2
    big = int_shape(
        [2**63 + 1, 2**63 + 2], [2**63 + 1, 2**64 - 5, 2**64 - 5], ptype=UINT64, pname="k", cname="k2"
    )
    assert_native_matches_oracle(monkeypatch, big)


def _chain_shape(ptype: pa.DataType = INT64) -> s.Shape:
    plan = s.make_plan(
        {
            "A": s.table_seed({"k": fpe_seed("ns_ab")}),
            "B": s.table_seed({"k": fpe_seed("ns_bc")}),
            "C": s.table_seed({"k": fpe_seed("ns_bc")}),
        }
    )
    a = [1000000001, 1000000002]
    b = [*a, 2000000002]
    c = [*b, 3000000003]
    return s.Shape(
        plan,
        s.graph(
            s.edge("A", "k", "B", "k", "ns_ab", REMAP),
            s.edge("B", "k", "C", "k", "ns_bc", REMAP),
        ),
        {n: pa.table({"k": pa.array(v, ptype)}) for n, v in (("A", a), ("B", b), ("C", c))},
    )


@needs_companion
def test_t5_an_inherited_intermediate_parent_chain_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, spy = assert_native_matches_oracle(monkeypatch, _chain_shape())
    assert spy.count("k") >= 3
    middle = [e for e in fpe_entries(native, "k") if e["table"] == "B"]
    assert {e["executed_backend"] for e in middle} <= {"rust_companion", "inherited"}


# T6: empty versus all-null.


@needs_companion
def test_t6_an_empty_integer_key_is_no_work_and_matches_the_oracle_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, spy = assert_native_matches_oracle(monkeypatch, int_shape([], []))
    assert spy.calls == []  # zero rows never reach the kernel
    assert native.outputs["P"].schema.field("pid").type == pa.float64()


@needs_companion
def test_t6_an_all_null_integer_key_calls_the_kernel_and_matches_the_oracle_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, spy = assert_native_matches_oracle(monkeypatch, int_shape([None, None], [None]))
    assert spy.count("pid") == 1  # an all-null column is still a native call
    assert native.outputs["P"].schema.field("pid").type == pa.null()


# T7: everything else stays on Python, and a payload shares a table with a native key.


def _declined_entry(result: Any, column: str) -> dict[str, Any]:
    (entry,) = [e for e in fpe_entries(result, column) if e["path"] == "scalar"]
    return entry


@needs_companion
def test_t7_a_nullable_integer_payload_beside_an_integer_key_stays_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = pa.array([123456, None, 9007199254740993, 1000000007], INT64)
    shape = int_shape(PARENT_KEYS, [1000000001], payload=payload)
    native, spy = assert_native_matches_oracle(monkeypatch, shape)
    assert spy.count("pid") >= 1  # the key went native
    assert spy.count("pay") == 0  # the payload never reached the kernel
    pay = _declined_entry(native, "pay")
    assert pay["executed_backend"] == "python_scalar"
    assert pay["reason"] == "fpe_source_type_not_string:pay:int64"
    assert _declined_entry(native, "pid")["executed_backend"] == "rust_companion"


def _typed_key_shape(array: pa.Array, child: pa.Array) -> s.Shape:
    plan = s.make_plan(
        {"P": s.table_seed({"pid": fpe_seed("ns_p")}), "C": s.table_seed({"cid": fpe_seed("ns_c")})}
    )
    return s.Shape(
        plan,
        s.graph(s.edge("P", "pid", "C", "cid", "ns_p", REMAP)),
        {"P": pa.table({"pid": array}), "C": pa.table({"cid": child})},
    )


@pytest.mark.parametrize(
    ("array", "type_text"),
    [
        (pa.array(["1000000001", "1000000002"], pa.large_string()), "large_string"),
        (pa.array([1000000001.0, 1000000002.0], pa.float64()), "double"),
        (pa.array([1000000001, 1000000002], pa.decimal128(12, 0)), "decimal128(12, 0)"),
    ],
    ids=["large_string", "float", "decimal"],
)
def test_t7_other_key_types_decline_with_the_string_only_reason(
    array: pa.Array, type_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = _typed_key_shape(array, array.slice(0, 1))
    oracle = s.run_full(shape).outputs
    native, spy = run_native(monkeypatch, shape)
    assert spy.calls == []
    assert_strict(oracle, native.outputs)
    entry = _declined_entry(native, "pid")
    assert entry["executed_backend"] == "python_scalar"
    assert entry["reason"] == f"fpe_source_type_not_string:pid:{type_text}"


def test_t7_a_checksum_integer_key_declines_on_the_checksum_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"checksum": "luhn"}
    valid = [4539578763621486, 4539578763621494]  # luhn-valid, so the Python handler can run
    shape = int_shape(valid, [valid[0]], pcfg=cfg, ccfg=cfg)
    native, spy = run_native(monkeypatch, shape)
    assert spy.calls == []
    assert _declined_entry(native, "pid")["reason"] == "fpe_checksum_not_native:pid"


def test_t7_a_missing_namespace_integer_key_raises_like_the_oracle_and_never_calls_the_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = int_shape(PARENT_KEYS, [1000000001])
    seeds = dict(shape.plan.seed_envelope.per_table)
    from dataclasses import replace

    bare = replace(fpe_seed(None))
    broken = s.make_plan({"P": s.table_seed({"pid": bare}), "C": seeds["C"]})
    shape = s.Shape(broken, shape.graph, shape.sources)
    with pytest.raises(DecoyError) as oracle:
        s.run_full(shape)
    spy = install_fpe(monkeypatch, SpyFpe())
    with pytest.raises(DecoyError) as native:
        s.run_seq(shape)
    assert error_code(native.value) == error_code(oracle.value)
    assert spy.calls == []


# T8: the admission order, checksum -> namespace -> config -> source type.


@pytest.mark.parametrize("admit", [_fpe_admit, _fpe_key_admit], ids=["payload", "key"])
def test_t8_the_admit_order_is_checksum_namespace_config_then_type(admit: Any) -> None:
    bad_type = pa.float64()
    both = fpe_seed(None, checksum="luhn", charset="aab")
    assert admit("c", bad_type, both) == "fpe_checksum_not_native:c"
    no_ns = fpe_seed(None, charset="aab")
    assert admit("c", bad_type, no_ns) == "fpe_requires_namespace_stays_python:c"
    bad_cfg = fpe_seed("ns", charset="aab")
    assert admit("c", bad_type, bad_cfg) == "fpe_config_stays_python:c"
    assert admit("c", bad_type, fpe_seed("ns")) == "fpe_source_type_not_string:c:double"
    assert admit("c", pa.string(), fpe_seed("ns")) is None


def test_t8_only_the_key_admit_takes_an_integer() -> None:
    seed = fpe_seed("ns")
    for t in (pa.int8(), pa.int64(), pa.uint16(), pa.uint64()):
        assert _fpe_key_admit("c", t, seed) is None
        assert _fpe_admit("c", t, seed) == f"fpe_source_type_not_string:c:{t}"
        assert _fpe_remap_input_reason("c", t) is None
    for t in (pa.large_string(), pa.float32(), pa.decimal128(10, 0), pa.bool_()):
        assert _fpe_key_admit("c", t, seed) == f"fpe_source_type_not_string:c:{t}"
        assert _fpe_remap_input_reason("c", t) == f"fpe_source_type_not_string:c:{t}"


# The integer-preservation guard runs before the coercive pin, through the production seam.


def test_t8_a_float_widened_key_aborts_as_an_invariant_not_a_silent_coercion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = SeqNativeRun.native_column

    def widened(self: SeqNativeRun, ctx: Any, df: Any, column: str, strategy: str, call: Any) -> Any:
        df = df.copy()
        df[column] = df[column].astype("float64")  # what to_pandas_fk_safe exists to prevent
        return real(self, ctx, df, column, strategy, call)

    monkeypatch.setattr(SeqNativeRun, "native_column", widened)
    spy = install_fpe(monkeypatch, SpyFpe("capture"))
    with pytest.raises(ExecutionError) as err:
        s.run_seq(int_shape(PARENT_KEYS, [1000000001]))
    assert err.value.code == "sequential_fpe_key_integer_not_preserved"
    assert spy.calls == []  # nothing was stringified: the abort comes before the pin


# T9: a mixed job keeps each column's own disposition.


@needs_companion
def test_t9_integer_string_hash_and_declined_float_keys_do_not_cross_contaminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tables = {
        "I": pa.table({"ik": pa.array([1000000001, 1000000002], INT64)}),
        "IC": pa.table({"ik2": pa.array([1000000001, 1000000009], INT64)}),
        "S": pa.table({"sk": s.strs(["2000000001", "2000000002"])}),
        "SC": pa.table({"sk2": s.strs(["2000000001", "2000000009"])}),
        "H": pa.table({"hk": s.strs(["h1", "h2"])}),
        "HC": pa.table({"hk2": s.strs(["h1", "h9"])}),
        "F": pa.table({"fk": pa.array([3000000001.0, 3000000002.0], pa.float64())}),
        "FC": pa.table({"fk2": pa.array([3000000001.0, 3000000009.0], pa.float64())}),
    }
    plan = s.make_plan(
        {
            "I": s.table_seed({"ik": fpe_seed("ns_i")}),
            "IC": s.table_seed({"ik2": fpe_seed("ns_i")}),
            "S": s.table_seed({"sk": fpe_seed("ns_s")}),
            "SC": s.table_seed({"sk2": fpe_seed("ns_s")}),
            "H": s.table_seed({"hk": s.hashed("ns_h")}),
            "HC": s.table_seed({"hk2": s.hashed("ns_h")}),
            "F": s.table_seed({"fk": fpe_seed("ns_f")}),
            "FC": s.table_seed({"fk2": fpe_seed("ns_f")}),
        }
    )
    shape = s.Shape(
        plan,
        s.graph(
            s.edge("I", "ik", "IC", "ik2", "ns_i", REMAP),
            s.edge("S", "sk", "SC", "sk2", "ns_s", REMAP),
            s.edge("H", "hk", "HC", "hk2", "ns_h", REMAP),
            s.edge("F", "fk", "FC", "fk2", "ns_f", REMAP),
        ),
        tables,
    )
    native, spy = assert_native_matches_oracle(monkeypatch, shape)
    assert spy.count("ik") >= 2 and spy.count("sk") >= 2
    assert spy.count("fk") == 0  # the declined float key never reached the kernel
    by_col = {c: _declined_entry(native, c) for c in ("ik", "sk", "fk")}
    assert by_col["ik"]["executed_backend"] == "rust_companion"
    assert by_col["sk"]["executed_backend"] == "rust_companion"
    assert by_col["fk"]["executed_backend"] == "python_scalar"
    assert by_col["fk"]["reason"] == "fpe_source_type_not_string:fk:double"
    hash_entries = native.quality_metrics["sequential_hash"]
    assert {e["executed_backend"] for e in hash_entries if e["path"] != "fk_inherited"} == {
        "rust_companion"
    }
