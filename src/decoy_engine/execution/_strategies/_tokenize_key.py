"""The tokenize strategy's vault-scoped key: one subkey per (mask key, namespace, vault).

Pattern: HKDF purpose separation (RFC 5869 §3.2 `info`; NIST SP 800-108r1 §4 Label and Context)
through the engine's existing `derive` envelope, the same shape as `vault-key/v1` (`vault.py`) and
`ff1-key/v1` (`determinism/_derive.py`).

    K_tok = derive(mask_key, namespace, b"tokenize-key/v1" || len(scope) || scope)
    token = "tok_" + hex(derive(K_tok, namespace, NFC(plaintext))[:16])

`scope` is the vault's own `derivation_scope`: a non-secret, vault-unique byte string the vault's
owner mints once. Binding it makes two vaults under one mask key and namespace tokenize the same
plaintext apart, so tokens cannot be correlated across vaults. The label makes the token HMAC key
an HKDF output under a different IKM from `hash`'s (`derive(mask_key, namespace, ...)`), so a
`hash` output is never a token payload. A label change is a new hard cut.

A writer with no scope predates this derivation; its tokens were minted under the old one, so it
is refused (`token_vault_derivation_unsupported`) rather than minted into under a second
derivation (pre-release hard cut, no migration).
"""

from __future__ import annotations

from typing import Any

from decoy_engine import reasons
from decoy_engine._opaque import OpaqueBox
from decoy_engine.determinism import derive
from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.execution._errors import StrategyError

TOKENIZE_KEY_LABEL = b"tokenize-key/v1"
SCOPE_MIN_BYTES = 16
SCOPE_MAX_BYTES = 64
_TOKEN_PREFIX = "tok_"  # noqa: S105 - a surrogate-token prefix, not a secret
_TOKEN_HEX_LEN = 32


def _summary(code: str) -> str:
    # The refusal text is the registered summary, verbatim: no column, value or scope in it.
    return reasons.get("runtime", code).summary


def require_derivation_scope(writer: Any) -> bytes:
    """The writer's `derivation_scope`, or the coded refusal before any row is processed."""
    unreadable = False
    try:
        scope = getattr(writer, "derivation_scope", None)
    except Exception:
        unreadable = True
    if scope is None and not unreadable:
        raise StrategyError(
            code=reasons.runtime.TOKEN_VAULT_DERIVATION_UNSUPPORTED,
            strategy="tokenize",
            message=_summary(reasons.runtime.TOKEN_VAULT_DERIVATION_UNSUPPORTED),
        )
    if (
        unreadable
        or type(scope) is not bytes
        or not SCOPE_MIN_BYTES <= len(scope) <= SCOPE_MAX_BYTES
    ):
        raise StrategyError(
            code=reasons.runtime.TOKENIZE_VAULT_SCOPE_REQUIRED,
            strategy="tokenize",
            message=_summary(reasons.runtime.TOKENIZE_VAULT_SCOPE_REQUIRED),
        )
    return scope


def tokenize_context(mask_key: OpaqueBox, namespace: str, scope: bytes) -> DeriveContext:
    """The per-column token HMAC context under the vault-scoped subkey `K_tok`. The mask key
    arrives boxed and is opened inside the derive call, so no frame here holds its bytes."""
    label = TOKENIZE_KEY_LABEL + len(scope).to_bytes(4, "big") + scope
    return DeriveContext.for_column(derive(mask_key.open()[0], namespace, label), namespace)


def token_from(keyed: DeriveContext, namespace: str, plaintext: str) -> str:
    """The proposed token for an NFC-normalized plaintext."""
    digest = keyed.derive_source(namespace, plaintext.encode("utf-8"))
    return _TOKEN_PREFIX + digest.hex()[:_TOKEN_HEX_LEN]
