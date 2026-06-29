"""group_key column-level strategy (SP-10c / P5.P.group_key).

Derives a deterministic, consistent hex identifier for every row sharing the
same group_by column value. All rows whose group_by value equals ``V`` receive
the same output key; rows with different group_by values receive different keys
(under the seed and namespace used for derivation).

This closes the household-coherence gap (Mask M4): a ``group_key`` column on
a customer table (group_by = "household_id") gives every member of the same
household an identical stable synthetic key, enabling join-safe cross-table
linking without leaking the real household identifier.

Pattern: reuses the engine's HKDF-SHA256 + HMAC-SHA256 keyed derivation from
``decoy_engine.determinism._derive.derive()``. This is the same
"hash-for-joinability" primitive already used throughout the engine for FK-
preserving deterministic masking (documented in docs/determinism.md).

    derive(seed, namespace, source_bytes) -> 32 bytes

where:

  seed            = the 8-byte job seed from the plan
  namespace       = "group_key/<column_name>" (per-column isolation)
  source_bytes    = group_by column value encoded as UTF-8

The output bytes are hex-encoded and optionally prefixed. The HKDF step binds
the derivation to a per-column context; the HMAC step mixes the per-group
source value.

References:
  RFC 5869 (HKDF-SHA256): https://datatracker.ietf.org/doc/html/rfc5869
  RFC 2104 (HMAC-SHA256): https://datatracker.ietf.org/doc/html/rfc2104
  Engine determinism contract: decoy_engine.determinism._derive.derive

Security design:
  No custom crypto. All keyed derivation routes through the engine's
  ``derive()`` function (RFC 5869 HKDF-SHA256 extract + RFC 2104 HMAC-SHA256)
  from ``decoy_engine.determinism._derive``. This is the canonical primitive
  for all deterministic masking in the engine.

  The output set is a CLOSED function of (seed, namespace, group_by value):
  no eval(), no exec(), no dynamic code.

Determinism:
  Same 8-byte seed + same namespace + same group_by value -> byte-identical
  key string on every run (subject to SEED_PROTOCOL_VERSION, per the engine
  compatibility contract).

Validation timing:
  group_by + length: config-parse time (GroupKeyConfig.from_dict).
  group_by column existence: plan-compile time (check_group_key_refs).
  Validation never mutates (per engine rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from decoy_engine.determinism._derive import derive
from decoy_engine.plan._errors import PlanCompileError

# Allowed length range (hex characters; must be even so byte count is integer).
_MIN_LENGTH = 8
_MAX_LENGTH = 64


@dataclass(frozen=True)
class GroupKeyConfig:
    """Configuration for a group_key column.

    Attributes:
        group_by: Name of the column whose value defines the group.
                  Rows with the same value share the same derived key.
        length:   Number of hex characters in the output key (default 16;
                  must be even and in [8, 64]).
        prefix:   Constant string prepended to every output key (default "").
    """

    group_by: str
    length: int
    prefix: str

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> GroupKeyConfig:
        """Parse and validate a group_key config dict.

        group_by and length are validated at parse time. Column-ref existence
        is validated at plan-compile time via check_group_key_refs.

        Args:
            cfg: Config dict with required key ``group_by``.

        Raises:
            PlanCompileError: ``group_by`` is missing or empty.
            PlanCompileError: ``length`` is not even or not in [8, 64].
        """
        group_by = cfg.get("group_by")
        if not group_by:
            raise PlanCompileError(
                code="group_key_group_by_missing",
                path="provider_config.group_by",
                message=(
                    "'group_by' is required for the group_key strategy. "
                    "Provide the name of the column that partitions rows into "
                    "groups."
                ),
            )

        length = int(cfg.get("length", 16))
        if length % 2 != 0:
            raise PlanCompileError(
                code="group_key_length_odd",
                path="provider_config.length",
                message=(
                    f"'length' must be even (hex string of n bytes = 2n characters); got {length}."
                ),
            )
        if length < _MIN_LENGTH or length > _MAX_LENGTH:
            raise PlanCompileError(
                code="group_key_length_out_of_range",
                path="provider_config.length",
                message=(f"'length' must be in [{_MIN_LENGTH}, {_MAX_LENGTH}]; got {length}."),
            )

        prefix = str(cfg.get("prefix", ""))

        return cls(group_by=str(group_by), length=length, prefix=prefix)


def apply_group_key(
    config: GroupKeyConfig,
    df: pd.DataFrame,
    seed: bytes,
    namespace: str,
) -> list[str]:
    """Derive a consistent key for each row based on its group_by column value.

    Pattern: HKDF-SHA256 + HMAC-SHA256 keyed derivation via
    ``decoy_engine.determinism._derive.derive()`` (RFC 5869 / RFC 2104).
    The same primitive the engine uses for FK-preserving deterministic masking.

    Each unique group_by value maps to a unique key (with overwhelming
    probability under the 32-byte HMAC output space). All rows sharing a
    group_by value receive the same key string.

    Args:
        config:    Parsed GroupKeyConfig.
        df:        DataFrame containing the group_by column.
        seed:      8-byte job seed (from plan.seed_envelope.job_seed).
        namespace: Per-column namespace string (e.g. "group_key/<col_name>")
                   used as the HKDF info parameter to isolate this column
                   from other derive() calls in the same job.

    Returns:
        List of key strings aligned to df rows. Each string is ``config.prefix``
        followed by ``config.length`` lowercase hex characters.
    """
    group_col = config.group_by
    n_bytes = config.length // 2  # hex chars -> bytes

    # Cache: avoid re-deriving for the same group value in the same call.
    key_cache: dict[Any, str] = {}

    result: list[str] = []
    for raw_val in df[group_col]:
        if raw_val not in key_cache:
            source = str(raw_val).encode("utf-8")
            raw_bytes = derive(seed, namespace, source)
            hex_key = raw_bytes[:n_bytes].hex()
            key_cache[raw_val] = config.prefix + hex_key
        result.append(key_cache[raw_val])

    return result
