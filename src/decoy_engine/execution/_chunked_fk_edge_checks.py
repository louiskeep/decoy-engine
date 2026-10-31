"""The ordered admission checks `gate_fk_child_edges` runs on each FK edge.

`EDGE_CHECKS` is a predicate table: `(name, rejects, error)` rows evaluated
top to bottom, and the first row whose `rejects(edge)` holds raises its
`error(edge)`. Order is part of the contract: a later predicate may read a
field only an earlier one has proven safe to read (the composite check runs
before anything indexes `parent_cols[0]`, the unprovable-dtype check runs
before `_dtype_family` sees a declared dtype), and a config that breaks
several conditions reports the earliest one. The lettered and numbered
conditions in each row's comment are the ones `gate_fk_child_edges`'s
docstring defines.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass
from typing import Any

from decoy_engine import reasons
from decoy_engine.execution._chunked_fk_dtype_safety import declared_fk_hash_dtype_is_safe
from decoy_engine.execution._chunked_fk_vocab import (
    DTYPE_INVARIANT_STRATEGIES,
    NAMESPACE_REQUIRING_STRATEGIES,
    _dtype_family,
)
from decoy_engine.plan._errors import PlanCompileError


@dataclass(frozen=True)
class FkEdge:
    """One `parent -> child` edge into the gated table, as raw config values.

    Table and column fields keep whatever the config holds (not coerced), so
    paths and messages render exactly as the config spelled them. The derived
    properties index `parent_cols[0]` / `child_cols[0]` and are only read by
    checks that run after the composite check.
    """

    parent_table: Any
    parent_cols: Any
    child_table: Any
    child_cols: Any
    rel_ns: str | None
    orphan_policy: Any
    col_index: Mapping[tuple[str, str], dict[str, Any]]
    child_endpoint_columns: Set[tuple[str, str]]

    @property
    def path_prefix(self) -> str:
        return (
            f"relationships[{self.parent_table}.{self.parent_cols}"
            f"->{self.child_table}.{self.child_cols}]"
        )

    @property
    def parent_col(self) -> str:
        return str(self.parent_cols[0])

    @property
    def child_col(self) -> str:
        return str(self.child_cols[0])

    @property
    def parent_cfg(self) -> dict[str, Any]:
        return self.col_index.get((str(self.parent_table), self.parent_col), {})

    @property
    def child_cfg(self) -> dict[str, Any]:
        return self.col_index.get((str(self.child_table), self.child_col), {})

    @property
    def parent_strategy(self) -> Any:
        return self.parent_cfg.get("strategy")

    @property
    def parent_ns(self) -> str | None:
        return self.parent_cfg.get("namespace")

    @property
    def child_ns(self) -> str | None:
        return self.child_cfg.get("namespace")

    @property
    def label(self) -> str:
        return (
            f"FK edge {self.parent_table}.{self.parent_col}->{self.child_table}.{self.child_col}: "
        )

    def parent_path(self, field: str) -> str:
        return f"tables.{self.parent_table}.columns.{self.parent_col}.{field}"

    def child_path(self, field: str) -> str:
        return f"tables.{self.child_table}.columns.{self.child_col}.{field}"


@dataclass(frozen=True)
class EdgeCheck:
    name: str
    rejects: Callable[[FkEdge], bool]
    error: Callable[[FkEdge], PlanCompileError]


def _orphan_policy_not_remap(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_ORPHAN_POLICY_NOT_REMAP,
        path=f"{e.path_prefix}.orphan_policy",
        message=(
            f"FK edge {e.parent_table}->{e.child_table}: chunked self-masking "
            f"requires orphan_policy 'remap'; got {e.orphan_policy!r}. "
            "REMAP mints via the same parent strategy and namespace as "
            "self-masking. WARN/FAIL/PRESERVE require the parent key set "
            "resident. Use run_pipeline or run_sequential instead."
        ),
    )


def _composite_unsupported(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_COMPOSITE_UNSUPPORTED,
        path=f"{e.path_prefix}.columns",
        message=(
            f"FK edge {e.parent_table}->{e.child_table}: composite FK edges "
            "(multi-column keys) are not supported in chunked self-masking. "
            "Use run_pipeline or run_sequential instead."
        ),
    )


def _parent_strategy_not_self_mask_safe(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_PARENT_STRATEGY_NOT_SELF_MASK_SAFE,
        path=e.parent_path("strategy"),
        message=(
            f"{e.label}"
            f"parent key strategy {e.parent_strategy!r} is not self-mask "
            "safe. Chunked FK self-masking admits exactly one strategy: "
            "'hash' (proven cross-adapter byte-identical for the exact "
            "dtype set predicate 12 restricts FK keys to). Use "
            "run_pipeline or run_sequential for any other strategy."
        ),
    )


def _parent_namespace_missing(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_PARENT_NAMESPACE_MISSING,
        path=e.parent_path("namespace"),
        message=(
            f"{e.label}"
            f"parent column has no namespace and strategy "
            f"{e.parent_strategy!r} requires one. "
            f"The {e.parent_strategy!r} strategy raises an error at "
            "execution when no namespace is declared. "
            "Add 'namespace: <ns>' to the parent column."
        ),
    )


def _parent_namespace_mismatch(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_PARENT_NAMESPACE_MISMATCH,
        path=f"{e.path_prefix}.namespace",
        message=(
            f"{e.label}"
            f"edge namespace {e.rel_ns!r} disagrees with parent column "
            f"namespace {e.parent_ns!r}. The parent column namespace is "
            "the authoritative masking key; the edge namespace is a "
            "label only and is never used to derive a masked value. "
            f"Update the edge namespace to {e.parent_ns!r} or remove it."
        ),
    )


def _child_namespace_missing(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_NAMESPACE_MISSING,
        path=e.child_path("namespace"),
        message=(
            f"{e.label}"
            f"child column {e.child_table}.{e.child_col} has no explicit namespace. "
            "ColumnSeed.namespace comes from the config column entry, not from "
            "FK auto-binding. "
            f"Add 'namespace: {e.parent_ns}' to the child column."
        ),
    )


def _child_namespace_mismatch(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_NAMESPACE_MISMATCH,
        path=e.child_path("namespace"),
        message=(
            f"{e.label}"
            f"child namespace {e.child_ns!r} does not match parent column "
            f"namespace {e.parent_ns!r}. "
            "Self-masking requires child_ns == parent_ns so "
            "derive(seed, ns, value) produces byte-identical output."
        ),
    )


def _child_strategy_missing(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_STRATEGY_MISSING,
        path=e.child_path("strategy"),
        message=(
            f"{e.label}"
            f"child column {e.child_table}.{e.child_col} has no explicit strategy. "
            "The by-reference model would stream raw FK key values without "
            "the parent map resident. "
            f"Add 'strategy: {e.parent_strategy}' to the child column."
        ),
    )


def _child_strategy_mismatch(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_STRATEGY_MISMATCH,
        path=e.child_path("strategy"),
        message=(
            f"{e.label}"
            f"child strategy {e.child_cfg.get('strategy')!r} != parent strategy "
            f"{e.parent_strategy!r}. Self-masking requires identical strategies. "
            f"Update the child strategy to {e.parent_strategy!r}."
        ),
    )


def _parent_not_root(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_PARENT_NOT_ROOT,
        path=f"tables.{e.parent_table}.columns.{e.parent_col}",
        message=(
            f"{e.label}"
            f"{e.parent_table}.{e.parent_col} is itself an FK child "
            "column elsewhere (a same-key cascade). The oracle FK-"
            "resolves it via its own parent map rather than masking "
            "it directly, so self-masking this edge's child would "
            "diverge from the resolved value. Use run_pipeline or "
            "run_sequential for a multi-hop same-key cascade."
        ),
    )


def _parent_when_unsupported(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_PARENT_WHEN_UNSUPPORTED,
        path=e.parent_path("when"),
        message=(
            f"{e.label}"
            f"parent key column {e.parent_table}.{e.parent_col} has an "
            "effective 'when'. The oracle masks this key "
            "conditionally (rows failing 'when' stay raw); self-"
            "masking has no 'when' and always masks, so the child "
            "would diverge on the gated rows. Remove 'when' from the "
            "parent key column, or use run_pipeline / run_sequential."
        ),
    )


def _child_when_unsupported(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_WHEN_UNSUPPORTED,
        path=e.child_path("when"),
        message=(
            f"{e.label}"
            f"child key column {e.child_table}.{e.child_col} has an "
            "effective 'when'. The oracle ignores 'when' on an FK-"
            "resolved child column, but self-masking would honor it "
            "and mask only the rows that pass, diverging from the "
            "oracle on the gated rows. Remove 'when' from the child "
            "key column, or use run_pipeline / run_sequential."
        ),
    )


def _parent_provider_not_scalar(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_ENDPOINT_NOT_SCALAR,
        path=e.parent_path("provider"),
        message=(
            f"{e.label}"
            f"parent key column {e.parent_table}.{e.parent_col} declares "
            "a provider. A provider can route a hash-declared key "
            "through composite bundle generation instead of the "
            "plain hash handler, which this gate cannot verify is "
            "safe. Remove the provider from the parent key column, "
            "or use run_pipeline / run_sequential."
        ),
    )


def _child_provider_not_scalar(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_ENDPOINT_NOT_SCALAR,
        path=e.child_path("provider"),
        message=(
            f"{e.label}"
            f"child key column {e.child_table}.{e.child_col} declares a "
            "provider. A provider can route a hash-declared key "
            "through composite bundle generation instead of the "
            "plain hash handler, which this gate cannot verify is "
            "safe. Remove the provider from the child key column, or "
            "use run_pipeline / run_sequential."
        ),
    )


def _child_config_mismatch(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_CONFIG_MISMATCH,
        path=e.child_path("provider_config"),
        message=(
            f"{e.label}"
            f"child provider_config {_provider_config(e.child_cfg)!r} != parent "
            f"provider_config {_provider_config(e.parent_cfg)!r}. Both declare "
            f"strategy {e.parent_strategy!r} but with different "
            "value-affecting settings, so self-masking the child "
            "independently would not reproduce the parent's masked "
            "value. Make the child provider_config identical to the "
            "parent's."
        ),
    )


def _child_key_dtype_unprovable(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_KEY_DTYPE_UNPROVABLE,
        path=e.child_path("dtype"),
        message=(
            f"{e.label}"
            f"strategy {e.parent_strategy!r} is value-sensitive, so "
            "chunked self-masking reproduces the parent's masked "
            "value only when the parent and child FK key columns "
            "share a dtype family. That cannot be proven here: "
            f"parent dtype {e.parent_cfg.get('dtype')!r}, child dtype "
            f"{e.child_cfg.get('dtype')!r} (an undeclared dtype is not knowable "
            "at compile time, and the child's chunked run never "
            "sees the parent's data to check at runtime). Declare a "
            "matching 'dtype' on both FK key columns, or use "
            "run_pipeline / run_sequential (which normalize FK key "
            "equality via the parent map)."
        ),
    )


def _child_key_dtype_mismatch(e: FkEdge) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_CHILD_KEY_DTYPE_MISMATCH,
        path=e.child_path("dtype"),
        message=(
            f"{e.label}"
            f"child dtype {e.child_cfg.get('dtype')!r} is not the same family as "
            f"parent dtype {e.parent_cfg.get('dtype')!r}. Chunked self-masking "
            "hashes/truncates/FPEs the child's own value "
            "independently of the parent, with no parent-map lookup "
            "to normalize FK key equality; a dtype-family mismatch "
            "(e.g. parent int vs child float) means the child's raw "
            "value differs byte-for-byte from the parent's even when "
            "both represent the 'same' logical key, so self-masking "
            "cannot be guaranteed to reproduce the parent's masked "
            "value. Use run_pipeline or run_sequential instead, or "
            "normalize both columns to the same dtype before masking."
        ),
    )


def _dtype_not_cross_adapter_safe(
    e: FkEdge, endpoint_table: Any, endpoint_col: str, endpoint_dtype: Any
) -> PlanCompileError:
    return PlanCompileError(
        code=reasons.plan.CHUNKED_FK_KEY_DTYPE_NOT_CROSS_ADAPTER_SAFE,
        path=f"tables.{endpoint_table}.columns.{endpoint_col}.dtype",
        message=(
            f"{e.label}"
            f"{endpoint_table}.{endpoint_col} declares FK key "
            f"dtype {endpoint_dtype!r}, which is not in the "
            "exact cross-adapter-safe set for hash-only FK "
            "self-masking (string, large_string, any integer "
            "width, bool, date32, an IANA-tz timestamp, or a "
            "32/64/128-bit decimal with scale >= 0). hash is "
            "only proven cross-adapter byte-identical for "
            "these dtypes. Declare one of them, or use "
            "run_pipeline / run_sequential."
        ),
    )


def _provider_config(cfg: dict[str, Any]) -> Any:
    return cfg.get("provider_config") or {}


def _has_effective_when(col_entry: dict[str, Any]) -> bool:
    # Mirrors the compiler's `when` normalization (`plan/_seed_envelope.py:245`).
    when_raw = col_entry.get("when")
    return isinstance(when_raw, str) and bool(when_raw.strip())


def _namespace_required(e: FkEdge) -> bool:
    return e.parent_strategy in NAMESPACE_REQUIRING_STRATEGIES


def _value_sensitive(e: FkEdge) -> bool:
    return e.parent_strategy not in DTYPE_INVARIANT_STRATEGIES


def _dtype_undeclared(e: FkEdge) -> bool:
    return e.parent_cfg.get("dtype") is None or e.child_cfg.get("dtype") is None


def _dtype_families_differ(e: FkEdge) -> bool:
    # Runs only after `child_key_dtype_unprovable` passed, so both keys are present.
    return _dtype_family(e.parent_cfg["dtype"]) != _dtype_family(e.child_cfg["dtype"])


EDGE_CHECKS: tuple[EdgeCheck, ...] = (
    # (d) REMAP mints via parent_strategy(seed, ns, orphan_key), which equals
    # self-masking; WARN/FAIL/PRESERVE need the parent key set resident.
    EdgeCheck(
        "orphan_policy_not_remap",
        lambda e: e.orphan_policy != "remap",
        _orphan_policy_not_remap,
    ),
    # First cut: single-column FK edges only. Every later check indexes [0].
    EdgeCheck(
        "composite_unsupported",
        lambda e: len(e.parent_cols) != 1 or len(e.child_cols) != 1,
        _composite_unsupported,
    ),
    # (a) A single-strategy allowlist closes the strategy x representation x
    # substrate hole class and fails closed on any future chunk-safe addition.
    EdgeCheck(
        "parent_strategy_not_self_mask_safe",
        lambda e: e.parent_strategy != "hash",
        _parent_strategy_not_self_mask_safe,
    ),
    # (c1-c3) Only namespace-requiring strategies derive(job_seed, namespace, ...);
    # the parent-column namespace is the authoritative masking key.
    EdgeCheck(
        "parent_namespace_missing",
        lambda e: _namespace_required(e) and e.parent_ns is None,
        _parent_namespace_missing,
    ),
    EdgeCheck(
        "parent_namespace_mismatch",
        lambda e: _namespace_required(e) and e.rel_ns is not None and e.rel_ns != e.parent_ns,
        _parent_namespace_mismatch,
    ),
    # ColumnSeed.namespace comes from the column entry; FK auto-binding does not set it.
    EdgeCheck(
        "child_namespace_missing",
        lambda e: _namespace_required(e) and e.child_ns is None,
        _child_namespace_missing,
    ),
    EdgeCheck(
        "child_namespace_mismatch",
        lambda e: _namespace_required(e) and e.child_ns != e.parent_ns,
        _child_namespace_mismatch,
    ),
    # (b) The by-reference model (no child strategy) would stream raw FK keys.
    EdgeCheck(
        "child_strategy_missing",
        lambda e: e.child_cfg.get("strategy") is None,
        _child_strategy_missing,
    ),
    EdgeCheck(
        "child_strategy_mismatch",
        lambda e: e.child_cfg.get("strategy") != e.parent_strategy,
        _child_strategy_mismatch,
    ),
    # (8) A parent column that is a component of any child endpoint is FK-resolved
    # by the oracle, not masked by its strategy. Component membership also
    # catches a parent that is one column of an upstream composite child.
    EdgeCheck(
        "parent_not_root",
        lambda e: (str(e.parent_table), str(e.parent_col)) in e.child_endpoint_columns,
        _parent_not_root,
    ),
    # (9/10) Self-masking has no `when`, so a row-gated key diverges on gated rows.
    EdgeCheck(
        "parent_when_unsupported",
        lambda e: _has_effective_when(e.parent_cfg),
        _parent_when_unsupported,
    ),
    EdgeCheck(
        "child_when_unsupported",
        lambda e: _has_effective_when(e.child_cfg),
        _child_when_unsupported,
    ),
    # (11) A provider can reroute a hash key through composite bundle generation.
    EdgeCheck(
        "parent_provider_not_scalar",
        lambda e: e.parent_cfg.get("provider") is not None,
        _parent_provider_not_scalar,
    ),
    EdgeCheck(
        "child_provider_not_scalar",
        lambda e: e.child_cfg.get("provider") is not None,
        _child_provider_not_scalar,
    ),
    # (e) Same strategy name with different settings masks to different bytes.
    EdgeCheck(
        "child_config_mismatch",
        lambda e: _provider_config(e.parent_cfg) != _provider_config(e.child_cfg),
        _child_config_mismatch,
    ),
    # (f) A value-sensitive strategy reproduces the parent's bytes only for a
    # shared dtype family; an undeclared dtype is unprovable, so fail closed.
    EdgeCheck(
        "child_key_dtype_unprovable",
        lambda e: _value_sensitive(e) and _dtype_undeclared(e),
        _child_key_dtype_unprovable,
    ),
    EdgeCheck(
        "child_key_dtype_mismatch",
        lambda e: _value_sensitive(e) and _dtype_families_differ(e),
        _child_key_dtype_mismatch,
    ),
    # (12) Declared stage of the exact cross-adapter-safe dtype set, narrower
    # than the family match; each endpoint is checked on its own, parent first.
    EdgeCheck(
        "parent_key_dtype_not_cross_adapter_safe",
        lambda e: (
            _value_sensitive(e)
            and not declared_fk_hash_dtype_is_safe(str(e.parent_cfg.get("dtype")))
        ),
        lambda e: _dtype_not_cross_adapter_safe(
            e, e.parent_table, e.parent_col, e.parent_cfg.get("dtype")
        ),
    ),
    EdgeCheck(
        "child_key_dtype_not_cross_adapter_safe",
        lambda e: (
            _value_sensitive(e)
            and not declared_fk_hash_dtype_is_safe(str(e.child_cfg.get("dtype")))
        ),
        lambda e: _dtype_not_cross_adapter_safe(
            e, e.child_table, e.child_col, e.child_cfg.get("dtype")
        ),
    ),
)
