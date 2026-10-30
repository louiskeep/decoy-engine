"""The record type and allowed vocabularies of the RNG draw-site inventory.

``DrawSite`` and the enums every catalogued entry draws from (``FAMILIES``,
``ENTROPY_ROOTS``, ``IDENTITIES``, ``DETERMINISTIC_NO_DRAW``). The per-family
data modules (``_draw_sites_*.py``) build their entries from these, and
``_determinism_protocol.py`` assembles them into ``DRAW_SITES`` and re-exports
everything here. Kept free of any catalogue import so the data modules can
import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decoy_engine.determinism import SEED_PROTOCOL_VERSION

# ---------------------------------------------------------------------------
# Allowed enums. The coverage test asserts every entry draws only from these.
# ---------------------------------------------------------------------------

# RNG families. Each names the determinism MECHANISM that Task 0.3 preserves,
# not merely the library: two numpy sites can differ in whether they reseed
# per row, per group, or draw one whole-column stream.
FAMILIES: frozenset[str] = frozenset(
    {
        "numpy_pcg64",  # draws from a numpy.random.Generator (default_rng / PCG64)
        "python_mt19937",  # draws from a stdlib random.Random (Mersenne Twister)
        "per_row_reseed",  # one Random reseeded every row from a per-row key
        "source_keyed_hmac",  # the HMAC/HKDF digest itself IS the pseudo-randomness
        "per_group_stream",  # one RNG stream advanced sequentially within a group
        "faker_seed_instance",  # Faker.seed_instance(...) then a provider draw
        "gen_derive_context",  # the GenDeriveContext seed-derivation substrate
    }
)

# The run-level entropy root each site is ultimately seeded from. The engine's
# real names: ``mask_key`` is ``StrategyContext.mask_key`` (the 8-byte job_seed
# verbatim when no KeyProvider secret is present, else a 32-byte HKDF root);
# ``job_seed`` is the 8-byte generation seed the generation path threads as
# ``seed``. ``none`` is a genuinely unseeded (non-deterministic-mode) site.
ENTROPY_ROOTS: frozenset[str] = frozenset({"mask_key", "job_seed", "none"})

# What a single draw is keyed on.
IDENTITIES: frozenset[str] = frozenset(
    {
        "row_index",  # the row's positional index within the column/frame
        "global_row_number",  # a row number that must survive partition boundaries
        "source_value",  # the input cell value (mask-side, keyed by content)
        "group_key",  # the partition/group label
        "column",  # whole-column single stream (no per-row key)
        "none",  # substrate / build-time, no per-output-row identity
    }
)

# Sentinel for a live strategy or generation kind that provably draws no
# randomness (identity transform, deterministic slice, expression eval).
DETERMINISTIC_NO_DRAW = "deterministic_no_draw"

_V6: str = f"seed_protocol_v{SEED_PROTOCOL_VERSION}"


@dataclass(frozen=True)
class DrawSite:
    """One place randomness is consumed to produce masked or generated output.

    Fields are the Task 0.1 record. ``seed_derivation`` and ``call_shape`` are
    verbatim-faithful to the code at ``call_site`` and are the exact behavior
    Task 0.3 preserves. ``notes`` and ``uncertain`` are additive: ``uncertain``
    flags a site whose behavior could not be fully determined from the code.
    """

    draw_site_id: str
    family: str
    call_site: str
    entropy_root: str
    seed_derivation: str
    api_operation: str
    call_shape: str
    consumes_variable_draws: bool
    identity: str
    null_draw_behavior: str
    partitionable: bool
    config_fingerprint_source: str
    provider_version: str
    notes: str = ""
    uncertain: bool = False
    # Sibling call sites that implement the SAME mechanism byte-for-byte
    # (pandas substrate, in-core/out-of-core). Recorded so the protocol knows
    # every physical location that must move in lockstep.
    mirror_call_sites: tuple[str, ...] = field(default_factory=tuple)
