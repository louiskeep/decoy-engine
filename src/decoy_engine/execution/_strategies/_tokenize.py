"""tokenize strategy (MG-5): reversible tokenization via a platform vault.

The engine emits a deterministic surrogate (the token) and records the
``(plaintext, token)`` pair through a platform-injected ``TokenVaultWriter``;
the platform owns the vault, its access control, and key custody (reversible
tokenization is an auth + persistence problem, not a data-behavior one). The
engine holds no vault -- library code does not know its callers -- so with no
vault the strategy fails closed.

Token shape: ``tok_`` + 32 hex chars of ``derive(mask_key, namespace,
nfc_normalize(str(value)).encode())``, keyed on the run's mask key AND the
column namespace, so the same value tokenizes differently in a different
namespace/vault. The token is deterministic, but the VAULT is the identity
boundary: if a token is already stored for a ``(namespace, plaintext)`` pair it
is REUSED, so a value's token is stable across jobs and across key rotations.
On a miss the strategy only PROPOSES the derived token; ``put`` returns the
authoritative stored one (a concurrent winner's, under a race) and that is what
is emitted.
The vault lookup and write are both scoped by namespace, so the same plaintext
under two namespaces is two distinct entries with two distinct tokens, never
one (namespaces cannot be linked through a shared token, and execution order
does not decide the token).

I/O shape (B-6): the column is resolved over its DISTINCT normalized plaintexts.
With a ``BatchTokenVaultWriter`` (both ``lookup_existing_many`` and ``put_many``)
that is one lookup and one bulk write per column; with a per-row writer it is
one ``lookup_existing`` per distinct plaintext and one ``put`` per miss. Either
way a repeated value is looked up and written once, and the emitted token per
row is identical: a hit is the vault's stored token, a miss is what the vault
returns for the proposed surrogate. Every writer result is checked to be a
non-empty ``str`` (and, for the batch calls, a mapping over exactly the
requested plaintexts) before it is published; anything else, and any writer
exception, becomes the coded, value-free ``tokenize_vault_error``.

Established methodology: reuses the engine's HKDF-SHA256 ``derive`` (the same
keyed derivation every re-identification-protecting strategy draws from), not a
hand-rolled hash, AND the same Unicode NFC string normalization ``mask.hash``
routes its derive input through (via ``_canonicalize_source``'s string branch)
rather than a bare ``str(value)`` -- tokenize is a REVERSIBLE primitive used as
an FK key, so two values that mean the same thing must canonicalize (and
therefore tokenize) identically. V0 is string-only: a fail-closed, coded
preflight restricts the source column to string/object dtype, which sidesteps
``mask.hash``'s other canonicalization branches entirely (no int-null-widening,
float, or tz-naive-datetime hazard ever reaches this handler, since none of
those dtypes passes the preflight). Wiring the full per-value
``_canonicalize_source`` dispatch (int/float/datetime branches, hard-errors
included) to lift this restriction is a tracked V1 follow-up. Format-preserving
tokens (same shape as the plaintext, via FPE under a vault key -- shape only,
NOT crypto reversal) are a separate tracked follow-up; this slice ships the
opaque default.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping

import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, TokenVaultWriter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

_TOKEN_PREFIX = "tok_"  # noqa: S105 - a surrogate-token prefix, not a secret
_TOKEN_HEX_LEN = 32


class TokenizeStrategyHandler:
    """Reversible tokenization: deterministic surrogate + vault-recorded mapping."""

    name: str = "tokenize"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        # getattr: handlers are also driven with hand-built contexts that predate
        # the field; a missing vault is the fail-closed case either way.
        vault = getattr(ctx, "vault_writer", None)
        if vault is None:
            raise StrategyError(
                code=reasons.runtime.TOKENIZE_REQUIRES_VAULT,
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but no vault was "
                    "provided by the platform; tokenize requires a vault-backed runner."
                ),
            )
        if plan.namespace is None:
            raise StrategyError(
                code=reasons.runtime.TOKENIZE_REQUIRES_NAMESPACE,
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but has no namespace; "
                    "the token is keyed on it (so different namespaces tokenize apart)."
                ),
            )
        col = df[column]
        # HIGH-1 (V0 canonicalization guard): tokenize is reversible and used as an
        # FK key, so a null-widened int column tokenizing as "123.0" (wrong on
        # reversal), or an int parent vs. a float-widened child FK tokenizing apart
        # (broken join), is not acceptable. Restricting the source to string/object
        # dtype sidesteps that widening hazard entirely (and the float / tz-naive-
        # datetime non-determinism mask.hash's canonicalizer hard-errors on): an
        # int/float/datetime/bool column fails closed here rather than silently
        # canonicalizing wrong.
        if not (pd.api.types.is_object_dtype(col.dtype) or isinstance(col.dtype, pd.StringDtype)):
            raise StrategyError(
                code=reasons.runtime.TOKENIZE_REQUIRES_STRING_DTYPE,
                strategy="tokenize",
                message=(
                    f"column {column!r} uses the tokenize strategy but its source dtype "
                    f"is {col.dtype!s}; V0 tokenize supports only string/object source "
                    "columns."
                ),
            )

        namespace = plan.namespace

        def _vault_error(op: str) -> StrategyError:
            # A vault callback error must become a fixed, coded, run-aborting error
            # that names neither the source value nor the token: a raw callback
            # exception (e.g. a database error echoing the plaintext) would otherwise
            # leak the value it is meant to protect through run_pipeline's exception.
            return StrategyError(
                code=reasons.runtime.TOKENIZE_VAULT_ERROR,
                strategy="tokenize",
                message=(
                    f"column {column!r}: the token vault raised during {op}; the run "
                    "is aborted so no partial or unrecorded output is written. The "
                    "underlying vault error is suppressed to avoid leaking the source "
                    "value or token."
                ),
            )

        # NFC-normalize like mask.hash's string branch (_canonicalize_source), so
        # two Unicode-equivalent-but-byte-different values tokenize identically.
        # The vault's plaintext key and the derive input are the SAME string, so
        # lookup/reversal and the token stay consistent with each other. Nulls
        # stay None and never reach the vault.
        normalized: list[str | None] = [
            None if pd.isna(value) else unicodedata.normalize("NFC", str(value))
            for value in col.tolist()
        ]
        # Distinct plaintexts, first-seen order, so vault I/O is per distinct value
        # (one lookup + one write with a batch writer) and never per cell.
        distinct = list(dict.fromkeys(p for p in normalized if p is not None))
        token_for = _resolve_tokens(vault, namespace, distinct, ctx.mask_key, _vault_error)
        df[column] = [None if p is None else token_for[p] for p in normalized]
        return df, []


def _batch_seam(vault: object) -> tuple[Callable[..., object], Callable[..., object]] | None:
    """The writer's ``(lookup_existing_many, put_many)`` when it provides BOTH,
    else None (per-row path). One of the two is not a batch writer."""
    lookup_many = getattr(vault, "lookup_existing_many", None)
    put_many = getattr(vault, "put_many", None)
    if callable(lookup_many) and callable(put_many):
        return lookup_many, put_many
    return None


def _vault_call(
    error: Callable[[str], StrategyError], op: str, fn: Callable[..., object], *args: object
) -> object:
    """Call one writer method; any exception becomes the coded, value-free error.

    The error is raised AFTER the ``except`` block exits (the same rule
    ``profile._fixed_width_reader`` and ``expressions._when_parser`` follow): a
    raise inside the handler, even ``from None``, leaves the writer's exception
    on ``__context__``, and a database error quotes the plaintext it failed on.
    """
    failed = False
    try:
        result = fn(*args)
    except Exception:
        failed = True
    if failed:
        raise error(op)
    return result


def _checked_token(token: object) -> str | None:
    """A writer-returned token the strategy may publish, or None when the writer
    returned something that is not a non-empty ``str`` (fail closed upstream)."""
    return token if isinstance(token, str) and token else None


def _checked_tokens(
    result: object,
    requested: list[str],
    *,
    require_all: bool,
    error: Callable[[str], StrategyError],
    op: str,
) -> dict[str, str]:
    """Validate a batch writer's ``{plaintext: token}`` result: a mapping whose keys
    are a subset of (or, with ``require_all``, exactly) the requested plaintexts
    and whose values are non-empty strings. Anything else fails closed with the
    coded, value-free error, so a buggy or hostile writer can never publish an
    unmapped, empty or foreign token."""
    if not isinstance(result, Mapping):
        raise error(op)
    allowed = set(requested)
    out: dict[str, str] = {}
    for plaintext, token in result.items():
        checked = _checked_token(token)
        if plaintext not in allowed or checked is None:
            raise error(op)
        out[plaintext] = checked
    if require_all and len(out) != len(allowed):
        raise error(op)
    return out


def _resolve_tokens(
    vault: TokenVaultWriter,
    namespace: str,
    distinct: list[str],
    mask_key: bytes,
    error: Callable[[str], StrategyError],
) -> dict[str, str]:
    """``{plaintext: token}`` for every distinct plaintext: the vault's stored
    token where one exists (the vault is authoritative), else the token the vault
    returns for the proposed HKDF surrogate. Batch writer: one lookup, one bulk
    write. Per-row writer: one lookup per plaintext, one put per miss."""
    token_for: dict[str, str] = {}
    if not distinct:
        return token_for
    batch = _batch_seam(vault)
    if batch is None:
        for plaintext in distinct:
            existing = _vault_call(error, "lookup", vault.lookup_existing, namespace, plaintext)
            if existing is not None:
                checked = _checked_token(existing)
                if checked is None:
                    raise error("lookup")
                token_for[plaintext] = checked
    else:
        lookup_many, _ = batch
        found = _vault_call(error, "lookup", lookup_many, namespace, list(distinct))
        token_for.update(
            _checked_tokens(found, distinct, require_all=False, error=error, op="lookup")
        )

    # Only PROPOSALS: the vault's committed token is what gets emitted.
    minted = {
        plaintext: _TOKEN_PREFIX
        + derive(mask_key, namespace, plaintext.encode("utf-8")).hex()[:_TOKEN_HEX_LEN]
        for plaintext in distinct
        if plaintext not in token_for
    }
    if not minted:
        return token_for
    if batch is None:
        for plaintext, token in minted.items():
            stored = _vault_call(error, "write", vault.put, namespace, plaintext, token)
            # The vault's committed token wins a race; a writer that returns
            # nothing (the pre-0.16 Protocol) must not publish a null/unmapped token.
            checked = _checked_token(stored)
            if checked is None:
                raise error("write")
            token_for[plaintext] = checked
    else:
        _, put_many = batch
        stored_many = _vault_call(error, "write", put_many, namespace, dict(minted))
        token_for.update(
            _checked_tokens(stored_many, list(minted), require_all=True, error=error, op="write")
        )
    return token_for
