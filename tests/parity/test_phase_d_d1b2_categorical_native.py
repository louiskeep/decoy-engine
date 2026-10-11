"""Phase D / D1b-ii: the REAL compiled companion on the out-of-core categorical route.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the declined-path
and reference-double suites cover the same routing. These tests run the real index kernel and
check what only it can show: byte parity with the pinned baseline, the Python reference and the
pandas oracle for text both Unicode versions agree on, both the uniform and the weighted (CDF
searchsorted) selection, the accepted post-Unicode-version divergence (characterized in a test
of its own, apart from a locked native selection), and the guard that keeps the normalization
tables a reviewed decision.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import PYTHON_SCALAR, RUST_COMPANION
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.out_of_core._hash_run import OocHashRun
from decoy_engine.execution.out_of_core._mask import mask_batch
from decoy_engine.execution.out_of_core._mask_group_b import categorical_array
from decoy_engine.keyprovider import SecretKeyProvider
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import (
    JOB_SEED,
    SECRET,
    chain_graph,
    chain_plan,
    chain_sources,
    run_chain,
)
from tests.parity._d1b2_categorical_data import (
    CATEGORIES,
    EXPECTED,
    WEIGHTS,
    categorical_seed,
    source_column,
)
from tests.parity._kernel_witness import ExpectedMask, KernelWitness
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs
from tests.parity.test_phase_d_d1a_hash_native import (
    _NORMALIZATION_CRATE,
    _locked_package,
    _vendored_table_version,
)


def _companion_loads() -> bool:
    try:
        load_compiled_index_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

_MODES = ["resident", "sink-batch", "sink-reorder"]
KEY = "out_of_core_categorical"
_SEEDS = (
    ("c_uniform", categorical_seed()),
    ("c_weighted", categorical_seed(weights=WEIGHTS)),
    ("c_other_ns", categorical_seed(namespace="ns_other")),
)
# `a` + U+0897 + U+0316: the post-version mark reorders under Unicode >= 16, not under 14.
_SENSITIVE = "a̖ࢗ"
# Chosen so the two canonicalizations select DIFFERENT categories: with only two categories a
# reduction can map different derived indices to the same pool slot and hide the divergence.
_DIVERGENCE_KEY = bytes(range(32))
_DIVERGENCE_NS = "n"
_DIVERGENCE_POOL = ("0", "1")


def _payload() -> pa.Table:
    return pa.table({name: source_column() for name, _ in _SEEDS})


def test_the_loaded_companion_is_abi_3() -> None:
    from decoy_engine_native import _kernel

    assert _kernel.abi_version() == "decoy-native-abi-3"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("batch_rows", [None, 7])
def test_native_output_is_the_pinned_baseline_and_is_consumed(
    mode: str, batch_rows: int | None, tmp_path: Path
) -> None:
    witness = KernelWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    expected = [ExpectedMask("t", name, "categorical_array") for name, _ in _SEEDS]
    sources = chain_sources(_payload())
    witness.assert_native_ran(sources, run.tables, expected)
    assert witness.native_attempts > 0
    for name, _ in _SEEDS:
        got = run.tables["t"].column(name)
        assert got.type == pa.string(), name
        assert got.to_pylist() == EXPECTED[name], name
    disposition = {(e["table"], e["column"]): e for e in run.result.quality_metrics[KEY]}
    assert {e["executed_backend"] for e in disposition.values()} == {RUST_COMPANION}
    assert all("reason" not in e for e in disposition.values())


def _reference(values: pa.Array, seed: Any, key: bytes = JOB_SEED) -> list[str | None]:
    cfg = dict(seed.provider_config)
    return categorical_array(
        values, job_seed=key, namespace=seed.namespace, deterministic=True, cfg=cfg
    ).to_pylist()


def test_native_equals_the_python_reference_for_shared_version_input() -> None:
    run = run_chain(_payload(), _SEEDS)
    for name, seed in _SEEDS:
        assert run.tables["t"].column(name).to_pylist() == _reference(source_column(), seed), name


def test_a_wide_weighted_pool_matches_the_python_reference_band_for_band() -> None:
    # Many uneven bands over many distinct keys so the searchsorted boundaries are exercised.
    rng = random.Random(7)
    pool = tuple(f"cat{i}" for i in range(12))
    weights = tuple(rng.choice([0.5, 1, 2, 3, 7, 11, 0.05]) for _ in pool)
    values = pa.array(
        [None if i % 17 == 0 else f"row-{rng.randrange(10**9)}-{i}" for i in range(4000)],
        pa.string(),
    )
    seed = categorical_seed(categories=pool, weights=weights)
    run = run_chain(pa.table({"c": values}), (("c", seed),), batch_rows=512)
    got = run.tables["t"].column("c").to_pylist()
    assert got == _reference(values, seed)
    assert {v for v in got if v is not None} == set(pool)
    assert [v is None for v in got] == [i % 17 == 0 for i in range(4000)]


def test_secret_backed_mask_key_matches_the_pin_and_differs_from_job_seed() -> None:
    seeds = (("c_secret", categorical_seed(weights=WEIGHTS)),)
    run = run_chain(
        pa.table({"c_secret": source_column()}), seeds, key_provider=SecretKeyProvider(SECRET)
    )
    got = run.tables["t"].column("c_secret").to_pylist()
    assert got == EXPECTED["c_secret"]
    assert got != EXPECTED["c_weighted"]


def test_composed_and_decomposed_text_agree_on_both_paths() -> None:
    table = pa.table({"s": pa.array(["é", "é"], pa.string())})
    seed = categorical_seed()
    native = run_chain(table, (("s", seed),)).tables["t"].column("s").to_pylist()
    assert native == _reference(table.column("s").combine_chunks(), seed)
    assert native[0] == native[1]


def test_native_matches_the_pandas_oracle_strictly_uniform_and_weighted() -> None:
    # A payload column on the FK-chain table `t` (which also carries a parent key and an FK).
    # Only the frozen D0 field_metadata gap may differ from the oracle.
    payload = pa.table(
        {
            "u": pa.array(["alice", "Bob", "", "héllo", None, "x"], pa.string()),
            "w": pa.array(["alice", "Bob", "", "héllo", None, "x"], pa.string()),
        }
    )
    seeds = (("u", categorical_seed()), ("w", categorical_seed(weights=WEIGHTS)))
    ooc = run_chain(payload, seeds).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    for table in ("up", "t", "child"):
        report = sc.compare_strict(oracle[table], ooc[table])
        assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}, table
    for column in ("u", "w"):
        assert ooc["t"].column(column).to_pylist() == oracle["t"].column(column).to_pylist()


def test_an_all_null_column_reconciles_to_the_oracle_null_type() -> None:
    payload = pa.table({"u": pa.array([None, None, None, None], pa.string())})
    seeds = (("u", categorical_seed()),)
    ooc = run_chain(payload, seeds, batch_rows=2).tables
    oracle = _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs
    assert ooc["t"].column("u").type == oracle["t"].column("u").type == pa.null()
    report = sc.compare_strict(oracle["t"], ooc["t"])
    assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}


# --- Declined columns stay Python even with the real companion present ---------------------


def test_a_non_string_source_stays_python_with_the_real_companion() -> None:
    from tests.parity._d1b2_categorical_data import EXPECTED_NON_STRING, non_string_sources

    for name, column in non_string_sources().items():
        seeds = ((name, categorical_seed(categories=CATEGORIES[:3])),)
        run = run_chain(pa.table({name: column}), seeds)
        (entry,) = (e for e in run.result.quality_metrics[KEY] if e["column"] == name)
        assert entry["executed_backend"] == PYTHON_SCALAR, name
        assert entry["reason"] == f"categorical_source_not_string:{name}:{column.type}"
        assert run.tables["t"].column(name).to_pylist() == EXPECTED_NON_STRING[name], name


def test_an_abi_incompatible_real_companion_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine_native import _kernel

    monkeypatch.setattr(_kernel, "abi_version", lambda: "decoy-native-abi-0")
    run = run_chain(_payload().select(["c_uniform"]), (_SEEDS[0],))
    entries = run.result.quality_metrics[KEY]
    assert {(e["executed_backend"], e["reason"]) for e in entries} == {
        (PYTHON_SCALAR, "index_extension_unavailable")
    }
    assert run.tables["t"].column("c_uniform").to_pylist() == EXPECTED["c_uniform"]


def test_the_loader_self_test_is_not_observed_as_a_masking_call() -> None:
    witness = KernelWitness()
    with witness.observe():
        load_compiled_index_kernel()
    assert witness.observations == []
    assert witness.native_attempts == 0


# --- The accepted Unicode-version divergence, and the guard that keeps it reviewed ---------


def _select_through_the_route(values: list[str], *, native: bool) -> list[str | None]:
    """Mask `values` through `mask_batch` with the divergence key, on the native or Python path."""
    seed = categorical_seed(namespace=_DIVERGENCE_NS, categories=_DIVERGENCE_POOL)
    plan = chain_plan((("c", seed),))
    batch = pa.record_batch([pa.array(values, pa.string())], names=["c"])
    table_plan = None
    if native:
        table_plan = OocHashRun().prepare_table(
            plan, "t", batch.schema, frozenset(), (), site="test"
        )
        assert table_plan.categorical is not None
        assert table_plan.categorical.decisions["c"].native
    out = mask_batch(plan, "t", batch, mask_key=_DIVERGENCE_KEY, hash_plan=table_plan)
    return out.column(0).to_pylist()


def test_native_selection_for_a_post_python_version_sequence_is_locked() -> None:
    # Native (Rust, newer tables) is canonical: this selection must not drift.
    assert _select_through_the_route([_SENSITIVE], native=True) == ["1"]


def test_post_python_version_sequence_selects_differently_from_the_python_path() -> None:
    # Accepted by Cam 2026-10-11 for hash and carried to categorical: native normalizes under
    # the newer Unicode tables, the Python path under its runtime's. Only meaningful while the
    # runtime predates Unicode 16, where U+0897 gained its combining class.
    import unicodedata

    assert tuple(int(p) for p in unicodedata.unidata_version.split(".")) < (16, 0, 0)
    native = _select_through_the_route([_SENSITIVE], native=True)
    python = _select_through_the_route([_SENSITIVE], native=False)
    assert python == ["0"]
    assert native != python
    # The divergence is the normalization, not the character: the lone codepoint and the
    # already-ordered sequence select the same category on both paths.
    agree = ["ࢗ", "a̖ࢗ"]
    assert _select_through_the_route(agree, native=True) == _select_through_the_route(
        agree, native=False
    )


def test_the_resolved_normalization_crate_and_its_unicode_table_version_are_pinned() -> None:
    locked = _locked_package("unicode-normalization")
    assert locked["version"] in _NORMALIZATION_CRATE, (
        "unicode-normalization moved: its Unicode tables may have changed. Record the new "
        "table version in the D1a hash guard after reviewing the effect on native hash tokens "
        "and categorical selections."
    )
    table_version, checksum = _NORMALIZATION_CRATE[locked["version"]]
    assert locked["checksum"] == checksum
    assert table_version == (17, 0, 0)


def test_the_recorded_table_version_matches_the_vendored_crate_when_present() -> None:
    vendored: Any = _vendored_table_version("0.1.25")
    if vendored is None:
        pytest.skip("the crate source is not in the local cargo registry")
    assert vendored == _NORMALIZATION_CRATE["0.1.25"][0]
