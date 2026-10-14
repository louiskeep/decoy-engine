"""Out-of-core fpe native slice: the REAL compiled FF1 kernel against the oracle.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the routing suite
and the companion-absent cases cover the Python path. Compared here: the OOC native route vs
the full-frame pandas oracle on the strict parity contract, vs the Python `fpe_array`
reference, across batch sizes and both drivers; fail-closed codes and first-error ordering vs
the oracle; and the fpe config variants (separators, Luhn, charset, join group, secret key).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.out_of_core._mask_group_b import fpe_array
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
from tests.parity._ooc_fpe_data import (
    BAD_DOMAIN,
    BAD_VALUE,
    DIGITS,
    KEY,
    SOURCE,
    FpeConsumedWitness,
    companion_loads,
    forbid_python_fpe,
    fpe_seed,
    hide_fpe_companion,
    install_real_fpe_kernel,
)
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

pytestmark = pytest.mark.skipif(
    not companion_loads(),
    reason="decoy-engine-native abi-3 companion not loadable; the companion-present job covers this",
)

MODES = ["resident", "sink-batch", "sink-reorder"]
_SEEDS = (("a", fpe_seed()),)


def _payload(values: list[Any] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def _reference(values: list[Any], seed: Any, key: bytes = JOB_SEED) -> list[str | None]:
    return fpe_array(
        pa.array(values, pa.string()),
        job_seed=key,
        namespace=seed.namespace,
        column="a",
        cfg=dict(seed.provider_config),
    ).to_pylist()


def _oracle_tables(payload: pa.Table, seeds: Any) -> dict[str, pa.Table]:
    return _run_oracle(chain_plan(seeds), chain_sources(payload), chain_graph()).outputs


def _assert_strict_equal_to_oracle(ooc: dict[str, pa.Table], oracle: dict[str, pa.Table]) -> None:
    for table in ("up", "t", "child"):
        report = sc.compare_strict(oracle[table], ooc[table])
        # Only the frozen D0 field_metadata gap may differ from the oracle.
        assert {dim for dim, agree in report.items() if not agree} <= {"field_metadata"}, table


def test_the_loaded_companion_is_abi_3() -> None:
    from decoy_engine_native import _kernel

    assert _kernel.abi_version() == "decoy-native-abi-3"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("batch_rows", [None, 1, 5, 7])
def test_native_output_is_the_oracle_and_the_reference_and_is_consumed(
    mode: str, batch_rows: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = install_real_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
    witness = FpeConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    witness.assert_consumed(kernel)
    got = run.tables["t"].column("a")
    assert got.type == pa.string()
    assert got.to_pylist() == _reference(SOURCE, _SEEDS[0][1])
    _assert_strict_equal_to_oracle(run.tables, _oracle_tables(_payload(), _SEEDS))
    disposition = {e["column"]: e for e in run.result.quality_metrics[KEY]}
    assert disposition["a"]["executed_backend"] == RUST_COMPANION
    assert "reason" not in disposition["a"]


def test_native_matches_the_python_route_with_the_companion_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_real_fpe_kernel(monkeypatch)
    native = run_chain(_payload(), _SEEDS, batch_rows=3).tables["t"].column("a").to_pylist()
    hide_fpe_companion(monkeypatch)
    python = run_chain(_payload(), _SEEDS, batch_rows=3).tables["t"].column("a").to_pylist()
    assert native == python


@pytest.mark.parametrize(
    "values",
    [[None, None, None, None], ["", "", ""], [None, "", None], []],
    ids=["all-null", "all-empty", "null-and-empty", "zero-rows"],
)
@pytest.mark.parametrize("batch_rows", [None, 2])
def test_null_and_empty_columns_are_string_typed_and_match_the_oracle_values(
    values: list[Any], batch_rows: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_fpe_kernel(monkeypatch)
    payload = _payload(values)
    ooc = run_chain(payload, _SEEDS, batch_rows=batch_rows).tables
    oracle = _oracle_tables(payload, _SEEDS)
    assert ooc["t"].column("a").type == pa.string()
    assert ooc["t"].column("a").to_pylist() == oracle["t"].column("a").to_pylist() == values


def test_a_secret_backed_mask_key_matches_the_python_route_and_differs_from_job_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_real_fpe_kernel(monkeypatch)
    secret = run_chain(_payload(), _SEEDS, key_provider=SecretKeyProvider(SECRET)).tables
    plain = run_chain(_payload(), _SEEDS).tables
    hide_fpe_companion(monkeypatch)
    python = run_chain(_payload(), _SEEDS, key_provider=SecretKeyProvider(SECRET)).tables
    assert secret["t"].column("a").to_pylist() == python["t"].column("a").to_pylist()
    assert secret["t"].column("a").to_pylist() != plain["t"].column("a").to_pylist()


# --- config variants: native == oracle == Python reference ---------------------------------------

_VARIANTS = {
    "digits": (DIGITS, SOURCE),
    "separators_dropped": (
        (*DIGITS, ("preserve_separators", False)),
        ["123456789", "987654321", None, "000111222"],
    ),
    "validate_luhn": (
        (*DIGITS, ("validate_luhn", True)),
        ["4111111111111111", "5555555555554444", None, "4012888888881881", "38520000023237"],
    ),
    "alphanum": ((("charset", "ALPHANUM"),), ["Abc123", "Xyz789", None, "Test42", "Data99"]),
    "alpha": ((("charset", "alpha"),), ["abcdef", "hello", None, "world", "qwerty"]),
    "custom_charset": ((("charset", "abcdef0123"),), ["abc012", "fed321", None, "0a1b2c3d"]),
    "join_group": (
        (*DIGITS, ("fpe_join_group", "grp")),
        ["123456789", "000111222", None, "099900", "420000"],
    ),
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(_VARIANTS))
def test_config_variants_are_byte_identical_to_the_oracle(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, values = _VARIANTS[name]
    seed = fpe_seed(config=config)
    kernel = install_real_fpe_kernel(monkeypatch)
    forbid_python_fpe(monkeypatch)
    run = run_chain(_payload(values), (("a", seed),), batch_rows=2, **mode_kwargs(mode, tmp_path))
    assert kernel.calls > 0
    got = run.tables["t"].column("a").to_pylist()
    assert got == _reference(values, seed)
    oracle = _oracle_tables(_payload(values), (("a", seed),))
    assert got == oracle["t"].column("a").to_pylist()
    assert got != values


def test_join_group_columns_share_a_tweak_and_a_plain_column_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_real_fpe_kernel(monkeypatch)
    values = ["123456789", "987654321", "555555555"]
    grouped = fpe_seed(config=(*DIGITS, ("fpe_join_group", "grp")))
    payload = pa.table(
        {
            "a": pa.array(values, pa.string()),
            "b": pa.array(values, pa.string()),
            "c": pa.array(values, pa.string()),
        }
    )
    seeds = (("a", grouped), ("b", grouped), ("c", fpe_seed()))
    out = run_chain(payload, seeds).tables["t"]
    assert out.column("a").to_pylist() == out.column("b").to_pylist()
    assert out.column("a").to_pylist() != out.column("c").to_pylist()
    oracle = _oracle_tables(payload, seeds)["t"]
    for name in "abc":
        assert out.column(name).to_pylist() == oracle.column(name).to_pylist()


# --- O3 / O7: fail-closed parity with the oracle, with the real kernel -----------------------------


def _ooc_code(payload: pa.Table, **kw: Any) -> str:
    with pytest.raises(StrategyError) as caught:
        run_chain(payload, _SEEDS, **kw)
    return caught.value.code


def _oracle_code(payload: pa.Table) -> str:
    with pytest.raises(StrategyError) as caught:
        _oracle_tables(payload, _SEEDS)
    return caught.value.code


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("bad", [BAD_VALUE, BAD_DOMAIN])
@pytest.mark.parametrize("batch_rows", [None, 2])
def test_an_unencryptable_value_raises_the_oracles_code(
    bad: str, batch_rows: int | None, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_fpe_kernel(monkeypatch)
    payload = _payload(["123456", "234567", bad, "345678"])
    native = _ooc_code(payload, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    assert native == _oracle_code(payload)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "ordered",
    [
        [BAD_DOMAIN, BAD_VALUE],
        [BAD_VALUE, BAD_DOMAIN],
        ["123456", BAD_VALUE, BAD_DOMAIN, BAD_VALUE],
        ["123456", BAD_DOMAIN, BAD_VALUE, BAD_DOMAIN],
    ],
    ids=["domain-value", "value-domain", "ok-value-domain-value", "ok-domain-value-domain"],
)
def test_the_first_failing_row_decides_the_code_like_the_oracle(
    ordered: list[str], mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_real_fpe_kernel(monkeypatch)
    payload = _payload(ordered)  # one batch holds every failing row
    native = _ooc_code(payload, **mode_kwargs(mode, tmp_path))
    first_bad = next(v for v in ordered if v in (BAD_VALUE, BAD_DOMAIN))
    expected = "fpe_unencryptable_domain" if first_bad == BAD_DOMAIN else "fpe_unencryptable_value"
    assert native == _oracle_code(payload) == expected


def test_the_kernel_error_message_carries_no_cell_value(monkeypatch: pytest.MonkeyPatch) -> None:
    install_real_fpe_kernel(monkeypatch)
    secret_cell = "SECRET-CELL-xyz"
    with pytest.raises(StrategyError) as caught:
        run_chain(_payload(["123456", secret_cell]), _SEEDS)
    assert secret_cell not in str(caught.value)
    assert "'a'" in str(caught.value)


def test_the_loader_self_test_is_not_a_masking_call(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel = install_real_fpe_kernel(monkeypatch)
    load_compiled_fpe_kernel()
    assert kernel.calls == 0
    assert kernel.results == []
