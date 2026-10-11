"""Phase D / D1a: the REAL compiled companion on the out-of-core hash route.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the
declined-path suites cover the same routing. These tests run the real kernel and
check what only it can show: byte parity with the Python reference and the pandas
oracle for text both Unicode versions agree on, the accepted post-Unicode-version
divergence (characterized separately from a locked native token), and a guard that
any change to the normalization tables is a reviewed decision.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.kernel import hash_array
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import (
    EXPECTED_TOKENS,
    JOB_SEED,
    NS,
    SECRET,
    chain_graph,
    chain_plan,
    chain_sources,
    column_seed,
    hash_seeds,
    run_chain,
    source_columns,
)
from tests.parity._kernel_witness import ExpectedMask, KernelWitness
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs


def _companion_loads() -> bool:
    try:
        load_compiled_crypto_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

_MODES = ["resident", "sink-batch", "sink-reorder"]
_TYPED = list(source_columns())
_ALL = [*_TYPED, "c_trunc", "c_other_ns"]
# `a` + U+0897 + U+0316: the post-version mark reorders under Unicode >= 16, not under 14.
_SENSITIVE = "a̖ࢗ"
_SENSITIVE_NATIVE_TOKEN = "18a1e2bb3115db92ca35562b009f759d454959777614e4d9d7a00d27eb2b5ae4"
_SENSITIVE_PYTHON_14_TOKEN = "3682379fae9e7a059e7dc794535530f5a3141c2cc37c5b61be770201c7719686"


def _payload() -> pa.Table:
    cols = source_columns()
    return pa.table({**cols, "c_trunc": cols["c_utf8"], "c_other_ns": cols["c_utf8"]})


_SEEDS = (
    *hash_seeds(_TYPED),
    ("c_trunc", column_seed(provider_config=(("truncate", 12),))),
    ("c_other_ns", column_seed(namespace="ns_other")),
)


def test_the_loaded_companion_is_abi_3() -> None:
    from decoy_engine_native import _kernel

    assert _kernel.abi_version() == "decoy-native-abi-3"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 3])
def test_native_output_is_the_pinned_baseline_and_is_consumed(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    expected = [ExpectedMask("t", c, "hash_array") for c in (*_ALL, "hk")]
    sources = chain_sources(_payload())
    witness.assert_native_ran(sources, run.tables, expected)
    assert witness.native_attempts > 0
    for name in (*_ALL, "hk"):
        assert run.tables["t"].column(name).to_pylist() == EXPECTED_TOKENS[name], name
    disposition = {
        (e["table"], e["column"]): e for e in run.result.quality_metrics["out_of_core_hash"]
    }
    assert {e["executed_backend"] for e in disposition.values()} == {RUST_COMPANION}
    assert all("reason" not in e for e in disposition.values())


def test_native_equals_the_python_reference_for_shared_version_input() -> None:
    run = run_chain(_payload(), _SEEDS)
    for name in _TYPED:
        want = hash_array(source_columns()[name], seed=JOB_SEED, namespace=NS).to_pylist()
        assert run.tables["t"].column(name).to_pylist() == want, name


def test_secret_backed_mask_key_matches_the_pin_and_differs_from_job_seed() -> None:
    table = pa.table({"c_utf8_secret": source_columns()["c_utf8"]})
    run = run_chain(
        table, (("c_utf8_secret", column_seed()),), key_provider=SecretKeyProvider(SECRET)
    )
    got = run.tables["t"].column("c_utf8_secret").to_pylist()
    assert got == EXPECTED_TOKENS["c_utf8_secret"]
    assert got != EXPECTED_TOKENS["c_utf8"]


def test_composed_and_decomposed_text_agree_on_both_paths() -> None:
    table = pa.table({"s": pa.array(["é", "é"], pa.string())})
    native = run_chain(table, (("s", column_seed()),)).tables["t"].column("s").to_pylist()
    python = hash_array(table.column("s"), seed=JOB_SEED, namespace=NS).to_pylist()
    assert native == python
    assert native[0] == native[1]


def test_native_hash_matches_the_pandas_oracle_strictly() -> None:
    # A parent-key column (hk), an FK-chain payload and non-FK payloads, no nulls in the
    # integer columns (the oracle rejects those). Only the frozen D0 gap may differ.
    payload = pa.table(
        {
            "s": pa.array(["alice", "Bob", "", "héllo", None], pa.string()),
            "i64": pa.array([-(2**63), -1, 0, 1, 2**63 - 1], pa.int64()),
            "i32": pa.array([-(2**31), -1, 0, 1, 2**31 - 1], pa.int32()),
            "u8": pa.array([0, 1, 127, 128, 255], pa.uint8()),
            "u64": pa.array([0, 1, 2**32, 2**63, 2**64 - 1], pa.uint64()),
            "b": pa.array([True, False, True, False, True], pa.bool_()),
        }
    )
    seeds = hash_seeds(payload.column_names)
    ooc = run_chain(payload, seeds).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    for table in ("up", "t", "child"):
        report = sc.compare_strict(oracle[table], ooc[table])
        assert {dim for dim, agree in report.items() if not agree} == {"field_metadata"}, table
    assert ooc["t"].column("hk").to_pylist() == oracle["t"].column("hk").to_pylist()


# --- Declined columns stay Python even with the real companion present --------------------


def test_tz_aware_timestamp_stays_python_with_the_real_companion() -> None:
    column = pa.array(
        [dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc), None], pa.timestamp("us", tz="UTC")
    )
    run = run_chain(pa.table({"c": column}), (("c", column_seed()),))
    entry = next(e for e in run.result.quality_metrics["out_of_core_hash"] if e["column"] == "c")
    assert entry["executed_backend"] == PYTHON_SCALAR
    assert entry["reason"] == "hash_input_type_tz_timestamp_deferred:c"
    assert (
        run.tables["t"].column("c").to_pylist()
        == hash_array(column, seed=JOB_SEED, namespace=NS).to_pylist()
    )


def test_an_abi_incompatible_real_companion_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine_native import _kernel

    monkeypatch.setattr(_kernel, "abi_version", lambda: "decoy-native-abi-0")
    run = run_chain(_payload().select(["c_utf8"]), hash_seeds(["c_utf8"]))
    entries = run.result.quality_metrics["out_of_core_hash"]
    assert {(e["executed_backend"], e["reason"]) for e in entries} == {
        (PYTHON_SCALAR, "crypto_extension_unavailable")
    }
    assert run.tables["t"].column("c_utf8").to_pylist() == EXPECTED_TOKENS["c_utf8"]


def test_the_loader_self_test_is_not_observed_as_a_masking_call() -> None:
    witness = KernelWitness()
    with witness.observe():
        load_compiled_crypto_kernel()
    assert witness.observations == []
    assert witness.native_attempts == 0


# --- The accepted Unicode-version divergence, and the guard that keeps it reviewed --------


def test_native_token_for_a_post_python_version_sequence_is_locked() -> None:
    table = pa.table({"s": pa.array([_SENSITIVE, "a̖ࢗ"], pa.string())})
    got = run_chain(table, (("s", column_seed()),)).tables["t"].column("s").to_pylist()
    assert got == [_SENSITIVE_NATIVE_TOKEN, _SENSITIVE_NATIVE_TOKEN]


def test_post_python_version_sequence_differs_from_the_python_path() -> None:
    # Accepted by Cam 2026-10-11: native (Rust, newer tables) is canonical; the Python path
    # normalizes under its runtime's older Unicode version. Only meaningful while the runtime
    # predates Unicode 16, where U+0897 gained its combining class.
    assert tuple(int(p) for p in unicodedata.unidata_version.split(".")) < (16, 0, 0)
    table = pa.table({"s": pa.array([_SENSITIVE], pa.string())})
    native = run_chain(table, (("s", column_seed()),)).tables["t"].column("s").to_pylist()
    python = hash_array(table.column("s"), seed=JOB_SEED, namespace=NS).to_pylist()
    assert native != python
    assert python == [_SENSITIVE_PYTHON_14_TOKEN] or unicodedata.unidata_version != "14.0.0"
    # The divergence is the normalization, not the character: the lone codepoint, and the
    # already-ordered sequence, agree on both paths.
    agree = pa.table({"s": pa.array(["ࢗ", "a̖ࢗ"], pa.string())})
    got = run_chain(agree, (("s", column_seed()),)).tables["t"].column("s").to_pylist()
    assert got == hash_array(agree.column("s"), seed=JOB_SEED, namespace=NS).to_pylist()


# `unicode-normalization` -> (Unicode table version it carries, registry checksum). A
# `cargo update` that moves the crate changes the tables, and with them re-identification
# output for newly assigned characters, so the bump must be a deliberate edit of this row.
_NORMALIZATION_CRATE: dict[str, tuple[tuple[int, int, int], str]] = {
    "0.1.25": ((17, 0, 0), "5fd4f6878c9cb28d874b009da9e8d183b5abc80117c40bbd187a1fde336be6e8"),
}
_NATIVE_DIR = Path(__file__).parents[2] / "decoy-engine-native"


def _locked_package(name: str) -> dict[str, str]:
    text = (_NATIVE_DIR / "Cargo.lock").read_text()
    for block in text.split("[[package]]"):
        if re.search(rf'^name = "{re.escape(name)}"$', block, re.M):
            return dict(re.findall(r'^(version|checksum) = "([^"]+)"$', block, re.M))
    raise AssertionError(f"{name} is not in Cargo.lock")


def test_the_resolved_normalization_crate_and_its_unicode_table_version_are_pinned() -> None:
    locked = _locked_package("unicode-normalization")
    assert locked["version"] in _NORMALIZATION_CRATE, (
        "unicode-normalization moved: its Unicode tables may have changed. Record the new "
        "table version here after reviewing the effect on native hash tokens."
    )
    table_version, checksum = _NORMALIZATION_CRATE[locked["version"]]
    assert locked["checksum"] == checksum
    assert table_version == (17, 0, 0)


def test_the_normalization_dependency_is_not_loosened_beyond_the_lock() -> None:
    manifest = (_NATIVE_DIR / "Cargo.toml").read_text()
    assert re.search(r'^unicode-normalization = "0\.1\.25"$', manifest, re.M)


def _vendored_table_version(version: str) -> tuple[int, int, int] | None:
    for tables in Path.home().glob(
        f".cargo/registry/src/*/unicode-normalization-{version}/src/tables.rs"
    ):
        match = re.search(
            r"UNICODE_VERSION: \(u8, u8, u8\) = \((\d+), (\d+), (\d+)\)", tables.read_text()
        )
        if match:
            return (int(match[1]), int(match[2]), int(match[3]))
    return None


def test_the_recorded_table_version_matches_the_vendored_crate_when_present() -> None:
    vendored: Any = _vendored_table_version("0.1.25")
    if vendored is None:
        pytest.skip("the crate source is not in the local cargo registry")
    assert vendored == _NORMALIZATION_CRATE["0.1.25"][0]
