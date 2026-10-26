"""tokenize (MG-5) DrawSite + provider-primitive data siblings.

Split out of ``_determinism_protocol.py`` / ``_draw_site_providers.py`` for the
same reason ``_draw_sites_gen_pool.py`` / ``_draw_site_providers_gen_pool.py``
split out: both files are allowlisted at their recorded LOC ceiling
(``tests/sentry/test_module_size.py``), so the new ``mask.tokenize`` catalogue
entry and its provider-primitive association land here and are spliced into
``DRAW_SITES`` / ``_SOURCE_KEYED_PRIMITIVE`` respectively, instead of
regrowing either already-capped file.
"""

from __future__ import annotations

from decoy_engine.execution.native._determinism_protocol import _V6, DrawSite

TOKENIZE_DRAW_SITES: tuple[DrawSite, ...] = (
    DrawSite(
        draw_site_id="mask.tokenize",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_tokenize.py:122",
        entropy_root="mask_key",
        seed_derivation="derive(mask_key, namespace, nfc_normalize(str(value)).encode())",
        api_operation="determinism.derive -> HMAC-SHA256 digest hex",
        call_shape="'tok_' + derive(...).hex()[:32]  # per value, then vault-recorded",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null/NaN rows emit null and consume no derivation",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)",
        provider_version=_V6,
        notes=(
            "V0 preflight restricts the source column to string/object dtype "
            "(tokenize_requires_string_dtype otherwise), sidestepping every other "
            "mask.hash canonicalization branch (int-null-widening, float, tz-naive "
            "datetime); within that string-only domain the value is NFC-normalized "
            "like mask.hash's string branch, not a raw str(value). The HMAC digest "
            "seeds a fresh surrogate token; the (plaintext, token) pair -- plaintext "
            "is the SAME NFC-normalized string the token derives from -- is then "
            "recorded in the PLATFORM vault. The vault is external state: "
            "lookup_existing may override the freshly drawn token with a previously "
            "stored one (the vault is the identity boundary). tokenize is never "
            "native-routed (no compiled kernel; per-cell vault callback), so this "
            "site is catalogued for totality/audit, not for a native draw."
        ),
    ),
)

# tokenize draws its surrogate through the same `derive` primitive as mask.hash
# (both source_keyed_hmac, single-primitive sites); the vault reuse/persistence
# that follows the draw is outside this association.
TOKENIZE_PRIMITIVE: dict[str, str] = {"mask.tokenize": "derive"}
