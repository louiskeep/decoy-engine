"""Generate the shared keyed-derivation KAT fixture from the live Python reference.

The Rust `kat_derive.rs` test and the Python cross-process parity harness both read
`keyed_derivation_kat.json`. Values here come from RUNNING
`decoy_engine.execution.native._crypto_ext.reference_keyed_derivation()` and
`decoy_engine.kernel._canonicalize.canonicalize_derive_source`, never from a
hand-derived byte guess, so the fixture is correct by construction against the
shipped Python behavior (the compatibility oracle per crypto-testing-reference
Gate 2). Re-run this script only when the Python reference itself changes
(a SEED_PROTOCOL_VERSION bump), never to chase a Rust mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine.execution.native._crypto_ext import reference_keyed_derivation
from decoy_engine.kernel._canonicalize import canonicalize_derive_source

_MASK_KEY = bytes(range(32))
_REFERENCE = reference_keyed_derivation()


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
    """Convert a JSON-schema logical value to what `pa.array` expects."""
    if value is None:
        return None
    kind = descriptor["kind"]
    if kind == "int":
        return int(value)  # decimal string -> Python int (arbitrary magnitude safe)
    if kind == "timestamp":
        # pd.Timestamp parses fractional seconds up to nanosecond precision,
        # which datetime.fromisoformat cannot (needed for the ns-unit cases).
        return pd.Timestamp(value)
    return value  # str already matches for utf8/bool


def build_case(
    name: str,
    *,
    arrow_type: dict[str, Any],
    logical_values: list[Any],
    namespace: str,
    truncate: int | None = None,
    mask_key: bytes = _MASK_KEY,
) -> dict[str, Any]:
    dtype = _arrow_type(arrow_type)
    native_values = [_logical_to_native(arrow_type, v) for v in logical_values]
    array = pa.array(native_values, type=dtype)

    canonical_hex: list[str | None] = []
    for value in array.to_pylist():
        canonical_hex.append(None if value is None else canonicalize_derive_source(value).hex())

    output = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=truncate
    )

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
        "mask_key_hex": mask_key.hex(),
        "namespace": namespace,
        "truncate": truncate,
        "expected_canonical_source_hex": canonical_hex,
        "expected_output": output.to_pylist(),
    }


def _int_type(bits: int, signed: bool) -> dict[str, Any]:
    return {"kind": "int", "bits": bits, "signed": signed}


def _ts_type(unit: str, tz: str) -> dict[str, Any]:
    return {"kind": "timestamp", "unit": unit, "tz": tz}


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # --- utf8 / large_utf8 -------------------------------------------------
    cases.append(
        build_case(
            "utf8_basic_with_nulls",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob", None, ""],
            namespace="people.name",
        )
    )
    cases.append(
        build_case(
            "utf8_truncated",
            arrow_type={"kind": "utf8"},
            logical_values=["alice", "bob", None],
            namespace="people.name",
            truncate=16,
        )
    )
    cases.append(
        build_case(
            "large_utf8_basic_with_nulls",
            arrow_type={"kind": "large_utf8"},
            logical_values=["alice", None, "carol"],
            namespace="people.name",
        )
    )
    # NFC composed vs NFD decomposed forms of the same logical string ("café")
    # must canonicalize to the SAME bytes (Unicode normalization drift guard).
    cases.append(
        build_case(
            "utf8_nfc_nfd_equivalence",
            arrow_type={"kind": "utf8"},
            logical_values=["café", "café"],
            namespace="people.name",
        )
    )
    # Framing-ambiguity / namespace-vs-source boundary pair: moving bytes
    # between namespace and source must change the result despite an
    # identical raw concatenation ("ab" + "c" == "a" + "bc").
    cases.append(
        build_case(
            "framing_boundary_ab_c",
            arrow_type={"kind": "utf8"},
            logical_values=["c"],
            namespace="ab",
        )
    )
    cases.append(
        build_case(
            "framing_boundary_a_bc",
            arrow_type={"kind": "utf8"},
            logical_values=["bc"],
            namespace="a",
        )
    )

    # --- integer widths, signed and unsigned --------------------------------
    int_boundaries = {
        (8, True): ["-128", "-1", "0", "1", "127", None],
        (8, False): ["0", "1", "255", None],
        (16, True): ["-32768", "-1", "0", "1", "32767", None],
        (16, False): ["0", "1", "65535", None],
        (32, True): ["-2147483648", "-1", "0", "1", "2147483647", None],
        (32, False): ["0", "1", "4294967295", None],
        (64, True): ["-9223372036854775808", "-1", "0", "1", "9223372036854775807", None],
        (64, False): ["0", "1", "18446744073709551615", None],
    }
    for (bits, signed), values in int_boundaries.items():
        signedness = "signed" if signed else "unsigned"
        cases.append(
            build_case(
                f"int{bits}_{signedness}_boundaries",
                arrow_type=_int_type(bits, signed),
                logical_values=values,
                namespace="accounts.balance",
            )
        )
    cases.append(
        build_case(
            "int64_signed_truncated",
            arrow_type=_int_type(64, True),
            logical_values=["12345", None],
            namespace="accounts.balance",
            truncate=16,
        )
    )

    # --- bool ----------------------------------------------------------------
    cases.append(
        build_case(
            "bool_basic_with_null",
            arrow_type={"kind": "bool"},
            logical_values=[True, False, None],
            namespace="flags.active",
        )
    )

    # --- timestamp-with-tz, every supported unit ------------------------------
    cases.append(
        build_case(
            "timestamp_s_utc",
            arrow_type=_ts_type("s", "UTC"),
            logical_values=["2020-01-01T12:30:45+00:00", None],
            namespace="events.at",
        )
    )
    cases.append(
        build_case(
            "timestamp_ms_utc",
            arrow_type=_ts_type("ms", "UTC"),
            logical_values=["2020-01-01T12:30:45.120000+00:00", None],
            namespace="events.at",
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
            namespace="events.at",
        )
    )
    cases.append(
        build_case(
            "timestamp_ns_utc_fraction_precision",
            arrow_type=_ts_type("ns", "UTC"),
            logical_values=[
                "2020-01-01T12:30:45.123456789+00:00",  # true sub-microsecond digits
                "2020-01-01T12:30:45.123456000+00:00",  # exact microsecond multiple
                "2020-01-01T12:30:45+00:00",  # zero fraction
                None,
            ],
            namespace="events.at",
        )
    )
    cases.append(
        build_case(
            "timestamp_ns_non_utc_tz",
            arrow_type=_ts_type("ns", "America/New_York"),
            logical_values=["2020-06-15T08:00:00.000000001-04:00", None],
            namespace="events.at",
        )
    )

    return cases


def main() -> None:
    fixture = {"format_version": 1, "cases": build_cases()}
    out_path = Path(__file__).parent / "keyed_derivation_kat.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {len(fixture['cases'])} cases to {out_path}")


if __name__ == "__main__":
    main()
