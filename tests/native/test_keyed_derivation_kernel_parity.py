"""Byte-parity between the compiled Rust `KeyedDerivationKernel` and the LIVE Python
`reference_keyed_derivation()`.

This grades the compiled `decoy_engine_native._kernel.derive_batch` against the shipped
Python behavior directly (not only against the static JSON fixture, which the Rust `cargo
test` suite already covers): every `HASH_KAT` vector, a generated corpus spanning every
admitted Arrow type, and the shared `keyed_derivation_kat.json` fixture re-run through the
LIVE reference kernel rather than its own pinned `expected_output`, so a Python-side
regression would also be caught here.

Skipped entirely when the `decoy-engine-native` companion is not installed (the default
`.[dev]` install): building it is `maturin develop` from `decoy-engine-native/`, per its
README.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.determinism._derive import DeterminismError
from decoy_engine.execution.native._crypto_ext import HASH_KAT, reference_keyed_derivation

_COMPANION_PRESENT = importlib.util.find_spec("decoy_engine_native") is not None

pytestmark = pytest.mark.skipif(
    not _COMPANION_PRESENT,
    reason="decoy-engine-native companion not installed (maturin develop); "
    "the companion-present CI job covers this",
)

_REFERENCE = reference_keyed_derivation()
_KAT_FIXTURE_PATH = (
    Path(__file__).parents[2] / "decoy-engine-native" / "vectors" / "keyed_derivation_kat.json"
)


def _compiled_kernel() -> Any:
    import decoy_engine_native._kernel as kernel

    return kernel


def test_every_hash_kat_vector_matches_the_compiled_kernel() -> None:
    kernel = _compiled_kernel()
    for vector in HASH_KAT:
        array = pa.array([vector.value])
        got = kernel.derive_batch(
            array,
            mask_key=vector.mask_key,
            namespace=vector.namespace,
            truncate=vector.truncate,
        )
        assert got.to_pylist() == [vector.expected], vector


@pytest.mark.parametrize(
    "arrow_type,values",
    [
        (pa.string(), ["alice", "bob", None, "", "café", "café"]),
        (pa.large_string(), ["alice", None, "carol"]),
        (pa.int8(), [-128, -1, 0, 1, 127, None]),
        (pa.uint8(), [0, 1, 255, None]),
        (pa.int16(), [-32768, -1, 0, 1, 32767, None]),
        (pa.uint16(), [0, 1, 65535, None]),
        (pa.int32(), [-2147483648, -1, 0, 1, 2147483647, None]),
        (pa.uint32(), [0, 1, 4294967295, None]),
        (pa.int64(), [-(2**63), -1, 0, 1, 2**63 - 1, None]),
        (pa.uint64(), [0, 1, 2**64 - 1, None]),
        (pa.bool_(), [True, False, None]),
    ],
)
def test_compiled_kernel_matches_live_reference_over_admitted_types(
    arrow_type: pa.DataType, values: list[Any]
) -> None:
    kernel = _compiled_kernel()
    array = pa.array(values, type=arrow_type)
    mask_key = bytes(range(32))
    namespace = "parity.generated_corpus"

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=None
    )
    compiled_out = kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=None)
    assert compiled_out.to_pylist() == reference_out.to_pylist()

    # Truncated form too: a separate call path in both kernels.
    reference_trunc = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=16
    )
    compiled_trunc = kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=16)
    assert compiled_trunc.to_pylist() == reference_trunc.to_pylist()


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
@pytest.mark.parametrize("tz", ["UTC", "America/New_York"])
def test_compiled_kernel_matches_live_reference_over_timestamps(unit: str, tz: str) -> None:
    import pandas as pd

    kernel = _compiled_kernel()
    dtype = pa.timestamp(unit, tz=tz)
    raw = [
        "2020-01-01T12:30:45",
        "2020-06-15T08:00:00.123456789" if unit == "ns" else "2020-06-15T08:00:00.123456",
        "1969-12-31T23:59:59.999999",
        None,
    ]
    values = [None if v is None else pd.Timestamp(v, tz="UTC") for v in raw]
    array = pa.array(values, type=dtype)
    mask_key = bytes(range(32))
    namespace = "parity.timestamps"

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=None
    )
    compiled_out = kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=None)
    assert compiled_out.to_pylist() == reference_out.to_pylist()


def test_compiled_kernel_rejects_float_array_with_mixed_object_not_native() -> None:
    kernel = _compiled_kernel()
    array = pa.array([1.0, 2.0, None], type=pa.float64())
    with pytest.raises(ValueError, match="mixed_object_not_native"):
        kernel.derive_batch(
            array, mask_key=bytes(range(32)), namespace="parity.reject", truncate=None
        )


def test_compiled_kernel_rejects_naive_timestamp_with_mixed_object_not_native() -> None:
    import datetime

    kernel = _compiled_kernel()
    array = pa.array([datetime.datetime(2020, 1, 1)], type=pa.timestamp("us", tz=None))
    with pytest.raises(ValueError, match="mixed_object_not_native"):
        kernel.derive_batch(
            array, mask_key=bytes(range(32)), namespace="parity.reject", truncate=None
        )


def test_compiled_kernel_requires_a_mask_key() -> None:
    kernel = _compiled_kernel()
    array = pa.array(["alice"])
    with pytest.raises(ValueError, match="mask_key_required"):
        kernel.derive_batch(array, mask_key=None, namespace="parity.reject", truncate=None)
    with pytest.raises(ValueError, match="mask_key_required"):
        kernel.derive_batch(array, mask_key=b"", namespace="parity.reject", truncate=None)


@pytest.mark.parametrize("wrong_length", [16, 20, 64])
def test_off_length_mask_key_fails_closed_in_both_kernels(wrong_length: int) -> None:
    """`decoy_engine.determinism._derive._SEED_LENGTHS` admits only 8 (job_seed) and 32
    (mask_key) bytes; every other length is a caller bug the reference rejects with
    `DeterminismError(code="seed_wrong_length")` BEFORE any output. The compiled kernel must
    fail the same way, not silently derive a token the reference would have refused to
    produce."""
    kernel = _compiled_kernel()
    array = pa.array(["alice"])
    mask_key = bytes(wrong_length)

    with pytest.raises(DeterminismError) as ref_exc:
        _REFERENCE.derive_batch(
            array, mask_key=mask_key, namespace="parity.wrong_length", truncate=None
        )
    assert ref_exc.value.code == "seed_wrong_length"

    with pytest.raises(ValueError, match="seed_wrong_length"):
        kernel.derive_batch(
            array, mask_key=mask_key, namespace="parity.wrong_length", truncate=None
        )


def test_eight_byte_job_seed_key_matches_the_live_reference() -> None:
    """The 8-byte `job_seed` path (the no-secret pre-DE-02 default) must keep working
    byte-for-byte: the seed-length guard rejects wrong LENGTHS, not the job_seed length
    itself."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(8))
    namespace = "parity.job_seed"

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=None
    )
    compiled_out = kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=None)
    assert compiled_out.to_pylist() == reference_out.to_pylist()
    assert compiled_out.to_pylist()[0] is not None  # sanity: this path actually produced tokens


@pytest.mark.parametrize(
    "mask_key,namespace",
    [
        pytest.param(bytes(20), "parity.wrong_length_key", id="wrong_length_key"),
        pytest.param(bytes(range(32)), "", id="empty_namespace"),
    ],
)
@pytest.mark.parametrize(
    "array_factory",
    [
        pytest.param(lambda: pa.array([], type=pa.string()), id="empty"),
        pytest.param(lambda: pa.array([None, None, None], type=pa.string()), id="all_null"),
    ],
)
def test_empty_or_all_null_batch_succeeds_despite_a_bad_key_or_namespace(
    array_factory, mask_key: bytes, namespace: str
) -> None:
    """`derive()` (Rust) / `derive` (Python) validates seed length and namespace only when a
    non-null row actually reaches it. `_ReferenceKeyedDerivation.derive_batch` never calls
    `derive()` for an empty or all-null batch, so a wrong-length key or an empty namespace must
    NOT raise in that case."""
    kernel = _compiled_kernel()
    array = array_factory()

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=None
    )
    compiled_out = kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=None)
    assert compiled_out.to_pylist() == reference_out.to_pylist()
    assert all(v is None for v in compiled_out.to_pylist())


def test_non_null_row_with_empty_namespace_fails_closed_in_both_kernels() -> None:
    """The mirror of the wrong-length-key case, for the OTHER value `derive()` validates
    per-row: an empty namespace with at least one non-null row must fail closed in both
    kernels, with the reference's own `namespace_empty` code."""
    kernel = _compiled_kernel()
    array = pa.array([None, "alice", None])
    mask_key = bytes(range(32))

    with pytest.raises(DeterminismError) as ref_exc:
        _REFERENCE.derive_batch(array, mask_key=mask_key, namespace="", truncate=None)
    assert ref_exc.value.code == "namespace_empty"

    with pytest.raises(ValueError, match="namespace_empty"):
        kernel.derive_batch(array, mask_key=mask_key, namespace="", truncate=None)


@pytest.mark.parametrize("truncate", [-1, -63, -64, -65, -1_000_000, 1_000_000])
def test_negative_and_large_truncate_matches_the_live_reference(truncate: int) -> None:
    """The reference does `token[:truncate]`, Python slicing: a negative `truncate` counts
    back from the end of the 64-character hex string (clamping at zero, never erroring), and
    one far past either end is a no-op / empties the string, also never erroring. The compiled
    kernel's `truncate` is a signed `isize` specifically so it can reproduce this exactly."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.negative_truncate"

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=truncate
    )
    compiled_out = kernel.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=truncate
    )
    assert compiled_out.to_pylist() == reference_out.to_pylist()


@pytest.mark.parametrize(
    "truncate",
    [2**63, 2**64, 10**100, -(2**63), -(2**64), -(10**100)],
)
def test_huge_magnitude_truncate_matches_the_live_reference(truncate: int) -> None:
    """Python slice indices are arbitrary precision; `isize` is not. `token[:truncate]` for a
    `truncate` this large only ever cares about its SIGN relative to the 64-character token
    (huge positive keeps everything, huge negative empties it), so the compiled kernel clamps
    by sign at the FFI boundary instead of raising `OverflowError` converting a value this big
    to a machine word."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.huge_truncate"

    reference_out = _REFERENCE.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=truncate
    )
    compiled_out = kernel.derive_batch(
        array, mask_key=mask_key, namespace=namespace, truncate=truncate
    )
    assert compiled_out.to_pylist() == reference_out.to_pylist()


def test_hostile_index_object_is_refused_by_both_kernels() -> None:
    """A `truncate` whose `__index__` raises `OverflowError` and whose `__lt__` always returns
    `False` is the adversarial case the sign-only clamp must not fall for: naively catching
    `OverflowError` and then checking `value < 0` would call this object's own `__lt__`,
    which lies, making the native kernel treat it as `+isize::MAX` (a full token) instead of
    refusing it the way the reference's own `token[:truncate]` slicing does (its slicing calls
    `__index__` first, which raises). The native kernel must require a genuine `int` BEFORE any
    sign check, so this object never reaches `.lt()` at all."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.hostile_truncate"

    class Hostile:
        def __index__(self) -> int:
            raise OverflowError("not a real int")

        def __lt__(self, other: object) -> bool:
            return False

    hostile = Hostile()

    # The reference's own slicing raises for this object (its exact exception type is an
    # implementation detail of CPython's slice machinery, not part of the contract); only that
    # it refuses matters here.
    with pytest.raises(Exception):
        _REFERENCE.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=hostile)

    with pytest.raises(TypeError, match="truncate must be an int or None"):
        kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=hostile)


@pytest.mark.parametrize("bad_truncate", ["5", 1.5, [1, 2], {}])
def test_wrong_type_truncate_is_refused_by_both_kernels(bad_truncate: object) -> None:
    """A `truncate` that is not `None` or an `int` (a string, a float, a list, ...) must fail
    closed in both kernels: the reference's `token[:truncate]` raises `TypeError` for these,
    and the native kernel must refuse them too rather than silently coercing or clamping."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.wrong_type_truncate"

    with pytest.raises(TypeError):
        _REFERENCE.derive_batch(
            array, mask_key=mask_key, namespace=namespace, truncate=bad_truncate
        )

    with pytest.raises(TypeError, match="truncate must be an int or None"):
        kernel.derive_batch(array, mask_key=mask_key, namespace=namespace, truncate=bad_truncate)


def test_bool_truncate_matches_the_live_reference() -> None:
    """`bool` is an `int` subtype in Python (and the reference's `token[:truncate]` accepts it
    fine); the native kernel's genuine-`int` gate must accept it too, not just reject it as
    "not really an int"."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.bool_truncate"

    for truncate in (True, False):
        reference_out = _REFERENCE.derive_batch(
            array, mask_key=mask_key, namespace=namespace, truncate=truncate
        )
        compiled_out = kernel.derive_batch(
            array, mask_key=mask_key, namespace=namespace, truncate=truncate
        )
        assert compiled_out.to_pylist() == reference_out.to_pylist()


def test_int_subclass_with_hostile_lt_matches_the_live_reference() -> None:
    """`isinstance(value, int)` is true for an `int` subclass too, so the native kernel's
    genuine-`int` gate alone is not enough to make a Python-level sign check safe: a subclass
    can override `__lt__` to lie about the sign of the value it wraps. The native kernel must
    read the C-level integer value directly (via `PyNumber_AsSsize_t`, never `__lt__`) so the
    override has no effect on the result; this asserts both that the result matches the
    reference AND that the override was never invoked."""
    kernel = _compiled_kernel()
    array = pa.array(["alice", "bob", None])
    mask_key = bytes(range(32))
    namespace = "parity.evil_int_subclass_truncate"

    lt_calls: list[tuple[object, object]] = []

    class Evil(int):
        def __lt__(self, other: object) -> bool:
            lt_calls.append((self, other))
            return False

    for raw in (-(10**100), 10**100, -1, 5, 0):
        lt_calls.clear()
        evil = Evil(raw)

        reference_out = _REFERENCE.derive_batch(
            array, mask_key=mask_key, namespace=namespace, truncate=raw
        )
        compiled_out = kernel.derive_batch(
            array, mask_key=mask_key, namespace=namespace, truncate=evil
        )
        assert compiled_out.to_pylist() == reference_out.to_pylist(), raw
        assert lt_calls == [], (
            f"__lt__ was invoked during native truncate conversion for {raw}: {lt_calls}"
        )


def _arrow_type_from_fixture(descriptor: dict[str, Any]) -> pa.DataType:
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
def test_compiled_kernel_matches_live_reference_over_the_shared_kat_fixture() -> None:
    """Re-run the shared fixture's cases through the LIVE reference (not its own pinned
    `expected_output`): this is what catches a reference-side regression the static fixture
    alone cannot, since the fixture's own values came from the reference at generation time."""
    kernel = _compiled_kernel()
    fixture = json.loads(_KAT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["cases"], "the shared fixture must not be empty"

    for case in fixture["cases"]:
        dtype = _arrow_type_from_fixture(case["arrow_type"])
        native_values = [_logical_to_native(case["arrow_type"], v) for v in case["logical_values"]]
        array = pa.array(native_values, type=dtype)
        mask_key = bytes.fromhex(case["mask_key_hex"])

        reference_out = _REFERENCE.derive_batch(
            array, mask_key=mask_key, namespace=case["namespace"], truncate=case["truncate"]
        )
        compiled_out = kernel.derive_batch(
            array, mask_key=mask_key, namespace=case["namespace"], truncate=case["truncate"]
        )
        assert compiled_out.to_pylist() == reference_out.to_pylist(), case["name"]
        assert compiled_out.to_pylist() == case["expected_output"], case["name"]
