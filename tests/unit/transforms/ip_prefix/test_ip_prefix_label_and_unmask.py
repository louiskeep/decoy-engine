"""The Crypto-PAn key label cannot be reached from data, the key is pinned, and unmask reverses."""

from __future__ import annotations

import ipaddress
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from decoy_engine.determinism import derive
from decoy_engine.kernel import canonicalize_derive_source, hash_array
from decoy_engine.transforms.network._cryptopan import CRYPTOPAN_KEY_LABEL
from decoy_engine.unmask import unmask_pipeline
from tests.unit.transforms.ip_prefix import _support as s

_LABEL = CRYPTOPAN_KEY_LABEL


def test_label_shape_is_unreachable_by_construction() -> None:
    assert _LABEL[0] == 0xFF  # not valid UTF-8 as a lead byte, not an _encode_int length prefix
    assert len(_LABEL) > 1  # not a canonical bool
    with pytest.raises(UnicodeDecodeError):
        _LABEL.decode("utf-8")


_SOURCES = st.one_of(
    st.text(),
    st.binary(),
    st.integers(min_value=-(2**200), max_value=2**200),
    st.booleans(),
    st.decimals(allow_nan=False, allow_infinity=False),
    st.dates(),
    st.datetimes(timezones=st.just(timezone.utc)),
)


@seed(20261107)
@settings(max_examples=2000, deadline=None)
@given(value=_SOURCES)
def test_no_canonical_source_equals_the_label(value: object) -> None:
    assert canonicalize_derive_source(value) != _LABEL


@pytest.mark.parametrize(
    "value",
    [
        _LABEL,
        _LABEL.decode("latin-1"),
        _LABEL[1:].decode("ascii"),
        "�" + _LABEL[1:].decode("ascii"),
        Decimal("1"),
        date(2026, 11, 7),
        datetime(2026, 11, 7, tzinfo=timezone.utc),
    ],
)
def test_a_hash_cell_never_emits_the_cryptopan_key(value: object) -> None:
    key_hex = derive(s.MASK_KEY, "ip_ns", _LABEL).hex()
    out = hash_array([value], seed=s.MASK_KEY, namespace="ip_ns", derive_func=derive)
    assert out.to_pylist() != [key_hex]


def test_a_binary_arrow_column_canonicalises_through_str_not_raw_bytes() -> None:
    key_hex = derive(s.MASK_KEY, "ip_ns", _LABEL).hex()
    column = pa.array([_LABEL, b"\xff"], type=pa.binary())
    out = hash_array(column, seed=s.MASK_KEY, namespace="ip_ns", derive_func=derive).to_pylist()
    assert key_hex not in out
    assert out[0] == derive(s.MASK_KEY, "ip_ns", str(_LABEL).encode("utf-8")).hex()


def test_a_binary_hash_column_through_the_pipeline_never_emits_the_key(tmp_path: Path) -> None:
    key_hex = derive(s.KEY_PROVIDER.mask_key(), "ip_ns", _LABEL).hex()
    table = pa.table({"h": pa.array([_LABEL, b"x"], type=pa.binary())})
    result = s.pipeline(
        tmp_path, {"t": table}, {"t": [{"name": "h", "strategy": "hash", "namespace": "ip_ns"}]}
    )
    assert key_hex not in result.outputs["t"].column("h").to_pylist()


_GOLDEN_IN = ["192.0.2.1", "10.0.0.255", "::ffff:192.0.2.1", "2001:db8::1", "fe80::abcd"]
_GOLDEN_OUT = [
    "192.0.2.226",
    "10.0.0.14",
    "::ffff:192.0.2.226",
    "2001:db8:0:dfdc:1fbc:47f:9ff:e107",
    "fe80::dfe0:8838:83b8:fe6f:53ce",
]


def test_golden_outputs_pin_the_label_and_the_keying() -> None:
    out, ctx = s.run_handler(list(_GOLDEN_IN))
    assert ctx.row_errors == []
    assert out == _GOLDEN_OUT


def _unmask_config(tmp_path: Path, **provider_config: object) -> dict:
    cfg = s.config(("t", [s.column(**provider_config)]))
    cfg.update(s.write_sources(tmp_path, {"t": pa.table({"ip": ["192.0.2.0"]})}))
    return cfg


def test_unmask_recovers_every_address_and_reports_unverified(tmp_path: Path) -> None:
    sources = ["192.0.2.1", "10.9.8.7", "::FFFF:192.0.2.9", "2001:DB8::1", "fe80::1", None]
    masked = s.pipeline(
        tmp_path,
        {"t": pa.table({"ip": pa.array(sources, type=pa.string())})},
        {"t": [s.column(keep_bits_v4=16)]},
    ).outputs["t"]
    result = unmask_pipeline(
        _unmask_config(tmp_path, keep_bits_v4=16), {"t": masked}, key_provider=s.KEY_PROVIDER
    )
    expected = [None if v is None else str(ipaddress.ip_address(v)) for v in sources]
    expected[2] = "::ffff:192.0.2.9"
    assert result.outputs["t"].column("ip").to_pylist() == expected
    (report,) = [r for r in result.columns if r.column == "ip"]
    assert report.status == "reversed_unverified"
    assert report.strategy == "ip_prefix"


def test_unmask_with_another_namespace_gives_different_addresses(tmp_path: Path) -> None:
    sources = ["192.0.2.1", "2001:db8::1"]
    masked = s.pipeline(tmp_path, {"t": pa.table({"ip": sources})}, {"t": [s.column()]}).outputs[
        "t"
    ]
    cfg = _unmask_config(tmp_path)
    cfg["tables"][0]["columns"][0]["namespace"] = "other_ns"
    result = unmask_pipeline(cfg, {"t": masked}, key_provider=s.KEY_PROVIDER)
    recovered = result.outputs["t"].column("ip").to_pylist()
    assert recovered != sources
    assert recovered[0].startswith("192.0.")
    (report,) = [r for r in result.columns if r.column == "ip"]
    assert report.status == "reversed_unverified"


def test_unmask_leaves_a_non_address_alone_and_never_echoes_it(tmp_path: Path) -> None:
    masked = pa.table({"ip": [s.SENTINEL_BAD, "192.0.2.1"]})
    result = unmask_pipeline(_unmask_config(tmp_path), {"t": masked}, key_provider=s.KEY_PROVIDER)
    assert result.outputs["t"].column("ip").to_pylist()[0] == s.SENTINEL_BAD
    (report,) = [r for r in result.columns if r.column == "ip"]
    assert "1 value(s)" in report.detail
    assert s.SENTINEL_BAD not in report.detail
