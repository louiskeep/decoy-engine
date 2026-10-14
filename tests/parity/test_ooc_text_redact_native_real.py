"""Out-of-core text_redact native slice: the REAL compiled span kernel against the oracle.

Needs the abi-3 companion; skipped (not weakened) on a box without it, where the routing suite
and the companion-absent cases cover the Python path. Compared here: the OOC native route vs the
full-frame pandas oracle on the strict parity contract, vs the Python `text_redact_array`
reference, across batch sizes and both drivers; the lookaround detectors and a mixed
ASCII/non-ASCII column (per-cell domain split); the config variants; and the detector set the
compiled candidate call actually receives (the None-means-all rule).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.native._text_redact_kernel import SUPPORTED_IDS
from decoy_engine.execution.out_of_core._mask_group_b import text_redact_array
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import chain_graph, chain_plan, chain_sources, run_chain
from tests.parity._ooc_text_redact_data import (
    CLEAN,
    KEY,
    SOURCE,
    TextRedactConsumedWitness,
    forbid_python_text_redact,
    hide_text_redact_companion,
    install_marking,
    kernel_loads,
    spy_candidates,
    tr_seed,
)
from tests.parity.test_out_of_core_fk_parity import _run_oracle
from tests.parity.test_phase_d_d1a_hash_baseline import mode_kwargs

pytestmark = pytest.mark.skipif(
    not kernel_loads(),
    reason="decoy-engine-native text_redact kernel not loadable; the companion-present job covers this",
)

MODES = ["resident", "sink-batch", "sink-reorder"]
_SEEDS = (("a", tr_seed()),)


def _payload(values: list[Any] = SOURCE) -> pa.Table:
    return pa.table({"a": pa.array(values, pa.string())})


def _reference(values: list[Any], seed: Any = None) -> list[str | None]:
    seed = seed or tr_seed()
    return text_redact_array(pa.array(values, pa.string()), plan=seed).to_pylist()


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
    kernel = install_marking(monkeypatch, gate=False)
    forbid_python_text_redact(monkeypatch)
    witness = TextRedactConsumedWitness()
    with witness.observe():
        run = run_chain(_payload(), _SEEDS, batch_rows=batch_rows, **mode_kwargs(mode, tmp_path))
    witness.assert_consumed(kernel)
    got = run.tables["t"].column("a")
    assert got.type == pa.string()
    assert got.to_pylist() == _reference(SOURCE)
    assert got.to_pylist() != SOURCE
    _assert_strict_equal_to_oracle(run.tables, _oracle_tables(_payload(), _SEEDS))
    disposition = {e["column"]: e for e in run.result.quality_metrics[KEY]}
    assert disposition["a"]["executed_backend"] == RUST_COMPANION
    assert "reason" not in disposition["a"]


def test_native_matches_the_python_route_with_the_companion_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = run_chain(_payload(), _SEEDS, batch_rows=3).tables["t"].column("a").to_pylist()
    hide_text_redact_companion(monkeypatch)
    python = run_chain(_payload(), _SEEDS, batch_rows=3).tables["t"].column("a").to_pylist()
    assert native == python


@pytest.mark.parametrize(
    "values",
    [[None, None, None, None], ["", "", ""], [None, "", None], []],
    ids=["all-null", "all-empty", "null-and-empty", "zero-rows"],
)
@pytest.mark.parametrize("batch_rows", [None, 2])
def test_null_and_empty_columns_are_string_typed_and_match_the_oracle_values(
    values: list[Any], batch_rows: int | None
) -> None:
    payload = _payload(values)
    ooc = run_chain(payload, _SEEDS, batch_rows=batch_rows).tables
    oracle = _oracle_tables(payload, _SEEDS)
    assert ooc["t"].column("a").type == pa.string()
    assert ooc["t"].column("a").to_pylist() == oracle["t"].column("a").to_pylist() == values


# --- T1: per-detector-family and per-cell domain parity -----------------------------------------

_FAMILIES = {
    "rust_eligible": [
        "mail alice@example.com now",
        "call 415-555-0132",
        "card 4111 1111 1111 1111",
        "iban GB82WEST12345698765432",
        "ip 192.168.1.10",
        "see https://example.com/a",
        None,
    ],
    "lookaround": [
        "ssn 123-45-6789 on file",
        "zip 90210 and 10001-1234",
        "at 123 Main Street today",
        "ssn 123-45-6789 zip 90210",
        None,
        "",
    ],
    "non_ascii": [
        "Zoë mailed alice@example.com",
        "日本語 bob@example.org 415-555-0199",
        "naïve 123-45-6789 café",
        "emoji 😀 192.168.1.10",
        None,
    ],
    "mixed_ascii_non_ascii": [
        "plain alice@example.com",
        "Zoë alice@example.com",
        "plain 415-555-0132 then ssn 123-45-6789",
        "Zoë 415-555-0132 then ssn 123-45-6789",
        "\x1c separator alice@example.com",
        "no pii",
        None,
    ],
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", list(_FAMILIES))
def test_each_detector_family_is_byte_identical_to_the_oracle(
    family: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _FAMILIES[family]
    kernel = install_marking(monkeypatch, gate=False)
    forbid_python_text_redact(monkeypatch)
    run = run_chain(_payload(values), _SEEDS, batch_rows=3, **mode_kwargs(mode, tmp_path))
    assert kernel.calls
    got = run.tables["t"].column("a").to_pylist()
    assert got == _reference(values)
    assert got == _oracle_tables(_payload(values), _SEEDS)["t"].column("a").to_pylist()
    assert got != values


def test_the_full_corpus_agrees_cell_by_cell_with_the_oracle_in_one_batch() -> None:
    values = [v for fam in _FAMILIES.values() for v in fam] + SOURCE
    got = run_chain(_payload(values), _SEEDS).tables["t"].column("a").to_pylist()
    oracle = _oracle_tables(_payload(values), _SEEDS)["t"].column("a").to_pylist()
    assert len(got) == len(oracle) == len(values)
    for i, (g, o, v) in enumerate(zip(got, oracle, values, strict=True)):
        assert g == o, (i, v)


# --- T2: config variants and the compiled candidate call -------------------------------------------

_VARIANTS: dict[str, dict[str, Any]] = {
    "default": {},
    "subset_email": {"detectors": ["email"]},
    "subset_lookaround_only": {"detectors": ["ssn", "us_zip", "street_address"]},
    "subset_mixed_order": {"detectors": ["url", "ssn", "email", "us_phone"]},
    "unknown_detector": {"detectors": ["email", "no_such_detector"]},
    "empty_selection": {"detectors": []},
    "null_selection": {"detectors": None},
    "token": {"token": "###"},
    "empty_token": {"token": ""},
    "label": {"label_token": True},
    "label_and_token": {"token": "<X>", "label_token": True},
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("name", list(_VARIANTS))
def test_config_variants_are_byte_identical_to_the_oracle(
    name: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = tr_seed(**_VARIANTS[name])
    kernel = install_marking(monkeypatch, gate=False)
    forbid_python_text_redact(monkeypatch)
    run = run_chain(_payload(), (("a", seed),), batch_rows=5, **mode_kwargs(mode, tmp_path))
    assert kernel.calls
    got = run.tables["t"].column("a").to_pylist()
    assert got == _reference(SOURCE, seed)
    assert got == _oracle_tables(_payload(), (("a", seed),))["t"].column("a").to_pylist()


@pytest.mark.parametrize(
    "config", [{}, {"detectors": None}, {"detectors": []}], ids=["absent", "null", "empty"]
)
def test_an_empty_or_absent_selection_sends_every_supported_detector_to_the_candidate_call(
    config: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_candidates(monkeypatch)
    run = run_chain(_payload(), (("a", tr_seed(**config)),), batch_rows=8)
    assert [ids for _, ids in spy.calls] == [tuple(SUPPORTED_IDS)] * 2  # () would send none
    assert [n for n, _ in spy.calls] == [8, 8]
    got = run.tables["t"].column("a").to_pylist()
    assert all(g != s for g, s in zip(got, SOURCE, strict=True) if s not in CLEAN)
    oracle = _oracle_tables(_payload(), (("a", tr_seed(**config)),))
    assert got == oracle["t"].column("a").to_pylist()


@pytest.mark.parametrize(
    ("detectors", "sent"),
    [
        (["email"], ("email",)),
        (["url", "email"], ("url", "email")),
        (["email", "ssn", "us_phone"], ("email", "us_phone")),  # lookaround runs in Python
        (["email", "no_such_detector"], ("email",)),
    ],
    ids=["one", "ordered", "lookaround-stays-python", "unknown-dropped"],
)
def test_a_subset_reaches_the_candidate_call_as_its_supported_ids(
    detectors: list[str], sent: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = spy_candidates(monkeypatch)
    run_chain(_payload(), (("a", tr_seed(detectors=detectors)),), batch_rows=8)
    assert {ids for _, ids in spy.calls} == {sent}


def test_a_lookaround_only_selection_still_masks_through_the_python_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = tr_seed(detectors=["ssn", "us_zip", "street_address"])
    run = run_chain(_payload(_FAMILIES["lookaround"]), (("a", seed),), batch_rows=3)
    got = run.tables["t"].column("a").to_pylist()
    assert got == _reference(_FAMILIES["lookaround"], seed)
    assert "123-45-6789" not in "".join(g for g in got if g)


def test_the_loader_self_test_is_not_a_masking_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.native._text_redact_kernel import load_text_redact_kernel

    spy = spy_candidates(monkeypatch)
    assert load_text_redact_kernel() is not None
    assert spy.calls == []
