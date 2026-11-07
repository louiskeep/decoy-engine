"""email_mask DrawSite + provider-primitive data siblings.

Kept beside ``_draw_sites_tokenize.py`` for the same reason: the catalogue entry and its primitive
association are spliced into ``DRAW_SITES`` and ``_SOURCE_KEYED_PRIMITIVE`` from here, appended
last so no existing entry or digest moves.
"""

from __future__ import annotations

from decoy_engine.execution.native._draw_site_types import _V6, DrawSite

EMAIL_MASK_DRAW_SITES: tuple[DrawSite, ...] = (
    DrawSite(
        draw_site_id="mask.email_mask",
        family="source_keyed_hmac",
        call_site="execution/_strategies/_email_mask/_handler.py:107",
        entropy_root="mask_key",
        seed_derivation="derive(mask_key, namespace, nfc_normalize(str(value)).encode())",
        api_operation="determinism.derive -> HMAC-SHA256 digest hex",
        call_shape="derive(...).hex()[:20] + '@' + kept domain or class domain  # per value",
        consumes_variable_draws=False,
        identity="source_value",
        null_draw_behavior="null/NaN rows emit null and consume no derivation",
        partitionable=True,
        config_fingerprint_source="namespace_registry(namespace)+keep",
        provider_version=_V6,
        notes=(
            "The local part is the first 20 hex characters of the HMAC over the whole address, so "
            "it is the prefix of mask.hash's token for the same string value under the same "
            "namespace. The domain part takes no draw. email_mask is never native-routed (no "
            "operator), so this site is catalogued for totality and audit."
        ),
    ),
)

EMAIL_MASK_PRIMITIVE: dict[str, str] = {"mask.email_mask": "derive"}
