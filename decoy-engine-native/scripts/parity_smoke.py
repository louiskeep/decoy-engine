"""Post-install parity + ABI smoke test for a built companion wheel.

Task 3.1 (supported production matrix): one script, reused by every wheel job in
`native-companion.yml` (Linux x86-64 native, Linux ARM64 under QEMU, Windows x86-64,
macOS arm64), so all twelve wheel rows (four targets x cp310-cp312) run the identical
check rather than four hand-copied variants drifting apart. Assumes the wheel and
`pyarrow>=14` are already installed; this script installs nothing itself.

Checks, in order:
1. `abi_version()` equals the core's pinned `_EXPECTED_ABI_VERSION`
   (`decoy_engine.execution.native._crypto_ext._EXPECTED_ABI_VERSION`). A wheel built
   from a mismatched crate revision fails here before either KAT runs.
2. The hash KAT: `derive_batch(['alice'], mask_key=bytes(range(32)),
   namespace='people.ssn')` reproduces the frozen keyed-derivation vector, with the
   Arrow return type pinned to `string` (not `large_string` or anything else the
   caller would have to special-case).
3. The index KAT: `derive_index_batch(['alice'], mask_key=bytes(range(32)),
   namespace='pool.city', pool_size=97)` reproduces the frozen pool-index vector, with
   the Arrow return type pinned to `uint64`.

Exits 0 and prints one OK line per check on success; exits 1 with the failing check's
message on first failure (a KAT wheel is unfit to ship, so this does not try to
collect every failure before reporting). Checks use explicit `if not ...: raise
SystemExit(...)` rather than `assert`: every wheel job runs this under plain
`python` (no -O) today, but `assert` strips under `-O` and would then make the smoke
exit 0 having verified nothing -- the same failure mode fixed in the redact/truncate
kernel dispatchers.
"""

from __future__ import annotations

import sys

_EXPECTED_ABI_VERSION = "decoy-native-abi-2"
_MASK_KEY = bytes(range(32))

_HASH_EXPECTED = [
    "398a93520101bdc8e91ad659396a2bdf262bb59224ba39bfc807e075c33ab64c",
]
_INDEX_EXPECTED = [59]


def main() -> int:
    import pyarrow as pa
    from decoy_engine_native import _kernel

    reported_abi = _kernel.abi_version()
    if reported_abi != _EXPECTED_ABI_VERSION:
        raise SystemExit(f"ABI mismatch: got {reported_abi!r}, expected {_EXPECTED_ABI_VERSION!r}")
    print(f"OK: abi_version() == {reported_abi!r}")

    hash_result = _kernel.derive_batch(
        pa.array(["alice"]),
        mask_key=_MASK_KEY,
        namespace="people.ssn",
        truncate=None,
        native_threads=1,
    )
    if hash_result.to_pylist() != _HASH_EXPECTED:
        raise SystemExit(
            f"hash KAT mismatch: got {hash_result.to_pylist()!r}, expected {_HASH_EXPECTED!r}"
        )
    if hash_result.type != pa.string():
        raise SystemExit(f"hash KAT Arrow type mismatch: got {hash_result.type!r}, expected string")
    print(f"OK: hash KAT == {_HASH_EXPECTED!r}, Arrow type == string")

    index_result = _kernel.derive_index_batch(
        pa.array(["alice"]),
        mask_key=_MASK_KEY,
        namespace="pool.city",
        pool_size=97,
        native_threads=1,
    )
    if index_result.to_pylist() != _INDEX_EXPECTED:
        raise SystemExit(
            f"index KAT mismatch: got {index_result.to_pylist()!r}, expected {_INDEX_EXPECTED!r}"
        )
    if index_result.type != pa.uint64():
        raise SystemExit(
            f"index KAT Arrow type mismatch: got {index_result.type!r}, expected uint64"
        )
    print(f"OK: index KAT == {_INDEX_EXPECTED!r}, Arrow type == uint64")

    print("parity_smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
