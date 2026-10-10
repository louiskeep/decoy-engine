"""Acceptance: native FPE (FF1) on the chunked route (C6a plan §4).

Differential native-vs-oracle on the SAME `run_mask_chunked` entry (the `run_pair` harness runs
the configured native leg beside a forced-oracle leg and asserts byte parity: values, Arrow type,
field metadata, warnings, timings, vault). The native leg's `native_admitted is True` /
`reroute_reason is None` is the native-taken proof, so a silent reroute to the oracle would fail
the comparison instead of passing against itself.

Fail-closed parity, the checksum decline and the companion-absent fallback run the native and
oracle chunked routes directly (both legs raise / both decline, so `run_pair` does not apply).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import native_fpe
from tests.native._b8_support import assert_same_as_oracle, run_pair
from tests.native._chunked_entry_support import ENGINE_VERSION, TABLE, key_provider, make_config

_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable; the native FPE path requires it",
)
_MASK_KEY = bytes(range(32))
_NS = "people.ssn"


def fpe_col(name: str = "c", **pc: Any) -> dict[str, Any]:
    cfg = {"charset": "digits", **pc}
    return {"name": name, "strategy": "fpe", "namespace": _NS, "provider_config": cfg}


def _col(values: list[Any], typ: pa.DataType | None = None) -> pa.Array:
    return pa.array(values, type=typ or pa.string())


# Valid FF1 digit values sit at/above the million-element domain floor (radix 10, length >= 6).
_SSNS = ["123456789", "987654321", "123-45-6789", "555112222", "123456789", "000000001"]


# ── Byte-match matrix (native leg admitted, byte-equal to the oracle leg) ──


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "pc, values",
    [
        ({"charset": "digits"}, _SSNS),
        # preserve_separators=False needs in-charset-only values (a "-" would fail closed).
        ({"charset": "digits", "preserve_separators": False}, ["123456789", "987654321", None, ""]),
        (
            {"charset": "ALPHANUM"},
            ["AB12CD34EF", "ZZ99XX88YY", None, "MN45OP67QR", "", "AB12CD34EF"],
        ),
        (
            {"charset": "0123456789ABCDEF"},
            ["DEADBEEF1234", "0123456789AB", None, "", "DEADBEEF1234"],
        ),
        (
            {"charset": "digits", "validate_luhn": True},
            ["4111111111111111", "4012888888881881", None, ""],
        ),
    ],
    ids=["digits", "no_sep", "ALPHANUM", "custom_hex", "luhn"],
)
@pytest.mark.parametrize("batch", [2, 50_000], ids=["ragged", "one_batch"])
def test_native_chunked_matches_oracle_byte_identical(
    pc: dict[str, Any], values: list[Any], batch: int
) -> None:
    chunks = [pa.table({"c": _col(values[i : i + batch])}) for i in range(0, len(values), batch)]
    native, forced = run_pair([fpe_col(**pc)], chunks)
    assert_same_as_oracle(native, forced)


@_NEEDS_COMPANION
@pytest.mark.parametrize("batch", [2, 50_000], ids=["ragged", "one_batch"])
def test_native_chunked_partial_plaintext_warning_parity(batch: int) -> None:
    # An alphanumeric out-of-charset prefix retained under preserve_separators emits one
    # residual-risk warning per oracle-equivalent chunk; the native leg matches the oracle leg
    # chunk for chunk (assert_same_as_oracle compares each chunk's warnings).
    values = ["M000001", "M000002", "000003", None, "M000004", ""]
    chunks = [pa.table({"c": _col(values[i : i + batch])}) for i in range(0, len(values), batch)]
    native, forced = run_pair([fpe_col(preserve_separators=True)], chunks)
    assert_same_as_oracle(native, forced)
    emitted = [
        w for r in native.sink for w in r.warnings if w.code == "fpe_partial_plaintext_disclosure"
    ]
    assert emitted  # the native leg actually produced the warning, not just matched an empty set


@_NEEDS_COMPANION
def test_native_chunked_join_group_shares_ciphertext() -> None:
    chunks = [pa.table({"a": _col(_SSNS[:3]), "b": _col(_SSNS[:3])})]
    native, forced = run_pair(
        [fpe_col("a", fpe_join_group="grp"), fpe_col("b", fpe_join_group="grp")], chunks
    )
    assert_same_as_oracle(native, forced)
    a = native.out[0].column("a").to_pylist()
    b = native.out[0].column("b").to_pylist()
    assert a == b  # identical source values share ciphertext under one join group


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "label, values",
    [
        ("empty", []),
        ("all_null", [None, None, None]),
        ("all_empty", ["", "", ""]),
        ("populated", _SSNS),
        ("null_and_empty", [None, "", "123456789", None, ""]),
    ],
)
def test_native_chunked_degenerate_and_null_shapes(label: str, values: list[Any]) -> None:
    chunks = [pa.table({"c": _col(values)})]
    native, forced = run_pair([fpe_col()], chunks)
    assert_same_as_oracle(native, forced)


@_NEEDS_COMPANION
def test_native_chunked_pd_na_raw_list_shape() -> None:
    """A later all-null chunk (null-typed) is cast and masked identically on both legs; the
    Step-0 pd.NA alignment keeps the oracle and the kernel agreeing on missingness."""
    chunks = [
        pa.table({"c": _col(_SSNS[:3])}),
        pa.table({"c": _col([None, None], pa.null())}),
        pa.table({"c": _col(_SSNS[3:])}),
    ]
    native, forced = run_pair([fpe_col()], chunks)
    assert_same_as_oracle(native, forced)


# ── Fail-closed parity: same StrategyError code on both chunked legs (§3d) ──


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


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "pc, values, expected_code",
    [
        # out-of-charset with no separators to lean on -> fpe_unencryptable_value
        (
            {"charset": "digits", "preserve_separators": False},
            ["12ab34", "999999999"],
            "fpe_unencryptable_value",
        ),
        # below the FF1 million-domain floor (3 digits) -> fpe_unencryptable_domain
        ({"charset": "digits"}, ["123", "456"], "fpe_unencryptable_domain"),
        # a body longer than the FF1 profile's max length (256) -> fpe_unencryptable_length
        ({"charset": "digits"}, ["1" * 300, "123456789"], "fpe_unencryptable_length"),
    ],
    ids=["unencryptable_value", "unencryptable_domain", "unencryptable_length"],
)
def test_fail_closed_code_matches_oracle(
    pc: dict[str, Any], values: list[Any], expected_code: str
) -> None:
    config = make_config([fpe_col(**pc)])
    chunks = [pa.table({"c": _col(values)})]
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert type(native_exc) is type(oracle_exc)
    assert native_exc.code == oracle_exc.code == expected_code


@_NEEDS_COMPANION
def test_fail_closed_picks_the_first_failing_rows_code() -> None:
    """Two DIFFERENT failure codes in one chunk: the kill carries the FIRST failing row's code
    (lowest index), matching the oracle's first-failure raise, not the last or any other row."""
    # row 0: "123" is below the FF1 domain floor -> fpe_unencryptable_domain
    # row 1: "12ab34" is out of charset (no separators to keep) -> fpe_unencryptable_value
    config = make_config([fpe_col(preserve_separators=False)])
    chunks = [pa.table({"c": _col(["123", "12ab34"])})]
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert native_exc.code == oracle_exc.code == "fpe_unencryptable_domain"


@_NEEDS_COMPANION
def test_fail_closed_first_failure_in_later_chunk() -> None:
    """A bad value in the SECOND chunk kills with that value's code, matching the oracle's
    chunk-by-chunk first-failure raise."""
    config = make_config([fpe_col(preserve_separators=False)])
    chunks = [pa.table({"c": _col(_SSNS[:2])}), pa.table({"c": _col(["ok999999", "12ab34"])})]
    native_exc = _native_raises(config, chunks)
    oracle_exc = _oracle_raises(config, chunks)
    assert native_exc.code == oracle_exc.code


# ── Checksum decline to the oracle (§3g) ──


@_NEEDS_COMPANION
def test_checksum_declines_to_oracle() -> None:
    config = make_config([fpe_col(checksum="luhn")])
    chunks = [pa.table({"c": _col(["4111111111111111", "4012888888881881"])})]
    sink: list[Any] = []
    out = list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is False
    assert sink[0].reroute_reason == "fpe_checksum_not_native:c"
    oracle = list(
        run_mask_pipeline_chunked(
            config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        )
    )
    assert (
        pa.concat_tables(out).column("c").to_pylist()
        == pa.concat_tables(oracle).column("c").to_pylist()
    )


# ── Companion-absent fallback (§3f, test 6) ──


def test_companion_absent_declines_whole_table(monkeypatch: pytest.MonkeyPatch) -> None:
    import decoy_engine.execution.native._dispatch as dispatch
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError

    def _boom() -> Any:
        raise CryptoExtensionUnavailableError("fpe companion forced absent for the test")

    monkeypatch.setattr(dispatch, "load_compiled_fpe_kernel", _boom)
    config = make_config([fpe_col()])
    chunks = [pa.table({"c": _col(_SSNS)})]
    sink: list[Any] = []
    out = list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is False
    assert sink[0].reroute_reason == "fpe_extension_unavailable"
    oracle = list(
        run_mask_pipeline_chunked(
            config, chunks, table=TABLE, engine_version=ENGINE_VERSION, key_provider=key_provider()
        )
    )
    assert (
        pa.concat_tables(out).column("c").to_pylist()
        == pa.concat_tables(oracle).column("c").to_pylist()
    )


# ── Admission matrix: non-string sources decline whole-table (§3c, test 2b) ──


@pytest.mark.parametrize(
    "typ, values, frag",
    [
        (
            pa.large_string(),
            ["123456789", "987654321"],
            "fpe_source_type_not_string:c:large_string",
        ),
        (pa.int64(), [123456789, 987654321], "fpe_source_type_not_string:c:int64"),
    ],
    ids=["large_string", "int64"],
)
def test_nonstring_source_declines(typ: pa.DataType, values: list[Any], frag: str) -> None:
    config = make_config([fpe_col()])
    chunks = [pa.table({"c": _col(values, typ)})]
    sink: list[Any] = []
    list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is False
    assert frag in (sink[0].reroute_reason or "")


@_NEEDS_COMPANION
def test_dict_encoded_string_source_declines() -> None:
    config = make_config([fpe_col()])
    dict_arr = pa.array(_SSNS[:4]).dictionary_encode()
    chunks = [pa.table({"c": dict_arr})]
    sink: list[Any] = []
    list(
        run_mask_chunked(
            config,
            chunks,
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            route_evidence_sink=sink,
        )
    )
    assert sink[0].native_admitted is False


# ── Kernel round-trip: decrypt restores the plaintext (test 2 encrypt + decrypt) ──


@_NEEDS_COMPANION
def test_namespace_none_fails_closed_like_the_handler() -> None:
    """A None namespace raises `fpe_requires_namespace` before the kernel loads, matching the
    shipped handler's class + code (config-error parity, plan §3d)."""
    with pytest.raises(StrategyError) as exc:
        native_fpe(
            pa.array(_SSNS[:2], type=pa.string()),
            mask_key=_MASK_KEY,
            namespace=None,
            tweak_column="c",
            config=FpeConfig(charset="digits"),
        )
    assert exc.value.code == "fpe_requires_namespace"


@_NEEDS_COMPANION
@pytest.mark.parametrize(
    "charset, values",
    [
        ("digits", _SSNS[:4]),
        ("ALPHANUM", ["AB12CD34EF", "ZZ99XX88YY"]),
    ],
)
def test_kernel_round_trip(charset: str, values: list[Any]) -> None:
    cfg = FpeConfig(charset=charset)
    enc = native_fpe(
        pa.array(values, type=pa.string()),
        mask_key=_MASK_KEY,
        namespace=_NS,
        tweak_column="c",
        config=cfg,
    )
    dec = native_fpe(
        enc.values, mask_key=_MASK_KEY, namespace=_NS, tweak_column="c", config=cfg, forward=False
    )
    assert dec.values.to_pylist() == values
    # The kernel's ciphertext and per-row errors match the pure-Python reference oracle.
    ref = reference_fpe().encrypt_batch(
        pa.array(values, type=pa.string()),
        mask_key=_MASK_KEY,
        namespace=_NS,
        tweak_column="c",
        config=cfg,
    )
    assert enc.values.to_pylist() == ref.values.to_pylist()
    assert enc.errors == ref.errors
