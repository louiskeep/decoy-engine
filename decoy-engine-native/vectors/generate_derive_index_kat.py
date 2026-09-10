"""Generate the shared derive_index KAT fixture from the live Python reference.

Phase 2 Task 2.1: freeze the deterministic-Faker pool-selection contract before any
compiled `derive_index_batch` exists. The eventual Rust `derive_index_batch` and a
Python pinning test both read `derive_index_kat.json`. Every value here comes from
RUNNING the shipped `decoy_engine.determinism.derive_index` over the shipped
`canonicalize_derive_source` (the same canonicalization object the keyed-hash kernel
uses; verified identical), never a hand-derived guess, so the fixture is correct by
construction against shipped Python behavior. Re-run this ONLY when the Python
reference itself changes (a SEED_PROTOCOL_VERSION bump), never to chase a Rust
mismatch: a bump invalidates every expected index here, exactly like the hash KAT.

Index reduction under test: `int.from_bytes(derive(seed, namespace, source)[:8], "big")
% pool_size`, where `derive` is the frozen HMAC-SHA256 envelope
(`byte(SEED_PROTOCOL_VERSION) ++ u32be(len(ns)) ++ ns ++ u32be(len(source)) ++ source`,
keyed by HKDF-SHA256(seed, salt=b"decoy-engine/determinism/v1", info=ns, len=32)).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.determinism import DeterminismError, derive_index
from decoy_engine.determinism._derive import SEED_PROTOCOL_VERSION
from decoy_engine.generation.pool._canonicalize import _canonicalize_source

# Matches the hash KAT's shared 32-byte mask_key so cross-vector reasoning lines up
# (bytes(range(32))); the 8-byte job_seed exercises the no-secret seed length.
_MASK_KEY = bytes(range(32))
_JOB_SEED = bytes(range(8))


def _arrow_type(descriptor: dict[str, Any]) -> pa.DataType:
    kind = descriptor["kind"]
    if kind == "utf8":
        return pa.string()
    if kind == "large_utf8":
        return pa.large_string()
    if kind == "bool":
        return pa.bool_()
    if kind == "int":
        widths = {
            (8, True): pa.int8(),
            (8, False): pa.uint8(),
            (16, True): pa.int16(),
            (16, False): pa.uint16(),
            (32, True): pa.int32(),
            (32, False): pa.uint32(),
            (64, True): pa.int64(),
            (64, False): pa.uint64(),
        }
        return widths[(descriptor["bits"], descriptor["signed"])]
    if kind == "timestamp":
        return pa.timestamp(descriptor["unit"], tz=descriptor["tz"])
    raise ValueError(f"unhandled arrow_type kind {kind!r}")


def _logical_to_native(descriptor: dict[str, Any], value: Any) -> Any:
    if value is None:
        return None
    kind = descriptor["kind"]
    if kind == "int":
        return int(value)  # decimal string -> arbitrary-magnitude Python int
    if kind == "timestamp":
        return pd.Timestamp(value)  # parses fractional seconds to ns precision
    return value


def build_case(
    name: str,
    *,
    arrow_type: dict[str, Any],
    logical_values: list[Any],
    namespace: str,
    pool_size: int,
    seed: bytes = _MASK_KEY,
) -> dict[str, Any]:
    dtype = _arrow_type(arrow_type)
    native_values = [_logical_to_native(arrow_type, v) for v in logical_values]
    array = pa.array(native_values, type=dtype)

    canonical_hex: list[str | None] = []
    expected_index: list[int | None] = []
    # Null in -> null out: the sampler never calls canonicalize/derive_index for a null
    # row (verified in generation/pool/_sampler.py), so the fixture records null there.
    for value in array.to_pylist():
        if value is None:
            canonical_hex.append(None)
            expected_index.append(None)
            continue
        canonical = _canonicalize_source(value)
        canonical_hex.append(canonical.hex())
        expected_index.append(derive_index(seed, namespace, canonical, pool_size=pool_size))

    return {
        "name": name,
        "arrow_type": {
            "kind": arrow_type["kind"],
            "bits": arrow_type.get("bits"),
            "signed": arrow_type.get("signed"),
            "unit": arrow_type.get("unit"),
            "tz": arrow_type.get("tz"),
        },
        "logical_values": logical_values,
        "seed_hex": seed.hex(),
        "namespace": namespace,
        "pool_size": pool_size,
        "expected_canonical_source_hex": canonical_hex,
        "expected_index": expected_index,
    }


def _int_type(bits: int, signed: bool) -> dict[str, Any]:
    return {"kind": "int", "bits": bits, "signed": signed}


def _ts_type(unit: str, tz: str) -> dict[str, Any]:
    return {"kind": "timestamp", "unit": unit, "tz": tz}


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # --- utf8 / large_utf8, pool-size variety + null preservation ------------
    cases.append(
        build_case(
            "utf8_basic_with_nulls_pool1000",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob", None, ""],
            namespace="pool.city",
            pool_size=1000,
        )
    )
    # pool_size == 1 is degenerate: every non-null row maps to index 0.
    cases.append(
        build_case(
            "utf8_pool_size_one_all_zero",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob", None],
            namespace="pool.city",
            pool_size=1,
        )
    )
    # pool_size == 2**56 is ACCEPTED (guard is strictly `> 2**56`): pins the
    # inclusive boundary a compiled port must not shift to `>=`.
    cases.append(
        build_case(
            "utf8_pool_size_at_max_boundary",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob"],
            namespace="pool.city",
            pool_size=1 << 56,
        )
    )
    cases.append(
        build_case(
            "large_utf8_with_nulls",
            arrow_type={"kind": "large_utf8"},
            logical_values=["alice", None, "carol"],
            namespace="pool.city",
            pool_size=997,
        )
    )
    # A zero-non-null column (every row null) and an empty column: the sampler sizes draws
    # on the non-null count, so a compiled null-only fast path must still produce null/empty
    # without deriving or erroring.
    cases.append(
        build_case(
            "utf8_all_null",
            arrow_type={"kind": "utf8"},
            logical_values=[None, None, None],
            namespace="pool.city",
            pool_size=1000,
        )
    )
    cases.append(
        build_case(
            "utf8_empty",
            arrow_type={"kind": "utf8"},
            logical_values=[],
            namespace="pool.city",
            pool_size=1000,
        )
    )
    # NFC vs NFD forms of "café" must select the SAME index (normalization guard).
    cases.append(
        build_case(
            "utf8_nfc_nfd_equivalence",
            arrow_type={"kind": "utf8"},
            logical_values=["café", "café"],
            namespace="pool.city",
            pool_size=1000,
        )
    )
    # Framing boundary: "ab"+"c" vs "a"+"bc" share a raw concatenation but must
    # select different indices, proving the length-prefixed namespace/source frame.
    cases.append(
        build_case(
            "framing_boundary_ab_c",
            arrow_type={"kind": "utf8"},
            logical_values=["c"],
            namespace="ab",
            pool_size=100000,
        )
    )
    cases.append(
        build_case(
            "framing_boundary_a_bc",
            arrow_type={"kind": "utf8"},
            logical_values=["bc"],
            namespace="a",
            pool_size=100000,
        )
    )
    # Multi-byte UTF-8 namespace ("café.地区": Latin accent + CJK) whose source ALSO derives a
    # digest whose first 8 bytes have the high bit set (uint64 >= 2**63). One vector, two
    # discriminators: (1) the frame's namespace length prefix must be the UTF-8 BYTE length, not
    # the character count, so a port that mis-measures a non-ASCII namespace fails; (2) the digest
    # slice must be read as an UNSIGNED uint64, not a signed int64 -- for "alice" here the unsigned
    # index is 88 while a signed reading would give 472.
    cases.append(
        build_case(
            "utf8_unicode_namespace_high_bit_digest",
            arrow_type={"kind": "utf8"},
            logical_values=["alice"],
            namespace="café.地区",
            pool_size=1000,
        )
    )
    # 8-byte job_seed (no-secret path) alongside the 32-byte mask_key default: both
    # admitted seed lengths must derive an index, proving seed-length parity.
    cases.append(
        build_case(
            "utf8_job_seed_8_byte",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob", None],
            namespace="pool.city",
            pool_size=1000,
            seed=_JOB_SEED,
        )
    )

    # --- integer widths, signed and unsigned (canonicalization boundaries) ---
    int_boundaries = {
        (8, True): ["-128", "-1", "0", "1", "127", None],
        (8, False): ["0", "1", "255", None],
        (16, True): ["-32768", "0", "32767", None],
        (16, False): ["0", "65535", None],
        (32, True): ["-2147483648", "0", "2147483647", None],
        (32, False): ["0", "4294967295", None],
        (64, True): ["-9223372036854775808", "0", "9223372036854775807", None],
        (64, False): ["0", "18446744073709551615", None],
    }
    for (bits, signed), values in int_boundaries.items():
        signedness = "signed" if signed else "unsigned"
        cases.append(
            build_case(
                f"int{bits}_{signedness}_boundaries",
                arrow_type=_int_type(bits, signed),
                logical_values=values,
                namespace="pool.code",
                pool_size=4099,
            )
        )

    # --- bool ----------------------------------------------------------------
    cases.append(
        build_case(
            "bool_basic_with_null",
            arrow_type={"kind": "bool"},
            logical_values=[True, False, None],
            namespace="pool.flag",
            pool_size=64,
        )
    )

    # --- timestamp-with-tz, every supported unit -----------------------------
    cases.append(
        build_case(
            "timestamp_s_utc",
            arrow_type=_ts_type("s", "UTC"),
            logical_values=["2020-01-01T12:30:45+00:00", None],
            namespace="pool.at",
            pool_size=1000,
        )
    )
    cases.append(
        build_case(
            "timestamp_ms_utc",
            arrow_type=_ts_type("ms", "UTC"),
            logical_values=["2020-01-01T12:30:45.120000+00:00", None],
            namespace="pool.at",
            pool_size=1000,
        )
    )
    cases.append(
        build_case(
            "timestamp_us_utc_with_negative_epoch",
            arrow_type=_ts_type("us", "UTC"),
            logical_values=[
                "2020-01-01T12:30:45.500000+00:00",
                "1969-12-31T23:59:59.999999+00:00",
                None,
            ],
            namespace="pool.at",
            pool_size=1000,
        )
    )
    cases.append(
        build_case(
            "timestamp_ns_utc_fraction_precision",
            arrow_type=_ts_type("ns", "UTC"),
            logical_values=[
                "2020-01-01T12:30:45.123456789+00:00",
                "2020-01-01T12:30:45.123456000+00:00",
                "2020-01-01T12:30:45+00:00",
                None,
            ],
            namespace="pool.at",
            pool_size=1000,
        )
    )
    cases.append(
        build_case(
            "timestamp_ns_non_utc_tz",
            arrow_type=_ts_type("ns", "America/New_York"),
            logical_values=["2020-06-15T08:00:00.000000001-04:00", None],
            namespace="pool.at",
            pool_size=1000,
        )
    )

    return cases


def build_error_cases() -> list[dict[str, Any]]:
    """Pool-size and framing rejections, recorded with the exact coded error a
    compiled port must reproduce. Generated by actually calling derive_index and
    capturing the raised DeterminismError code."""
    source = _canonicalize_source("alice")
    specs = [
        ("pool_size_zero", _MASK_KEY, "pool.city", 0),
        ("pool_size_negative", _MASK_KEY, "pool.city", -5),
        ("pool_size_above_max", _MASK_KEY, "pool.city", (1 << 56) + 1),
        ("seed_wrong_length_16", bytes(16), "pool.city", 1000),
        ("namespace_empty", _MASK_KEY, "", 1000),
        # Combined-fault cases pin the guard ORDER: derive_index checks pool_size BEFORE
        # deriving (so seed/namespace faults never surface when pool_size is also bad). A port
        # that derives first and guards pool afterward would pass every single-fault case above
        # yet diverge here. Both a bad seed AND a bad namespace paired with a bad pool_size must
        # still report the pool_size code.
        ("pool_invalid_beats_seed", bytes(16), "pool.city", 0),
        ("pool_overflow_beats_namespace", _MASK_KEY, "", (1 << 56) + 1),
    ]
    out: list[dict[str, Any]] = []
    for name, seed, namespace, pool_size in specs:
        try:
            derive_index(seed, namespace, source, pool_size=pool_size)
        except DeterminismError as exc:
            code = exc.code
        else:  # pragma: no cover - a missing rejection is a contract regression
            raise AssertionError(f"{name}: expected a DeterminismError, none raised")
        out.append(
            {
                "name": name,
                "seed_hex": seed.hex(),
                "namespace": namespace,
                "source_hex": source.hex(),
                "pool_size": pool_size,
                "expected_error_code": code,
            }
        )
    return out


def main() -> None:
    cases = build_cases()
    error_cases = build_error_cases()
    fixture = {
        "format_version": 1,
        "seed_protocol_version": SEED_PROTOCOL_VERSION,
        "seed_protocol_version_note": (
            "expected_index values are valid only for this seed_protocol_version and the "
            "canonicalization live when generated; a bump invalidates them all"
        ),
        "cases": cases,
        "error_cases": error_cases,
    }
    out_path = Path(__file__).parent / "derive_index_kat.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases + {len(error_cases)} error cases to {out_path}")


if __name__ == "__main__":
    main()
