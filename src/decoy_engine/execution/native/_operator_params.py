"""Resolved per-operator parameters, shared by both native routes.

Each native operator needs the same few facts before it can call its kernel: config defaults,
coercions and a namespace. One frozen dataclass per operator holds them resolved, and
`resolve_operator_params` is the only place a default is written, so the unified full-frame
route (which resolves when it binds a node) and the chunked route (which resolves once per
table) cannot drift apart.

The resolver resolves; it never validates or declines. Declining is admission's job and the
chunked entry's tolerant categorical preparation, so moving a default here cannot change which
route a table takes. It reads no key material: a keyed operator's secret stays in the run
context and only its namespace lands here.

A leaf module: it imports `_categorical_prepared`, the date_shift defaults and the operator
registry, never the kernels or anything under `execution.physical`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution._strategies._faker_positional import faker_selection_namespace
from decoy_engine.execution._strategies._text_redact import _DEFAULT_TOKEN
from decoy_engine.execution.native._categorical_prepared import PreparedCategorical
from decoy_engine.execution.native._date_shift_ext import DEFAULT_MAX_DAYS, DEFAULT_MIN_DAYS

__all__ = [
    "BucketPerturbParams",
    "CategoricalParams",
    "DateShiftParams",
    "FakerParams",
    "GroupKeyParams",
    "HashParams",
    "OperatorParams",
    "PassthroughParams",
    "RedactParams",
    "TextRedactParams",
    "TruncateParams",
    "is_positional_faker_seed",
    "positional_faker_params",
    "resolve_operator_params",
    "resolve_params_by_column",
]


@dataclass(frozen=True)
class PassthroughParams:
    pass


@dataclass(frozen=True)
class RedactParams:
    redact_with: Any


@dataclass(frozen=True)
class TruncateParams:
    length: int
    keep: str
    mask_char: Any


@dataclass(frozen=True)
class TextRedactParams:
    # None runs every detector. The empty-list-means-all rule is applied once, by the resolver.
    detectors: tuple[str, ...] | None
    token: str
    label_token: bool


@dataclass(frozen=True)
class HashParams:
    namespace: str | None
    truncate: Any


@dataclass(frozen=True)
class FakerParams:
    # The CONFIGURED namespace: the pool identity and the deterministic draw read it.
    namespace: str | None
    # A non-deterministic REUSE column draws by row position, keyed on `job_seed`, from the
    # selection namespace (the configured one, else the per-table default).
    positional: bool = False
    selection_namespace: str | None = None


@dataclass(frozen=True)
class CategoricalParams:
    prepared: PreparedCategorical
    namespace: str | None


@dataclass(frozen=True)
class BucketPerturbParams:
    bucket: str
    date_format: str
    namespace: str | None


@dataclass(frozen=True)
class GroupKeyParams:
    group_by: str
    length: int
    prefix: str
    # The oracle ignores the plan namespace and keys on the TARGET column's own name.
    namespace: str


@dataclass(frozen=True)
class DateShiftParams:
    date_format: str
    min_days: int
    max_days: int
    namespace: str | None


OperatorParams = (
    PassthroughParams
    | RedactParams
    | TruncateParams
    | TextRedactParams
    | HashParams
    | FakerParams
    | CategoricalParams
    | BucketPerturbParams
    | GroupKeyParams
    | DateShiftParams
)


def _resolve_truncate_keep(cfg: Mapping[str, Any]) -> str:
    """Resolve the legacy `from_end` key to `keep` the way `TruncateHandler.run`
    does: an explicit `keep` wins; otherwise `from_end` maps tail/head.

    This is only the from_end->keep RESOLUTION, not the config VALIDATION: an
    invalid `keep` is rejected upstream at admission (Task 2.6's
    `truncate_config_rejection`, which reroutes the table before it reaches here)
    and again by `native_truncate` itself, so a bad value never reaches this
    admitted-only path.
    """
    keep = cfg.get("keep")
    if keep is not None:
        return keep
    return "tail" if bool(cfg.get("from_end", False)) else "head"


def resolve_operator_params(
    strategy: str,
    *,
    target: str,
    provider_config: Mapping[str, Any],
    namespace: str | None,
    prepared_categorical: PreparedCategorical | None = None,
) -> OperatorParams:
    """The resolved parameters of one admitted column.

    `target` is the column the operator writes (group_key's namespace is derived from it).
    `prepared_categorical` is the artifact the caller already holds for a categorical column:
    unified binding takes it from `prepare_categorical`, the chunked route from
    `_prepared_categoricals`. Its absence is a wiring bug, never an input condition.
    """
    cfg = provider_config
    if strategy == "passthrough":
        return PassthroughParams()
    if strategy == "redact":
        return RedactParams(cfg.get("redact_with", "REDACTED"))
    if strategy == "truncate":
        # Admission proved `length` a positive int; `native_truncate` re-validates it anyway.
        length = cfg.get("length")
        return TruncateParams(
            length if isinstance(length, int) else 0,
            _resolve_truncate_keep(cfg),
            cfg.get("mask_char"),
        )
    if strategy == "text_redact":
        # The oracle's normalization (`TextRedactHandler.run`): an empty list means every
        # detector, never none, and a value that is not a list or tuple means every detector
        # too (admission excludes it, so only the all-detectors reading is reachable).
        raw = cfg.get("detectors")
        detectors = tuple(str(d) for d in raw) or None if isinstance(raw, (list, tuple)) else None
        return TextRedactParams(
            detectors, cfg.get("token", _DEFAULT_TOKEN), bool(cfg.get("label_token", False))
        )
    if strategy == "hash":
        return HashParams(namespace, cfg.get("truncate"))
    if strategy == "faker":
        return FakerParams(namespace)
    if strategy == "categorical":
        if prepared_categorical is None:  # pragma: no cover - callers hold it by admission
            raise AssertionError(
                f"categorical column {target!r} reached the parameter resolver with no "
                "prepared categories; the caller must pass the prepare_categorical artifact."
            )
        return CategoricalParams(prepared_categorical, namespace)
    if strategy == "bucket_perturb":
        return BucketPerturbParams(str(cfg.get("bucket", "month")), cfg["date_format"], namespace)
    if strategy == "group_key":
        return GroupKeyParams(
            cfg["group_by"],
            cfg.get("length", 16),
            # The oracle and the full-frame binding both str() the prefix: None -> "None".
            str(cfg.get("prefix", "")),
            f"group_key/{target}",
        )
    if strategy == "date_shift":
        return DateShiftParams(
            cfg["date_format"],
            cfg.get("min_days", DEFAULT_MIN_DAYS),
            cfg.get("max_days", DEFAULT_MAX_DAYS),
            namespace,
        )
    raise AssertionError(f"no native operator parameters for strategy {strategy!r}")


def is_positional_faker_seed(seed: Any) -> bool:
    """The oracle's gate for the position-keyed draw: a non-deterministic REUSE faker."""
    return bool(
        seed.strategy == "faker" and not seed.deterministic and seed.cardinality_mode == "reuse"
    )


def positional_faker_params(seed: Any, *, table: str | None, column: str) -> FakerParams:
    """The parameters of a position-keyed faker column: the oracle's own default-namespace
    function, so every route that builds them cannot spell the selection namespace
    differently."""
    if table is None:  # pragma: no cover - every caller knows its table
        raise AssertionError(
            f"positional faker column {column!r} reached the parameter resolver with no table; "
            "its default selection namespace is keyed on the table."
        )
    return FakerParams(
        seed.namespace,
        positional=True,
        selection_namespace=faker_selection_namespace(table, column, seed.namespace),
    )


def resolve_params_by_column(
    col_seed_by_name: Mapping[str, Any],
    prepared_categoricals: Mapping[str, PreparedCategorical],
    *,
    excluded: frozenset[str],
    table: str | None = None,
) -> dict[str, OperatorParams]:
    """Parameters for every configured column of one table, resolved once for the whole run
    (defaults, coercions and namespaces never change between chunks).

    A column is left out when it is `excluded` (unconfigured passthrough, stored index), when
    its strategy is not a native operator, or when it is a categorical the chunked entry did
    not prepare. The route then fails closed on the missing entry, as it did when it looked
    these up per chunk. `table` keys the default selection namespace of a position-keyed faker.
    """
    params: dict[str, OperatorParams] = {}
    for name, seed in col_seed_by_name.items():
        if name in excluded or seed.strategy not in OPERATORS:
            continue
        if seed.strategy == "categorical" and name not in prepared_categoricals:
            continue
        if is_positional_faker_seed(seed):
            params[name] = positional_faker_params(seed, table=table, column=name)
            continue
        params[name] = resolve_operator_params(
            seed.strategy,
            target=name,
            provider_config=provider_config_to_dict(seed.provider_config),
            namespace=seed.namespace,
            prepared_categorical=prepared_categoricals.get(name),
        )
    return params
