"""Cross-process parity harness for `native_keyed_hash` (Task 2.5), per
crypto-testing-reference Section 5.3 (cross-language parity harness) and
Section 5.4 (mutation tests for the harness).

Section 5.3 requires the harness to launch the reference and the native
kernel through their PUBLIC entry points, never re-implement protocol
framing inside the harness, and run the two in different processes at
least once. `_run_native_in_subprocess` below spawns a fresh interpreter
that imports `native_keyed_hash` and calls it exactly as production code
would; the parent process computes the reference side. Neither side's
framing is reimplemented here.

Section 5.4 requires proving the harness itself can fail: a parity suite
that is green while silently omitting a critical field is not evidence of
anything. `_assert_matches_reference` is the one comparison every real
parity test in this file uses; the `TestHarnessMutations` class below
feeds it deliberately broken test-only implementations (never the shipped
`derive`/`native_keyed_hash` code) and asserts each one is caught.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.determinism._derive import _SALT
from decoy_engine.determinism._hkdf import hkdf_sha256
from decoy_engine.execution.native._crypto_ext import HASH_KAT, reference_keyed_derivation
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash
from decoy_engine.kernel._canonicalize import canonicalize_derive_source

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None

pytestmark = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed (maturin develop); "
    "the companion-present CI job covers this",
)

_MASK_KEY = bytes(range(32))
_NAMESPACE = "people.ssn"
_REFERENCE = reference_keyed_derivation()
_KAT_FIXTURE_PATH = (
    Path(__file__).parents[3] / "decoy-engine-native" / "vectors" / "keyed_derivation_kat.json"
)


def _serialize(array: pa.Array) -> dict[str, Any]:
    """Canonical representation for comparison: Arrow type plus logical values.

    Type is part of the comparison (not only `.to_pylist()`) so a wrapper
    that returns `large_string` where the contract requires `string` is
    caught the same way `test_kernels_scalar.py` and
    `test_crypto_ext_loader.py` catch it."""
    return {"type": str(array.type), "values": array.to_pylist()}


def _assert_matches_reference(
    source: pa.Array,
    candidate: pa.Array,
    *,
    mask_key: bytes,
    namespace: str,
    truncate: int | None,
) -> None:
    """The one comparison this harness makes: `candidate` (native_keyed_hash's
    output, or a stand-in under `TestHarnessMutations`) against the LIVE
    reference recomputed from `source`, the same input values `candidate`
    was itself derived from. Every real parity test below and every mutation
    test in `TestHarnessMutations` calls this exact function, so proving it
    can fail (Section 5.4) proves the real parity tests are not vacuously
    green."""
    reference_out = _REFERENCE.derive_batch(
        source, mask_key=mask_key, namespace=namespace, truncate=truncate
    )
    assert _serialize(candidate) == _serialize(reference_out)


# ---------------------------------------------------------------------------
# In-process: native_keyed_hash vs the live reference, over HASH_KAT and the
# shared KAT fixture (re-run through the LIVE reference, not its own pinned
# expected_output, so a reference-side regression is also caught here).
# ---------------------------------------------------------------------------


def test_native_keyed_hash_matches_reference_over_every_hash_kat_vector() -> None:
    for vector in HASH_KAT:
        array = pa.array([vector.value])
        got = native_keyed_hash(
            array, mask_key=vector.mask_key, namespace=vector.namespace, truncate=vector.truncate
        )
        _assert_matches_reference(
            array,
            got,
            mask_key=vector.mask_key,
            namespace=vector.namespace,
            truncate=vector.truncate,
        )
        assert got.to_pylist() == [vector.expected], vector


def _arrow_type_from_fixture(descriptor: dict[str, Any]) -> pa.DataType:
    """Mirrors `test_keyed_derivation_kernel_parity.py`'s helper of the same
    name: the shared KAT fixture covers every admitted Arrow type, not only
    strings, so building the array requires the same type mapping."""
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
    if descriptor["kind"] == "int":
        return int(value)
    if descriptor["kind"] == "timestamp":
        import pandas as pd

        return pd.Timestamp(value)
    return value


@pytest.mark.skipif(not _KAT_FIXTURE_PATH.exists(), reason="shared KAT fixture not generated yet")
def test_native_keyed_hash_matches_reference_over_the_shared_kat_fixture() -> None:
    fixture = json.loads(_KAT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["cases"], "the shared fixture must not be empty"

    for case in fixture["cases"]:
        mask_key = bytes.fromhex(case["mask_key_hex"])
        dtype = _arrow_type_from_fixture(case["arrow_type"])
        native_values = [_logical_to_native(case["arrow_type"], v) for v in case["logical_values"]]
        array = pa.array(native_values, type=dtype)
        got = native_keyed_hash(
            array, mask_key=mask_key, namespace=case["namespace"], truncate=case["truncate"]
        )
        _assert_matches_reference(
            array, got, mask_key=mask_key, namespace=case["namespace"], truncate=case["truncate"]
        )
        assert got.to_pylist() == case["expected_output"], case["name"]


# ---------------------------------------------------------------------------
# Cross-process: the reference runs in THIS process; native_keyed_hash runs
# in a freshly spawned interpreter. Detects process-local shared state and
# import-time configuration that an in-process comparison cannot.
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = """
import json
import sys

import pyarrow as pa

from decoy_engine.execution.native._kernels_keyed import native_keyed_hash

payload = json.loads(sys.argv[1])
array = pa.array(payload["values"], type=pa.string())
out = native_keyed_hash(
    array,
    mask_key=bytes.fromhex(payload["mask_key_hex"]),
    namespace=payload["namespace"],
    truncate=payload["truncate"],
)
sys.stdout.write(json.dumps({"type": str(out.type), "values": out.to_pylist()}))
"""


def _run_native_in_subprocess(
    *, values: list[str | None], mask_key: bytes, namespace: str, truncate: int | None
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "values": values,
            "mask_key_hex": mask_key.hex(),
            "namespace": namespace,
            "truncate": truncate,
        }
    )
    proc = subprocess.run(  # noqa: S603 -- args are test literals, not untrusted input
        [sys.executable, "-c", _CHILD_SCRIPT, payload],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize("truncate", [None, 16])
def test_native_keyed_hash_matches_reference_across_processes(truncate: int | None) -> None:
    values: list[str | None] = ["alice", "bob", None, "carol", "", "café"]
    reference_out = _REFERENCE.derive_batch(
        pa.array(values, type=pa.string()),
        mask_key=_MASK_KEY,
        namespace=_NAMESPACE,
        truncate=truncate,
    )
    child_result = _run_native_in_subprocess(
        values=values, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=truncate
    )
    assert child_result == _serialize(reference_out)


def test_native_keyed_hash_subprocess_with_a_different_key_diverges() -> None:
    """Sanity: the subprocess call is not a no-op that would pass regardless
    of input. A different mask_key must produce a different token."""
    values = ["alice"]
    result_a = _run_native_in_subprocess(
        values=values, mask_key=bytes(range(32)), namespace=_NAMESPACE, truncate=None
    )
    result_b = _run_native_in_subprocess(
        values=values, mask_key=bytes(range(1, 33)), namespace=_NAMESPACE, truncate=None
    )
    assert result_a != result_b


# ---------------------------------------------------------------------------
# Section 5.4: mutation tests for the harness itself. Each mutated function
# below is test-only -- it never touches shipped code -- and each must make
# `_assert_matches_reference` fail for at least one crafted case.
# ---------------------------------------------------------------------------


def _mutated_derive_namespace_dropped(namespace: str, source: bytes) -> bytes:
    """Section 5.4 mutation: "remove the namespace from the derivation
    frame." Neither the HKDF `info` nor the HMAC frame binds `namespace`
    here, so two different namespaces over the same source collide -- the
    exact defect a real compiled-kernel regression of this kind would
    produce."""
    hmac_key = hkdf_sha256(ikm=_MASK_KEY, salt=_SALT, info=b"", length=32)
    hmac_input = bytes([SEED_PROTOCOL_VERSION]) + len(source).to_bytes(4, "big") + source
    return hmac.new(hmac_key, hmac_input, hashlib.sha256).digest()


def _mutated_derive_little_endian_namespace_length(namespace: str, source: bytes) -> bytes:
    """Section 5.4 mutation: "change one length prefix from big-endian to
    little-endian." Everything else reproduces `derive` exactly; only the
    namespace length prefix's byte order is flipped."""
    hmac_key = hkdf_sha256(ikm=_MASK_KEY, salt=_SALT, info=namespace.encode("utf-8"), length=32)
    namespace_bytes = namespace.encode("utf-8")
    hmac_input = (
        bytes([SEED_PROTOCOL_VERSION])
        + len(namespace_bytes).to_bytes(4, "little")  # mutated: should be "big"
        + namespace_bytes
        + len(source).to_bytes(4, "big")
        + source
    )
    return hmac.new(hmac_key, hmac_input, hashlib.sha256).digest()


def _mutated_hash_array_wrong_truncate_width(
    values: list[Any], *, namespace: str, truncate: int
) -> pa.Array:
    """Section 5.4 mutation: "truncate at the wrong width." Applies
    `truncate + 1` characters instead of the requested width, reproducing
    the rest of `hash_array` exactly."""
    out: list[str | None] = []
    for value in values:
        if value is None:
            out.append(None)
            continue
        token = (
            hmac.new(
                hkdf_sha256(ikm=_MASK_KEY, salt=_SALT, info=namespace.encode("utf-8"), length=32),
                bytes([SEED_PROTOCOL_VERSION])
                + len(namespace.encode("utf-8")).to_bytes(4, "big")
                + namespace.encode("utf-8")
                + len(canonicalize_derive_source(value)).to_bytes(4, "big")
                + canonicalize_derive_source(value),
                hashlib.sha256,
            )
            .digest()
            .hex()
        )
        out.append(token[: truncate + 1])  # mutated: off by one
    return pa.array(out, type=pa.string())


class TestHarnessMutations:
    """Each mutation MUST make `_assert_matches_reference` raise. A parity
    suite that stayed green against any of these would be a broken net."""

    def test_dropped_namespace_binding_is_caught(self) -> None:
        source_array = pa.array(["alice"])
        source = canonicalize_derive_source("alice")
        mutated_token = _mutated_derive_namespace_dropped(_NAMESPACE, source).hex()
        mutated_array = pa.array([mutated_token], type=pa.string())

        with pytest.raises(AssertionError):
            _assert_matches_reference(
                source_array, mutated_array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
            )

    def test_little_endian_length_prefix_is_caught(self) -> None:
        source_array = pa.array(["alice"])
        source = canonicalize_derive_source("alice")
        mutated_token = _mutated_derive_little_endian_namespace_length(_NAMESPACE, source).hex()
        mutated_array = pa.array([mutated_token], type=pa.string())

        with pytest.raises(AssertionError):
            _assert_matches_reference(
                source_array, mutated_array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
            )

    def test_wrong_truncate_width_is_caught(self) -> None:
        source_array = pa.array(["alice", "bob", None])
        mutated_array = _mutated_hash_array_wrong_truncate_width(
            ["alice", "bob", None], namespace=_NAMESPACE, truncate=16
        )

        with pytest.raises(AssertionError):
            _assert_matches_reference(
                source_array, mutated_array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=16
            )

    def test_harness_still_passes_on_a_correct_candidate(self) -> None:
        """Belt-and-suspenders: the harness is strict (the three tests
        above), not merely broken in a way that always raises. A genuinely
        correct candidate must still pass."""
        array = pa.array(["alice", "bob", None])
        correct = native_keyed_hash(array, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None)
        _assert_matches_reference(
            array, correct, mask_key=_MASK_KEY, namespace=_NAMESPACE, truncate=None
        )
