"""joint_mask strategy (SP-08 / P5.S.joint_mask.1+2): compound reference-tuple masking.

Replaces a set of logically coupled columns (e.g. zip + city + state) with a
consistent tuple drawn from a reference table. Consistency is guaranteed because
the output IS a real reference-table row -- there is no per-column replacement
that could produce a (city, state) pair that does not exist in the source data.

Two modes:

  Mask mode (``mode="mask"``)
    Derives a deterministic reference-table row from the ``key_by`` column value
    via HMAC-SHA256(job_seed, key_value). Same key + same seed -> same row.
    Uses ``ReferenceTable.keyed_row`` per the SP-06 contract.

  Gen mode (``mode="gen"``)
    Draws rows via numpy.default_rng seeded from the job seed, independently of
    any source column value. Deterministic across runs for the same seed and
    DataFrame length.

Shipped reference tables (SP-08b additions):
  ``ndc_labeler_drug_strength`` -- NDC drug labeler/name/strength/dosage-form tuples.
      Source: FDA NDC Database (public domain); abbreviated seed set. Operators may
      swap in a full FDA export via the ``customer:`` path prefix.
  ``mcc_category_description``  -- MCC merchant category code/category/description tuples.
      Source: ISO 18245 (public standard); abbreviated seed set.

Customer-provided tables (SP-08b):
  Set ``reference: customer:/path/to/table.parquet`` to load a custom Parquet file.
  The file must have an ``id`` column (int64) plus the domain columns that ``columns``
  references. The swap-in hook follows the same schema convention as shipped tables.
  Determinism caveat: adding/removing rows changes ``row_count`` and shifts the
  modular HMAC index -- the same ``key_by`` value will select a different row.
  Document table version in your pipeline manifest if longitudinal consistency matters.

Pattern: HMAC-SHA256-keyed row derivation via ReferenceTable.keyed_row
  (RFC 2104, https://datatracker.ietf.org/doc/html/rfc2104).
  Reuses decoy_engine.reference_tables (SP-06 / P5.INFRA.3).

SP-06 keyed-access cross-version caveat (inherited from ReferenceTable.keyed_row):
  keyed_row selects rows from the id-sorted table at position ``HMAC(...) % row_count``.
  This mapping is deterministic WITHIN a table version but NOT stable ACROSS versions
  that differ in row_count: adding or removing rows shifts the modular index. Users
  must not assume that the same key_by value selects the same reference row after a
  table update. See decoy_engine.reference_tables._types.ReferenceTable.keyed_row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.reference_tables import ReferenceTable, load_table

# DE-02: source label for the secret-derived HMAC key of the mask-mode keyed
# row selection. `derive(mask_key, namespace, _KEYED_ROW_SOURCE)` yields a
# per-(secret, column) 32-byte key so the reference-row mapping depends on the
# run secret (Codex BLOCKER 1) instead of a public constant.
_KEYED_ROW_SOURCE = b"joint_mask/keyed_row/v1"
_DEFAULT_NAMESPACE = "joint_mask"

_LOG = logging.getLogger(__name__)

# Shipped table names that joint_mask can use out of the box.
# SP-08b: ndc_labeler_drug_strength + mcc_category_description added.
_KNOWN_TABLES = frozenset(
    {
        "us_zip5_city_state",
        "vehicle_make_model_year",
        "ndc_labeler_drug_strength",
        "mcc_category_description",
    }
)

# Prefix that identifies a customer-provided reference table path (SP-08b).
_CUSTOMER_PREFIX = "customer:"


@dataclass(frozen=True)
class JointMaskConfig:
    """Configuration for a joint_mask operation.

    Attributes:
        columns: Target output columns (must all exist in the reference table).
        reference: Name of the reference table (shipped or customer-provided).
        key_by: Source column whose value drives HMAC-keyed row selection in
            mask mode. Not used in gen mode.

    SP-06 keyed_row cross-version caveat:
        ``keyed_row`` selects a row by ``HMAC(...) % row_count`` on the id-sorted
        table. Deterministic WITHIN a table version. NOT stable if ``row_count``
        changes (rows added/removed). Do not assume the same ``key_by`` value
        maps to the same row after a reference-table update.
    """

    columns: tuple[str, ...]
    reference: str
    key_by: str
    _table: ReferenceTable = field(compare=False, repr=False, hash=False)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> JointMaskConfig:
        """Parse and validate a config dict; raise PlanCompileError on failure.

        Supports shipped table names (e.g. ``"us_zip5_city_state"``) and
        customer-provided paths (e.g. ``"customer:/data/my_table.parquet"``).

        Args:
            cfg: Raw config dict with keys ``columns``, ``reference``, ``key_by``.

        Raises:
            PlanCompileError: Missing or invalid fields detected at execution
                time, before any data is mutated (fail-closed).
        """
        validate_joint_mask_config(cfg)
        name = cfg["reference"]
        # load_table handles the customer: prefix internally (SP-08b).
        tbl = load_table(name)
        return cls(
            columns=tuple(cfg["columns"]),
            reference=name,
            key_by=cfg["key_by"],
            _table=tbl,
        )

    @property
    def table(self) -> ReferenceTable:
        """The loaded reference table."""
        return self._table


def validate_joint_mask_config(cfg: dict[str, Any]) -> None:
    """Validate a joint_mask config dict; raise PlanCompileError on any failure.

    Checks:
      - ``columns`` is present and non-empty.
      - ``reference`` is present and names a loadable table.
      - ``key_by`` is present.
      - Every column in ``columns`` exists in the reference table.

    This is an execution-time, pre-mutation check: it runs before any data
    is touched so invalid configs fail closed (fast and loud).

    Args:
        cfg: Raw config dict.

    Raises:
        PlanCompileError: Any validation failure.
    """
    if "columns" not in cfg or not cfg["columns"]:
        raise PlanCompileError(
            code="joint_mask_columns_missing",
            path="joint_masks.columns",
            message=(
                "'columns' is required for joint_mask and must be a non-empty list. "
                "Example: columns: [zip, city, state]"
            ),
        )

    if "key_by" not in cfg:
        raise PlanCompileError(
            code="joint_mask_key_by_missing",
            path="joint_masks.key_by",
            message=(
                "'key_by' is required for joint_mask (the source column used for "
                "HMAC-keyed row selection in mask mode). "
                "Example: key_by: patient_id"
            ),
        )

    ref_name = cfg.get("reference")
    if not ref_name:
        raise PlanCompileError(
            code="joint_mask_reference_missing",
            path="joint_masks.reference",
            message=(
                "'reference' is required for joint_mask and must name a shipped or "
                "customer-provided reference table. "
                "Example: reference: us_zip5_city_state"
            ),
        )

    try:
        tbl = load_table(ref_name)
    except FileNotFoundError:
        raise PlanCompileError(
            code="joint_mask_reference_not_found",
            path="joint_masks.reference",
            message=(
                f"reference table {ref_name!r} not found. "
                f"Known shipped tables: {sorted(_KNOWN_TABLES)}. "
                f"To use a customer-provided table, set reference: customer:/path/to/file.parquet."
            ),
        )
    except ValueError as exc:
        raise PlanCompileError(
            code="joint_mask_reference_invalid",
            path="joint_masks.reference",
            message=f"reference table {ref_name!r} failed to load: {exc}",
        )

    ref_cols = set(tbl.column_names) - {"id"}
    for col in cfg["columns"]:
        if col not in ref_cols:
            raise PlanCompileError(
                code="joint_mask_column_not_in_reference",
                path=f"joint_masks.columns.{col}",
                message=(
                    f"column {col!r} is not in reference table {ref_name!r}. "
                    f"Available columns: {sorted(ref_cols)}"
                ),
            )


# ── Core apply functions ──────────────────────────────────────────────────────


def apply_joint_mask(
    df: pd.DataFrame,
    config: JointMaskConfig,
    *,
    mode: str = "mask",
    job_seed: bytes,
    namespace: str | None = None,
) -> pd.DataFrame:
    """Apply joint_mask to ``df``, writing all target columns in one pass.

    Does NOT mutate the input DataFrame. Returns a new DataFrame with the
    target columns replaced by consistent reference-table rows.

    Args:
        df: Source DataFrame. Must contain ``config.key_by`` (mask mode only).
        config: Parsed + validated :class:`JointMaskConfig`.
        mode: ``"mask"`` (keyed HMAC) or ``"gen"`` (seeded random).
        job_seed: 32-byte entropy input. Same seed -> same output for a given
            input (mask mode) or DataFrame length (gen mode).

    Returns:
        A copy of ``df`` with ``config.columns`` replaced by reference-table rows.
    """
    result = df.copy()

    if mode == "mask":
        rows = _pick_rows_mask(df, config, job_seed, namespace=namespace)
    elif mode == "gen":
        rows = _pick_rows_gen(len(df), config, job_seed)
    else:
        raise ValueError(f"joint_mask: unsupported mode {mode!r}. Use 'mask' or 'gen'.")

    for col in config.columns:
        result[col] = [r.get(col) for r in rows]

    return result


def _pick_rows_mask(
    df: pd.DataFrame,
    config: JointMaskConfig,
    job_seed: bytes,
    *,
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Pick one reference row per DataFrame row using HMAC-keyed selection.

    Null key_by values are handled by falling back to a seeded random row
    (the same fallback a null value would need since there is no key to
    HMAC). This preserves the non-null row count and avoids crashes.

    DE-02: `job_seed` here is the keyed-mask IKM (`ctx.mask_key`). The reference
    row is selected under an HMAC key derived from it
    (`derive(mask_key, namespace, "joint_mask/keyed_row/v1")`), so the
    real-value -> reference-row mapping depends on the run secret and is not
    reversible off the public reference-table salt (Codex BLOCKER 1). The
    null-key fallback RNG also seeds off this IKM.

    Pattern: HMAC-SHA256 via ReferenceTable.keyed_row (RFC 2104).
    SP-06 cross-version caveat: modular index shifts if row_count changes.
    """
    tbl = config.table
    key_col = df[config.key_by] if config.key_by in df.columns else pd.Series([None] * len(df))
    hmac_key = derive(job_seed, namespace or _DEFAULT_NAMESPACE, _KEYED_ROW_SOURCE)

    rows: list[dict[str, Any]] = []
    # Seeded RNG for null-key fallback rows only.
    rng = np.random.default_rng(int.from_bytes(job_seed[:8], "big"))
    for val in key_col:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            # Null key: fall back to a seeded random row (no usable key).
            idx = int(rng.integers(0, tbl.row_count))
            rows.append(tbl.row(idx))
        else:
            # Non-null: keyed_row selects under the secret-derived HMAC key.
            rows.append(tbl.keyed_row(str(val), hmac_key=hmac_key))

    return rows


def _pick_rows_gen(
    n: int,
    config: JointMaskConfig,
    job_seed: bytes,
) -> list[dict[str, Any]]:
    """Pick ``n`` reference rows via numpy.default_rng seeded from ``job_seed``.

    Deterministic: same job_seed + same n -> same sequence of rows.
    Independent of source column values (gen mode has no source to key from).
    """
    tbl = config.table
    seed_int = int.from_bytes(job_seed[:8], "big")
    rng = np.random.default_rng(seed_int)
    indices = rng.integers(0, tbl.row_count, size=n)
    return [tbl.row(int(idx)) for idx in indices]
