"""Literal snapshot of every per-operator table, copied from engine main 01c560da.

R1 derives these tables from one operator registry. This file is the
behavior-preservation baseline: it was written and run green on the unmodified
code, so each literal is the value the hand-written tables held before the
refactor. A derived table that drifts from its old value fails here.
"""

from __future__ import annotations

from types import MappingProxyType

import pyarrow as pa

from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution import _unified_slice_evidence as evidence
from decoy_engine.execution._unified_slice_resident_types import _ADMITTED_RESIDENT_TYPES
from decoy_engine.execution.native import _operator_config_rejections as rejections
from decoy_engine.execution.native._chunked_evidence import _COMPANION_STRATEGIES
from decoy_engine.execution.native._dispatch import _INDEX_KERNEL_STRATEGIES
from decoy_engine.execution.native._real_type_admission import C1_PROVIDER_ALLOWLIST
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.execution.physical import _shadow_bindings as bindings
from decoy_engine.execution.physical._shadow_assembly import (
    _NULL_ON_EMPTY_STRATEGIES,
    _TOKENIZING_STRATEGIES,
)

# The non-text_redact operator ids. Grew by native_fpe (C6a); still the set every table
# below is built from, with native_text_redact folded in per test as before.
_NINE_IDS = frozenset(
    {
        "native_passthrough",
        "native_redact",
        "native_truncate",
        "native_keyed_hash",
        "native_fpe",
        "native_categorical",
        "native_bucket_perturb",
        "native_group_key",
        "native_date_shift",
        "native_faker_select",
    }
)
_NINE_STRATEGIES = frozenset(
    {
        "passthrough",
        "redact",
        "truncate",
        "hash",
        "fpe",
        "faker",
        "categorical",
        "bucket_perturb",
        "group_key",
        "date_shift",
    }
)


def test_operator_ids_and_constants() -> None:
    assert _NINE_IDS | {"native_text_redact"} == admission.ALLOWED_OPERATOR_IDS
    assert type(admission.ALLOWED_OPERATOR_IDS) is frozenset
    assert admission.HASH_OPERATOR_ID == "native_keyed_hash"
    assert admission.CATEGORICAL_OPERATOR_ID == "native_categorical"
    assert admission.BUCKET_PERTURB_OPERATOR_ID == "native_bucket_perturb"
    assert admission.GROUP_KEY_OPERATOR_ID == "native_group_key"
    assert admission.DATE_SHIFT_OPERATOR_ID == "native_date_shift"
    assert admission.FAKER_OPERATOR_ID == "native_faker_select"


def test_backend_by_operator_id() -> None:
    assert dict(admission.BACKEND_BY_OPERATOR_ID) == {
        "native_keyed_hash": "rust_companion",
        "native_fpe": "rust_companion",
        "native_categorical": "rust_companion",
        "native_bucket_perturb": "rust_companion",
        "native_date_shift": "rust_companion",
        "native_group_key": "rust_companion",
        "native_faker_select": "rust_pool_select",
        "native_redact": "arrow_python",
        "native_truncate": "arrow_python",
        "native_text_redact": "arrow_python",
        "native_passthrough": "arrow_python",
    }
    assert isinstance(admission.BACKEND_BY_OPERATOR_ID, MappingProxyType)


def test_companion_dependent_and_required_kernel() -> None:
    assert (
        frozenset(
            {
                "native_keyed_hash",
                "native_fpe",
                "native_categorical",
                "native_bucket_perturb",
                "native_group_key",
                "native_date_shift",
                "native_faker_select",
            }
        )
        == admission._COMPANION_DEPENDENT_OPERATOR_IDS
    )
    assert type(admission._COMPANION_DEPENDENT_OPERATOR_IDS) is frozenset
    assert admission._OPERATOR_REQUIRED_KERNEL == {
        "native_keyed_hash": "crypto",
        "native_fpe": "fpe",
        "native_categorical": "index",
        "native_bucket_perturb": "index",
        "native_group_key": "raw_hex",
        "native_date_shift": "index",
        "native_faker_select": "index",
    }
    assert type(admission._OPERATOR_REQUIRED_KERNEL) is dict


def test_routed_diagnostic_obligations_date_shift_and_fpe() -> None:
    # date_shift routes its format_error row errors; fpe routes its two Python-computed
    # residual-risk warnings (the fail-closed kill is a StrategyError, not a routed RowError).
    assert {
        "native_date_shift": frozenset({"reduce_row_error:format_error"}),
        "native_fpe": frozenset(
            {
                "reduce_warning:fpe_join_group_active",
                "reduce_warning:fpe_partial_plaintext_disclosure",
            }
        ),
    } == admission._ROUTED_DIAGNOSTIC_OBLIGATIONS
    assert type(admission._ROUTED_DIAGNOSTIC_OBLIGATIONS) is dict
    assert type(admission._ROUTED_DIAGNOSTIC_OBLIGATIONS["native_date_shift"]) is frozenset


def test_positive_kernel_evidence_operator_ids() -> None:
    assert (
        frozenset({"native_keyed_hash", "native_fpe", "native_faker_select"})
        == evidence._POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS
    )
    assert type(evidence._POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS) is frozenset


def test_slice_strategies_and_operator_id_by_strategy() -> None:
    assert _NINE_STRATEGIES | {"text_redact"} == bindings.SLICE_STRATEGIES
    assert type(bindings.SLICE_STRATEGIES) is frozenset
    assert bindings.OPERATOR_ID_BY_STRATEGY == {
        "passthrough": "native_passthrough",
        "redact": "native_redact",
        "truncate": "native_truncate",
        "hash": "native_keyed_hash",
        "fpe": "native_fpe",
        "faker": "native_faker_select",
        "categorical": "native_categorical",
        "bucket_perturb": "native_bucket_perturb",
        "group_key": "native_group_key",
        "date_shift": "native_date_shift",
        "text_redact": "native_text_redact",
    }
    assert type(bindings.OPERATOR_ID_BY_STRATEGY) is dict


def test_admitted_resident_types_has_nine_keys_and_no_group_key() -> None:
    assert {
        "passthrough": frozenset({pa.string(), pa.int64(), pa.bool_()}),
        "redact": frozenset({pa.string()}),
        "truncate": frozenset({pa.string()}),
        "hash": frozenset({pa.string(), pa.int64()}),
        "fpe": frozenset({pa.string()}),
        "categorical": frozenset({pa.string()}),
        "bucket_perturb": frozenset({pa.string()}),
        "date_shift": frozenset({pa.string()}),
        "faker": frozenset({pa.string()}),
        "text_redact": frozenset({pa.string()}),
    } == _ADMITTED_RESIDENT_TYPES
    assert "group_key" not in _ADMITTED_RESIDENT_TYPES
    assert type(_ADMITTED_RESIDENT_TYPES) is dict
    assert all(type(v) is frozenset for v in _ADMITTED_RESIDENT_TYPES.values())


def test_native_strategy_sets() -> None:
    assert (
        frozenset(
            {
                "passthrough",
                "redact",
                "truncate",
                "hash",
                "fpe",
                "categorical",
                "bucket_perturb",
                "group_key",
                "date_shift",
                "text_redact",
            }
        )
        == NATIVE_KERNEL_STRATEGIES
    )
    assert frozenset({"faker"}) == NATIVE_POOL_STRATEGIES
    assert type(NATIVE_KERNEL_STRATEGIES) is frozenset
    assert type(NATIVE_POOL_STRATEGIES) is frozenset
    # fpe loads its own compiled kernel, not the index kernel, so it is not here.
    assert (
        frozenset({"faker", "categorical", "bucket_perturb", "date_shift"})
        == _INDEX_KERNEL_STRATEGIES
    )
    assert (
        frozenset({"hash", "fpe", "categorical", "bucket_perturb", "date_shift", "group_key"})
        == _COMPANION_STRATEGIES
    )
    assert type(_INDEX_KERNEL_STRATEGIES) is frozenset
    assert type(_COMPANION_STRATEGIES) is frozenset


def test_assembly_strategy_sets() -> None:
    assert (
        frozenset(
            {
                "redact",
                "truncate",
                "hash",
                "fpe",
                "faker",
                "categorical",
                "group_key",
                "date_shift",
            }
        )
        == _TOKENIZING_STRATEGIES
    )
    assert frozenset({"bucket_perturb", "text_redact"}) == _NULL_ON_EMPTY_STRATEGIES
    assert type(_TOKENIZING_STRATEGIES) is frozenset
    assert type(_NULL_ON_EMPTY_STRATEGIES) is frozenset


def test_group_key_sibling_types_and_provider_allowlist() -> None:
    assert (
        frozenset({pa.string(), pa.int64(), pa.bool_()})
        == rejections._NATIVE_GROUP_KEY_SIBLING_TYPES
    )
    assert type(rejections._NATIVE_GROUP_KEY_SIBLING_TYPES) is frozenset
    assert frozenset({"person_first_name", "person_last_name"}) == C1_PROVIDER_ALLOWLIST
    assert type(C1_PROVIDER_ALLOWLIST) is frozenset
