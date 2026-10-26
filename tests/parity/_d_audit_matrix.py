"""D-audit: the compute-admission matrix, derived from the live admission predicates.

Nothing here reads an observed disposition. Each route's cell set comes from the constants the
router itself consults (the SEQ native op table and key rule, the out-of-core support sets and
deferral tables, the native-chunked operator registry and FK reroute), so a new operator or a
changed admission rule moves the matrix and the audit test fails until it is classified.

A cell is (route, strategy, role). The full-frame pandas route is the retained oracle and is
not a cell. Every cell carries exactly one classification.
"""

from __future__ import annotations

from typing import Final

from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution._sequential_native import _KEY_STRATEGIES, _OPS
from decoy_engine.execution._sequential_native_ops import FAKER
from decoy_engine.execution._sequential_native_text_mask import TEXT_MASK
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.execution.out_of_core import _compat

# Classifications.
COMPILED_RUST: Final = "compiled_rust_kernel"
COMPILED_SELECTION: Final = "compiled_index_selection"
NAMED_ARROW: Final = "named_arrow_kernel"
# Python per-distinct-value selection expanded with an Arrow dict-encode + take (NOT a pure Arrow
# or Rust kernel): the out-of-core code_set map (`kernel/_code_set.py`).
ARROW_DICT_MAP: Final = "arrow_dict_map"
NOOP: Final = "noop"
DEDICATED_PYTHON: Final = "dedicated_python"
EXCLUDED: Final = "excluded"

SEQ: Final = "sequential"
OOC: Final = "out_of_core"
CHUNKED: Final = "native_chunked"
PAYLOAD: Final = "payload"
KEY: Final = "key"

Cell = tuple[str, str, str]

# Strategies whose compute is a named Arrow kernel (`kernel/_scalar.py`), not a companion.
NAMED_ARROW_STRATEGIES: Final = frozenset({"redact", "truncate"})
NOOP_STRATEGIES: Final = frozenset({"passthrough"})
# Rust span detection with the splice in Python (the established text-mask shape).
TEXT_STRATEGIES: Final = frozenset({"text_redact", TEXT_MASK})

# The documented dedicated-Python compute. `why` is what the Phase D close records.
PYTHON_WHY: Final = {
    "code_set": "SEQ and native-chunked code_set run per-cell apply_code_set; out-of-core selects "
    "per distinct value via an Arrow dict-map (B6). Backlog: the SEQ/chunked consumers.",
    "ner": "Python-by-design (spaCy inference); a config of text_redact/text_mask, not a strategy",
    "faker_declined": "faker cells admission declines (config/type) or FK-key cells; Python pool",
    "key_declined": "native-capable non-injective strategy on an FK key stays Python, reporting only",
    "other_handler": "shared full-frame handler with no native admission on any route",
}


def _seq_payload(strategy: str) -> str:
    if strategy in _OPS:
        if strategy == FAKER:
            return COMPILED_SELECTION
        return COMPILED_RUST
    if strategy in NAMED_ARROW_STRATEGIES:
        return NAMED_ARROW
    if strategy in NOOP_STRATEGIES:
        return NOOP
    return DEDICATED_PYTHON


def _seq_key(strategy: str) -> str:
    if strategy in _OPS:
        return COMPILED_RUST if strategy in _KEY_STRATEGIES else DEDICATED_PYTHON
    return _seq_payload(strategy)


# code_set is the one out-of-core Group (c) member whose compute is neither a Rust kernel nor a
# pure Arrow op: B6 selects each distinct non-null value once through `apply_code_set` and expands
# with an Arrow dict-map + take (`kernel/_code_set.py`), so it is ARROW_DICT_MAP, not DEDICATED_
# PYTHON (per-cell) and not COMPILED_RUST. text_mask joined the compiled span path here too (B5),
# so this is the only hand-named OOC compute fact left; everything else admitted on this route has
# a proven out-of-core kernel.


def _ooc_payload(strategy: str) -> str:
    if strategy in NAMED_ARROW_STRATEGIES:
        return NAMED_ARROW
    if strategy in NOOP_STRATEGIES:
        return NOOP
    if strategy == FAKER:
        # B4: deterministic-REUSE person-name faker selects from its prepared pool
        # through the compiled index kernel (`_faker_native`), the same selection
        # class SEQ and native-chunked use. The exact-string source and the
        # companion-absent reference kernel are runtime qualifications, not a
        # different compute class.
        return COMPILED_SELECTION
    if strategy == "code_set":
        return ARROW_DICT_MAP
    if strategy in (
        _compat._INITIAL_SUPPORTED_STRATEGIES
        | _compat._GROUP_B_SUPPORTED_STRATEGIES
        | _compat._GROUP_C_SUPPORTED_STRATEGIES
    ):
        # hash (initial), all of Group B (fpe/text_redact/categorical, `_mask_group_b` byte-parity),
        # and bucket_perturb (Group (c) conditional kernel) -- each a proven out-of-core kernel.
        return COMPILED_RUST
    return DEDICATED_PYTHON  # outside every known OOC group: conservative, never a false Rust claim


def _chunked_payload(strategy: str) -> str:
    if strategy in NAMED_ARROW_STRATEGIES:
        return NAMED_ARROW
    if strategy in NOOP_STRATEGIES:
        return NOOP
    if strategy in NATIVE_POOL_STRATEGIES:
        return COMPILED_SELECTION
    return COMPILED_RUST


def derive_matrix() -> dict[Cell, str]:
    matrix: dict[Cell, str] = {}
    for strategy in SCALAR_HANDLERS:
        matrix[(SEQ, strategy, PAYLOAD)] = _seq_payload(strategy)
        matrix[(SEQ, strategy, KEY)] = _seq_key(strategy)
        if strategy in _compat._SUPPORTED_WORK_STRATEGIES:
            matrix[(OOC, strategy, PAYLOAD)] = _ooc_payload(strategy)
            if strategy in _compat._INITIAL_SUPPORTED_STRATEGIES:
                matrix[(OOC, strategy, KEY)] = _ooc_payload(strategy)
            else:
                matrix[(OOC, strategy, KEY)] = EXCLUDED
        else:
            matrix[(OOC, strategy, PAYLOAD)] = EXCLUDED
            matrix[(OOC, strategy, KEY)] = EXCLUDED
    for spec in OPERATORS.values():
        matrix[(CHUNKED, spec.strategy, PAYLOAD)] = _chunked_payload(spec.strategy)
        matrix[(CHUNKED, spec.strategy, KEY)] = EXCLUDED  # FK tables reroute to the oracle
    for strategy in SCALAR_HANDLERS:
        if strategy not in {s.strategy for s in OPERATORS.values()}:
            matrix[(CHUNKED, strategy, PAYLOAD)] = EXCLUDED  # fallback_policy -> oracle
            matrix[(CHUNKED, strategy, KEY)] = EXCLUDED
    return matrix


def cells(route: str, classification: str) -> list[Cell]:
    return sorted(c for c, v in derive_matrix().items() if c[0] == route and v == classification)


def kernel_sets_agree() -> bool:
    """The chunked allowlists are the registry's kernel and pool shapes, nothing else."""
    return {s.strategy for s in OPERATORS.values()} == (
        NATIVE_KERNEL_STRATEGIES | NATIVE_POOL_STRATEGIES
    )
