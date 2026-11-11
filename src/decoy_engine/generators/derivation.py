"""Generation seed derivation: strategy fingerprint plus column identity.

A column's draw stream derives from the job seed (or master key), the
fingerprint of its strategy config, and its identity (table name plus
column name). The fingerprint alone is not enough: two columns with the
same settings would otherwise share one stream and emit identical values
in every row. Identity does not depend on column order or row count, so
adding a column or growing ``row_count`` leaves other columns unchanged.

This module exposes:

    strategy_config_fingerprint(column_config) -> str
        Stable hex digest of the parts of column_config that actually
        affect output (strategy type + strategy-specific knobs, excluding
        display name and null probability). Snapshotted into the evidence
        manifest so verifiers can prove "same fingerprint => same
        generation contract"; also one input to the column root label.

    GenDeriveContext.for_column(derive_key, column_config, fallback_seed)
        Per-column generation derivation (F2/F3, v6). Resolves one 32-byte
        column root (keyed path: full derive_key("gen:" + fingerprint + ":" +
        identity), no longer truncated to 4 bytes), then exposes disjoint per-RNG-family
        and per-row sub-derivations. Honors ``determinism: fresh`` via
        os.urandom.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Callable
from typing import Any

from decoy_engine import fileaccess
from decoy_engine.determinism import SEED_PROTOCOL_VERSION
from decoy_engine.fileaccess import FileAccess, FileRole, entry_point

# Fields that are intentionally excluded from the fingerprint. ``name``
# is the rename surface R3.10 is severing. ``null_probability`` is a
# post-generation filter that doesn't change the underlying value the
# generator emits for a given row. ``determinism`` flips between stable
# and fresh paths but the *config* itself is the same set of inputs;
# determinism is recorded as a separate evidence field. ``pooled`` (GP2,
# Codex round-2 spec A) is a BUILD-STRATEGY toggle, not a value-affecting
# strategy knob: a column with `pooled` omitted or `pooled: false` must
# keep the exact pre-GP2 per-row bytes AND seed root, which requires the
# field to never touch the fingerprint (hashing it would shift the root
# for every existing faker column the day GP2 lands, whether or not it
# ever pools).
_EXCLUDED_FROM_FINGERPRINT = frozenset(
    {
        "name",
        "null_probability",
        "determinism",
        "pooled",
        # GEN-UNIQUE: a build-strategy toggle like `pooled`. A unique column keeps its seed root, so
        # its first-draw rows equal the plain column's values (invariant U5).
        "unique",
        # GEN-TYPES E2: how a value is printed, not which value is drawn; adding it must not
        # re-seed the column (the values before the cast stay equal).
        "output_type",
        "_table",
    }
)

# Config key that carries the owning table's name into `for_column`. Set once
# at each generation entry point via `with_table`; it is part of the column
# identity, not of the strategy fingerprint.
TABLE_KEY = "_table"


def with_table(column_config: dict[str, Any], table: str) -> dict[str, Any]:
    """Copy of `column_config` carrying its table name for seed derivation."""
    return {**column_config, TABLE_KEY: table}


def column_identity(column_config: dict[str, Any]) -> str:
    """Canonical `(table, column)` identity string mixed into the column root.

    JSON-encoded so a name containing a separator cannot collide with a
    different (table, column) pair. Raises when the table is missing: a
    route that skipped `with_table` would otherwise share streams across
    tables without any signal.
    """
    if TABLE_KEY not in column_config:
        raise ValueError(
            "column_identity: column config carries no table; build it with with_table() "
            f"(column={column_config.get('name', 'unnamed_column')!r})"
        )
    return json.dumps(
        [column_config[TABLE_KEY], column_config.get("name", "unnamed_column")],
        separators=(",", ":"),
    )


# ``snapshot_file`` (statistical columns) is a FILESYSTEM PATH, not a
# generation input. Hashing the path string couples the derived seed to a
# UI/ops-mutable location: renaming or date-stamping a snapshot silently
# shifts synthetic output, and a harness that stages the snapshot in a
# fresh temp dir per run gets a different seed every process (breaking the
# cross-process determinism gate). We substitute the path with the SHA-256
# of the file's CONTENT before fingerprinting, so the same distribution at
# ANY path yields the same seed, and two genuinely different snapshots
# yield different seeds. Excluding it outright (dropping it) would instead
# collide two different snapshots onto one seed -- the wrong direction.
_SNAPSHOT_FILE_KEY = "snapshot_file"


_UNREADABLE = (
    "strategy_config_fingerprint: snapshot_file {!r} is missing or unreadable ({}); cannot "
    "derive a stable content-based seed. Fix the path or file rather than falling back "
    "to the path string (which would silently change synthetic output)."
)


def _snapshot_content_digest(path: str, field_key: str = "snapshot_file") -> str:
    """SHA-256 hex of a snapshot file's raw bytes, for use in the fingerprint.

    Content, not path: the same snapshot at any location fingerprints
    identically. Raises RuntimeError (fail loud) when the file is missing
    or unreadable rather than silently falling back to the path string,
    which would reintroduce the path-coupling bug.
    """
    key = fileaccess.position_key(field_key)
    shown = fileaccess.shown(path, key)
    failure: str | None = None
    try:
        data = fileaccess.read_all(FileRole.SNAPSHOT, path, key)
    except OSError as exc:
        if fileaccess.keeps_class(exc):
            raise
        if fileaccess.is_ambient():
            raise RuntimeError(_UNREADABLE.format(shown, f"{type(exc).__name__}: {exc}")) from exc
        failure = f"{type(exc).__name__}: {fileaccess.redact_locators(str(exc), [(path, shown)])}"
    if failure is not None:
        # Strict: raised after the handler closes, so the library error is neither cause nor
        # context (its text can name the file).
        raise RuntimeError(_UNREADABLE.format(shown, failure))
    return "sha256:" + hashlib.sha256(data).hexdigest()


@entry_point
def strategy_config_fingerprint(
    column_config: dict[str, Any],
    *,
    snapshot_content_digest: str | None = None,
    file_access: FileAccess | None = None,
) -> str:
    """SHA-256 hex of canonical JSON of the strategy-relevant config.

    Canonical JSON: sorted keys, no whitespace, default ``ensure_ascii``
    so the bytes are identical regardless of locale or Python build.
    Returns the full 64-char hex; callers that want a shorter label can
    truncate.

    ``snapshot_file`` is fingerprinted by CONTENT, not path (see
    ``_SNAPSHOT_FILE_KEY``): its value in the payload is replaced with the
    SHA-256 of the referenced file's bytes so the seed follows the
    distribution, not the filesystem location.

    ``snapshot_content_digest``, when given, is used verbatim instead of
    reading ``snapshot_file`` from disk (same ``"sha256:<hex>"`` shape
    ``_snapshot_content_digest`` returns). DPS Scope B (guide section
    4.7/4.8): a Plan-pinned caller already knows this file's content
    digest from compile time -- `StatisticalSpec.snapshot_digest` -- and
    must not reopen the path merely to rederive a value it already has
    (defect F4). Direct/standalone callers with no Plan (the tests in
    `test_gen_derive_context.py`/`test_r3_10_generation_key_contract.py`)
    omit this and keep today's read-the-file behavior unchanged.
    """
    payload = {
        k: v for k, v in (column_config or {}).items() if k not in _EXCLUDED_FROM_FINGERPRINT
    }
    snapshot_file = payload.get(_SNAPSHOT_FILE_KEY)
    if snapshot_file:
        payload[_SNAPSHOT_FILE_KEY] = (
            snapshot_content_digest
            if snapshot_content_digest is not None
            else _snapshot_content_digest(str(snapshot_file))
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# RNG families a generated column may drive. Each gets a disjoint sub-key so
# the three never share one int (F3) and adjacent columns never correlate.
#   py    -> random.Random(...)
#   np    -> numpy.random.default_rng(...)
#   faker -> Faker.seed_instance(...)
#   faker_unique -> Faker.seed_instance(...) for the retry attempts of a unique faker row
#   unique_pair  -> random.Random(...) sampling the pair index of a `unique_together` entry
#   dist  -> numpy.random.default_rng(...) drawing a `distribution` column. Its own family, never
#            "np": the null mask draws default_rng(base_int("np")).random(n) on the same column and
#            `null_probability` is not in the fingerprint, so a shared family would hand the mask
#            and the value the same uniforms.
GEN_FAMILIES = ("py", "np", "faker", "faker_unique", "unique_pair", "dist")


def _gen_hmac(key: bytes, label: bytes, *, extra: bytes = b"") -> bytes:
    """Version-mixed, length-prefixed HMAC-SHA256.

    Mirrors `determinism._derive.derive_source` byte-for-byte in shape:
    `version_byte || len(label) || label || len(extra) || extra`. Mixing
    SEED_PROTOCOL_VERSION makes generation output version-aware (the same
    way the mask path is), so the protocol version is the single
    compatibility knob and a cross-version vaulted-unmask can detect drift.
    """
    try:
        hmac_input = (
            bytes([SEED_PROTOCOL_VERSION])
            + len(label).to_bytes(4, "big")
            + label
            + len(extra).to_bytes(4, "big")
            + extra
        )
        return hmac.new(key, hmac_input, hashlib.sha256).digest()
    except BaseException:
        del key
        raise


class GenDeriveContext:
    """Per-column generation derivation: full-32-byte, per-family, per-row.

    Replaces the F2 4-byte truncation (`_bytes_to_seed`) and the F3
    `column_seed + i` arithmetic. One 32-byte column root is resolved once
    (see `for_column`); per-family keys and per-row ints are derived from it
    by HMAC, mirroring `determinism._derive.DeriveContext` (precompute key
    once, cheap per-row HMAC). Distinct labels give independent HKDF/HMAC
    outputs, so:

    - the three RNG families (`GEN_FAMILIES`) no longer share one int, and
    - columns with identical settings but distinct identities get distinct
      roots, so they never produce identical or row-shifted streams.

    `base_int(family)` seeds base-only consumers (`random.Random`,
    `numpy.default_rng`) with full-width material. `row_int(family, i)`
    replaces the per-row `seed + i` for faker/formula row loops.
    """

    __slots__ = ("_family_keys", "_root")

    def __init__(self, root: bytes) -> None:
        self._root = root
        self._family_keys: dict[str, bytes] = {}

    @classmethod
    def for_column(
        cls,
        derive_key: Callable[[str], bytes] | None,
        column_config: dict[str, Any],
        fallback_seed: int,
        *,
        snapshot_content_digest: str | None = None,
    ) -> GenDeriveContext:
        """Resolve the 32-byte column root (fresh / keyed / unkeyed).

        Keyed path derives `derive_key("gen:" + fingerprint + identity)`,
        consuming all 32 bytes. Raises on a
        `derive_key` resolver failure (QA-1 M18 contract) rather than
        silently degrading to the unkeyed path.

        `snapshot_content_digest` passes straight through to
        `strategy_config_fingerprint` (a `type: statistical` column's
        already-pinned digest, guide section 4.7/4.8); every other column
        type ignores it.
        """
        cfg = column_config or {}

        if cfg.get("determinism") == "fresh":
            # Same root for every row this run (within-run stable), fresh
            # across runs. Full 32 bytes, not 4.
            return cls(os.urandom(32))

        fingerprint = strategy_config_fingerprint(
            cfg, snapshot_content_digest=snapshot_content_digest
        )

        identity = column_identity(cfg)

        if derive_key is not None:
            try:
                root = derive_key(f"gen:{fingerprint}:{identity}")
            except Exception as exc:
                raise RuntimeError(
                    f"GenDeriveContext: derive_key failed for fingerprint-based "
                    f"path (fingerprint={fingerprint!r}): {type(exc).__name__}"
                ) from exc
            return cls(root)

        # Unkeyed fallback: HKDF-strength root over the fallback seed +
        # fingerprint + identity. Reproducible without a master key.
        # fallback_seed is expected in [0, 2**64): the plan path normalizes it
        # via _normalize_job_seed_int before generation. A direct-API caller
        # passing a negative or oversized seed fails loud here (OverflowError)
        # rather than silently masking it the way the old `& 0x7FFFFFFF` did.
        digest = hashlib.sha256(
            int(fallback_seed).to_bytes(8, "big", signed=False)
            + fingerprint.encode("utf-8")
            + identity.encode("utf-8")
        ).digest()
        return cls(digest)

    def _family_key(self, family: str) -> bytes:
        # No local holds the sub-key (`_opaque`).
        if family not in self._family_keys:
            self._family_keys[family] = _gen_hmac(self._root, b"fam:" + family.encode("utf-8"))
        return self._family_keys[family]

    def family_bytes(self, label: str) -> bytes:
        """Public byte-returning accessor for an arbitrary sub-key label.

        `base_int`/`row_int` only serve the fixed `GEN_FAMILIES`
        ("py"/"np"/"faker" and the two GEN-UNIQUE families). GP2's pool bridge needs two ADDITIONAL,
        mutually-independent domains off the same column root --
        "faker_pool_build" and "faker_pool_selection" (Codex round-2/3
        spec B) -- that are neither RNG-family names nor row-indexed.
        This is the same HMAC(root, "fam:"+label) sub-key `_family_key`
        already computes, exposed under any label so a caller isn't
        limited to the three built-in families or forced to reach into
        the private cache.
        """
        return self._family_key(label)

    def base_int(self, family: str) -> int:
        """Full-width non-negative int for a base-only RNG seed (one per column)."""
        return int.from_bytes(self._family_key(family), "big", signed=False)

    def row_int(self, family: str, i: int) -> int:
        """Full-width non-negative int for row `i` of `family` (replaces seed + i)."""
        block = _gen_hmac(
            self._family_key(family), family.encode("utf-8"), extra=i.to_bytes(8, "big")
        )
        return int.from_bytes(block, "big", signed=False)
