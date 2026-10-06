"""One frozen descriptor per native masking operator: the single home of shared per-operator facts.

Before this module the same facts (operator id, backend, compiled kernel, resident types,
assembly shape, provider allowlist) were hand-written in about a dozen parallel tables, so
adding an operator edited 8-14 files and a missed table drifted silently. Each of those
tables is now a one-line derivation over `OPERATORS`, keeping its old name and type.

Adding an operator means one `OperatorSpec` here plus its kernel-call branch and its
route-specific gates. Route-specific contracts (chunked source-type domains, degenerate
output handling, the two evidence records, missing-companion handling, date_shift's error
channel, group_key's sibling check) deliberately stay in their route modules.

Leaf module: stdlib and pyarrow only. Cheap unified-slice admission imports this at process
start and must never reach `execution.physical`, and the backend vocabulary lives here
because `native/_chunked_evidence.py` (which imports the planner) cannot be imported back
without a cycle. `native/_chunked_evidence.py` re-exports the vocabulary names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pyarrow as pa

__all__ = [
    "ARROW_PYTHON",
    "OPERATORS",
    "PANDAS_ORACLE",
    "RUST_COMPANION",
    "RUST_POOL_SELECT",
    "OperatorSpec",
    "operator_spec",
]

# The backend vocabulary the chunked route's evidence and the unified slice share.
RUST_COMPANION: Final = "rust_companion"
RUST_POOL_SELECT: Final = "rust_pool_select"
ARROW_PYTHON: Final = "arrow_python"
PANDAS_ORACLE: Final = "pandas_oracle"

Shape = Literal["kernel", "pool"]
RequiredKernel = Literal["crypto", "index", "raw_hex"]
AssemblyShape = Literal["tokenizing", "null_on_empty", "type_preserving"]


@dataclass(frozen=True)
class OperatorSpec:
    """The facts about one native slice operator that more than one module needs.

    `unified_resident_types` is the target-column type domain the unified slice admits;
    `None` means "no target type gate", which is group_key (its admission checks the
    sibling column instead, so it has no entry in the derived table).
    `required_kernel` names the compiled kernel the operator loads, `None` for the
    pure-Arrow operators. `positive_kernel_evidence` marks operators whose "compiled kernel
    ran" claim must be observed rather than inferred."""

    strategy: str
    operator_id: str
    shape: Shape
    planned_backend: str
    required_kernel: RequiredKernel | None
    positive_kernel_evidence: bool
    unified_resident_types: frozenset[pa.DataType] | None
    full_frame_assembly: AssemblyShape
    provider_allowlist: frozenset[str] | None = None


_STRING_ONLY = frozenset({pa.string()})

_SPECS = (
    OperatorSpec(
        strategy="passthrough",
        operator_id="native_passthrough",
        shape="kernel",
        planned_backend=ARROW_PYTHON,
        required_kernel=None,
        positive_kernel_evidence=False,
        unified_resident_types=frozenset({pa.string(), pa.int64(), pa.bool_()}),
        full_frame_assembly="type_preserving",
    ),
    OperatorSpec(
        strategy="redact",
        operator_id="native_redact",
        shape="kernel",
        planned_backend=ARROW_PYTHON,
        required_kernel=None,
        positive_kernel_evidence=False,
        unified_resident_types=_STRING_ONLY,
        full_frame_assembly="tokenizing",
    ),
    OperatorSpec(
        strategy="truncate",
        operator_id="native_truncate",
        shape="kernel",
        planned_backend=ARROW_PYTHON,
        required_kernel=None,
        positive_kernel_evidence=False,
        unified_resident_types=_STRING_ONLY,
        full_frame_assembly="tokenizing",
    ),
    OperatorSpec(
        strategy="hash",
        operator_id="native_keyed_hash",
        shape="kernel",
        planned_backend=RUST_COMPANION,
        required_kernel="crypto",
        positive_kernel_evidence=True,
        unified_resident_types=frozenset({pa.string(), pa.int64()}),
        full_frame_assembly="tokenizing",
    ),
    OperatorSpec(
        strategy="faker",
        operator_id="native_faker_select",
        shape="pool",
        planned_backend=RUST_POOL_SELECT,
        required_kernel="index",
        positive_kernel_evidence=True,
        unified_resident_types=_STRING_ONLY,
        full_frame_assembly="tokenizing",
        # The frozen C1 recipe's providers (FIRST=person_first_name, LAST/MAIDEN=
        # person_last_name). Every other pool_native provider is out of scope and is
        # rejected with a coded reason rather than silently admitted.
        provider_allowlist=frozenset({"person_first_name", "person_last_name"}),
    ),
    OperatorSpec(
        strategy="categorical",
        operator_id="native_categorical",
        shape="kernel",
        planned_backend=RUST_COMPANION,
        required_kernel="index",
        positive_kernel_evidence=False,
        unified_resident_types=_STRING_ONLY,
        full_frame_assembly="tokenizing",
    ),
    OperatorSpec(
        strategy="bucket_perturb",
        operator_id="native_bucket_perturb",
        shape="kernel",
        planned_backend=RUST_COMPANION,
        required_kernel="index",
        positive_kernel_evidence=False,
        unified_resident_types=_STRING_ONLY,
        # Differs from the tokenizing operators only on an empty column: it passes its
        # source object series through, which becomes an Arrow null column.
        full_frame_assembly="null_on_empty",
    ),
    OperatorSpec(
        strategy="group_key",
        operator_id="native_group_key",
        shape="kernel",
        planned_backend=RUST_COMPANION,
        required_kernel="raw_hex",
        positive_kernel_evidence=False,
        unified_resident_types=None,
        full_frame_assembly="tokenizing",
    ),
    OperatorSpec(
        strategy="date_shift",
        operator_id="native_date_shift",
        shape="kernel",
        planned_backend=RUST_COMPANION,
        required_kernel="index",
        positive_kernel_evidence=False,
        unified_resident_types=_STRING_ONLY,
        full_frame_assembly="tokenizing",
    ),
)

# Keyed by strategy. Read-only so a consumer cannot mutate the shared facts.
OPERATORS: Final[Mapping[str, OperatorSpec]] = MappingProxyType(
    {spec.strategy: spec for spec in _SPECS}
)


def operator_spec(strategy: str) -> OperatorSpec:
    """The descriptor for `strategy`; raises `KeyError` for a strategy with no native slice operator."""
    return OPERATORS[strategy]
