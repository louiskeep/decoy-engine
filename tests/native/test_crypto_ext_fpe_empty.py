"""Empty-string handling of the FPE reference kernel.

The shipped strategy and `unmask` treat a non-null `""` as a missing-data cell:
preserved as `""`, never ciphered, and absent from the residual-risk warning
inputs. The reference kernel is the oracle for the native kernel, so it must
agree. The value functions stay fail-closed for `""`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.errors import FpeUnencryptableError, MaskKeyRequiredError
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution.native import _crypto_reference
from decoy_engine.execution.native._crypto_ext import FpeConfig
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    build_ff1_tweak,
    fpe_decrypt_value,
    fpe_encrypt_value,
)

MK = bytes(range(32))
NS = "people.ssn"
_JOB_SEED = (0xC0FFEE).to_bytes(8, "big")
_DIGITS = "0123456789"


def _ctx() -> StrategyContext:
    return StrategyContext(
        registry=get_default_registry(),
        pool_cache=PoolCache(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        job_seed=_JOB_SEED,
    )


def _col(config: dict[str, object]) -> ColumnSeed:
    return ColumnSeed(
        namespace="fpe_ns",
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=tuple(config.items()),
        coherent_with=(),
    )


def _shipped(config: dict[str, object], column: str, rows: list[str | None]):
    df = pd.DataFrame({column: rows}, dtype=object)
    out, warnings = FpeStrategyHandler(chunk_count=1).run(df, column, _col(config), _ctx())
    return out[column].tolist(), warnings


def _ref(
    forward: bool,
    rows: object,
    config: dict[str, object],
    *,
    column: str = "acct",
    key: bytes | None = _JOB_SEED,
    namespace: str = "fpe_ns",
):
    kern = reference_fpe()
    run = kern.encrypt_batch if forward else kern.decrypt_batch
    return run(
        rows,  # type: ignore[arg-type]
        mask_key=key,
        namespace=namespace,
        tweak_column=column,
        config=FpeConfig.from_mapping(config),
    )


def _warning_view(warnings: object) -> list[tuple[str, str, object]]:
    return [(w.code, w.column, w.detail) for w in warnings]  # type: ignore[attr-defined]


_WARNING_CASES = [
    ({"charset": "digits"}, ["", "M000001", None, ""]),
    ({"charset": "digits"}, ["", "", ""]),
    ({"charset": "digits", "fpe_join_group": "link"}, ["", "", ""]),
    ({"charset": "digits", "fpe_join_group": "link"}, ["", "123456", None]),
    ({"charset": "digits", "preserve_separators": False}, ["123456", "", "654321"]),
]


@pytest.mark.parametrize("config,rows", _WARNING_CASES)
def test_encrypt_warnings_match_strategy(config: dict[str, object], rows: list[str | None]) -> None:
    shipped_vals, shipped_warnings = _shipped(config, "acct", rows)
    result = _ref(True, rows, config)
    assert result.errors == ()
    assert result.to_pylist() == [None if pd.isna(v) else v for v in shipped_vals]
    assert _warning_view(result.warnings) == _warning_view(shipped_warnings)


def test_empty_cells_do_not_change_residual_risk_counts() -> None:
    result = _ref(True, ["", "M000001", None], {"charset": "digits"})
    (warning,) = [w for w in result.warnings if w.code == "fpe_partial_plaintext_disclosure"]
    assert warning.detail["affected_values"] == 1
    assert warning.detail["total_values"] == 1


@pytest.mark.parametrize(
    "config",
    [
        {"charset": "digits"},
        {"charset": "digits", "preserve_separators": True},
        {"charset": "digits", "preserve_separators": False},
        {"charset": "digits", "validate_luhn": True},
        {"charset": "digits", "checksum": "luhn"},
        {"charset": "digits", "fpe_join_group": "link"},
    ],
)
def test_decrypt_passes_empty_through_and_round_trips(config: dict[str, object]) -> None:
    plain = ["", "4111111111111111", None, "", "5500005555555559"]
    enc = _ref(True, plain, config, key=MK, namespace=NS)
    assert enc.errors == ()
    assert enc.to_pylist()[0] == "" and enc.to_pylist()[3] == ""
    dec = _ref(False, enc.values, config, key=MK, namespace=NS)
    assert dec.errors == ()
    assert dec.to_pylist() == plain
    all_empty = _ref(False, ["", ""], config, key=MK, namespace=NS)
    assert all_empty.errors == ()
    assert all_empty.to_pylist() == ["", ""]


@pytest.mark.parametrize("config", [{"charset": "digits", "checksum": "luhn"}])
def test_encrypt_empty_under_explicit_checksum_matches_strategy(
    config: dict[str, object],
) -> None:
    rows: list[str | None] = ["", "4111111111111111", ""]
    shipped_vals, shipped_warnings = _shipped(config, "acct", rows)
    result = _ref(True, rows, config)
    assert result.errors == ()
    assert result.to_pylist() == shipped_vals
    assert _warning_view(result.warnings) == _warning_view(shipped_warnings)


def test_value_functions_still_fail_closed_on_empty() -> None:
    key = b"\x01" * 32
    tweak = build_ff1_tweak(FF1_TWEAK_SCOPE_COLUMN, "acct")
    with pytest.raises(FpeUnencryptableError):
        fpe_encrypt_value("", key, _DIGITS, tweak, True, False, None)
    with pytest.raises(FpeUnencryptableError):
        fpe_decrypt_value("", key, _DIGITS, tweak, True, False, None)


@pytest.mark.parametrize("forward", [True, False])
def test_empty_boundary_with_digits(forward: bool) -> None:
    config = {"charset": "digits"}
    result = _ref(forward, ["", " ", "---", "123456"], config)
    out = result.to_pylist()
    assert out[0] == ""
    assert out[1] is None and out[2] is None
    assert out[3] not in (None, "")
    assert [(e.row_index, e.code) for e in result.errors] == [
        (1, "fpe_unencryptable_value"),
        (2, "fpe_unencryptable_value"),
    ]


@pytest.mark.parametrize("forward", [True, False])
def test_error_indices_survive_interspersed_empty_and_null(forward: bool) -> None:
    rows = ["", None, " ", "", "123456", None, "---", ""]
    result = _ref(forward, rows, {"charset": "digits"})
    assert [(e.row_index, e.code) for e in result.errors] == [
        (2, "fpe_unencryptable_value"),
        (6, "fpe_unencryptable_value"),
    ]
    out = result.to_pylist()
    assert [out[i] for i in (0, 3, 7)] == ["", "", ""]
    assert [out[i] for i in (1, 2, 5, 6)] == [None] * 4


class _EmptyText:
    def __str__(self) -> str:
        return ""


@pytest.mark.parametrize("forward", [True, False])
def test_object_whose_str_is_empty_is_treated_as_empty(forward: bool) -> None:
    result = _ref(forward, [_EmptyText(), "123456", None], {"charset": "digits"})
    assert result.errors == ()
    out = result.to_pylist()
    assert out[0] == "" and out[2] is None and out[1] not in (None, "")
    assert not result.warnings


@pytest.mark.parametrize("forward", [True, False])
def test_missing_key_still_raises_before_any_value(forward: bool) -> None:
    with pytest.raises(MaskKeyRequiredError):
        _ref(forward, [""], {"charset": "digits"}, key=None)


@pytest.mark.parametrize("forward", [True, False])
def test_invalid_charset_still_raises_with_empty_input(forward: bool) -> None:
    with pytest.raises(Exception) as excinfo:
        _ref(forward, [""], {"charset": "aa"})
    assert not isinstance(excinfo.value, MaskKeyRequiredError)


@pytest.mark.parametrize("forward", [True, False])
def test_empty_cells_never_reach_the_transform(
    forward: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_enc, real_dec = fpe_encrypt_value, fpe_decrypt_value

    def spy_enc(text: str, *args: object) -> str:
        seen.append(text)
        return real_enc(text, *args)  # type: ignore[arg-type]

    def spy_dec(text: str, *args: object) -> str:
        seen.append(text)
        return real_dec(text, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(_crypto_reference, "fpe_encrypt_value", spy_enc)
    monkeypatch.setattr(_crypto_reference, "fpe_decrypt_value", spy_dec)
    result = _ref(forward, ["", "123456", _EmptyText(), None, ""], {"charset": "digits"})
    assert result.errors == ()
    assert seen == ["123456"]
