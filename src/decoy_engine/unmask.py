"""Detokenization entry: invert fpe columns of a masked output.

`unmask_pipeline(config, masked_sources)` is the inverse of the fpe leg
of `run_pipeline`. The config is the SAME pipeline YAML the mask run
used; it carries everything reversal needs -- the job seed (the secret),
and per-column namespace + charset + separator/Luhn flags. Anyone
holding that config can reverse fpe columns, so the config must be
handled with the sensitivity of a key (stated in the CLI docs too).

What reverses and what does not:

| strategy             | status         | why |
|----------------------|----------------|-----|
| fpe                  | reversed_unverified | keyed NIST SP 800-38G FF1 permutation is a bijection; key = derive(seed, ns, FF1_KEY_LABEL). ALWAYS reported unverified (never plain `reversed`, even under a real secret): FF1 is unauthenticated, so a wrong key yields plausible but wrong plaintext with no signal that it is wrong (Task 5.2 P4) |
| any one-way + vault: true + vault file | vault_reversed / vault_miss | the mask run recorded the source->masked map into an encrypted vault (decoy_engine.vault); lookup keyed by (namespace, masked) |
| hash                 | irreversible   | HMAC-SHA256 is one-way; recovery needs the column's vault |
| redact / truncate    | irreversible   | information destroyed |
| faker / categorical / reference / composite | irreversible | substitution without stored mapping (unless vaulted) |
| date_shift / shuffle | irreversible   | per-row offsets / permutation not stored |
| text_redact          | irreversible   | span contents destroyed |
| (no strategy)        | untouched      | passed through unchanged |

Vault statuses: `vault_reversed` (at least one value recovered; the
detail carries the miss count when partial) and `vault_miss` (zero
hits, usually the wrong vault for this output). A vaulted column with
no vault supplied stays `irreversible` with a pointer in the detail.

Luhn caveat: fpe columns with `validate_luhn: true` recompute the check
digit on decrypt (it is not stored), so the round trip is byte-exact iff
the source satisfied Luhn -- the domain the mode exists for (PANs). A
non-Luhn source comes back with the body exact and the last digit
normalized; the per-column report carries this caveat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._strategies._fpe import FF1_KEY_LABEL
from decoy_engine.plan._seed import _normalize_job_seed
from decoy_engine.transforms.fpe import (
    FF1_TWEAK_SCOPE_COLUMN,
    FF1_TWEAK_SCOPE_JOIN_GROUP,
    build_ff1_tweak,
    check_charset_unique,
    fpe_decrypt_value,
    resolve_fpe_charset,
)

if TYPE_CHECKING:
    from decoy_engine.keyprovider import KeyProvider

_LUHN_CAVEAT = (
    "validate_luhn recomputes the check digit on decrypt; round trip is "
    "byte-exact iff the source was Luhn-valid"
)
_CHECKSUM_CAVEAT = (
    "checksum={scheme} recomputes the check digit on decrypt; round trip is "
    "byte-exact iff the source was valid for the scheme"
)
_FPE_UNVERIFIED_CAVEAT = (
    "UNVERIFIED: FF1 is unauthenticated, so a wrong key yields a plausible-looking "
    "but WRONG plaintext with no signal that it is wrong. This caveat applies "
    "regardless of whether a mask secret was supplied (Task 5.2 P4); there is "
    "no authenticated-reversal mode for FF1 today."
)


@dataclass(frozen=True)
class UnmaskColumnReport:
    """Per-column reversibility verdict for one unmask run."""

    table: str
    column: str
    strategy: str | None
    # reversed | reversed_unverified (fpe under the non-secret fallback, DE-02) |
    # vault_reversed | vault_miss | irreversible | untouched | table_missing
    status: str
    detail: str = ""


@dataclass(frozen=True)
class UnmaskResult:
    """Unmasked tables plus the per-column reversibility report."""

    outputs: dict[str, pa.Table]
    columns: tuple[UnmaskColumnReport, ...]


def _decrypt_column(
    table: pa.Table,
    column: str,
    *,
    key: bytes,
    cfg: dict[str, Any],
    tweak: bytes,
    table_name: str,
) -> pa.Table:
    charset_spec = cfg.get("charset", "digits")
    charset = resolve_fpe_charset(charset_spec)
    # Round-2 BLOCKER-2/MEDIUM-2: a duplicate-symbol or degenerate charset can
    # never have produced a masked value in the first place (the mask-side
    # `FpeStrategyHandler.run` / OOC `fpe_array` fail closed on both, codes
    # `fpe_charset_duplicate_symbols` / `fpe_charset_degenerate`). This used to
    # be a silent `return table` no-op here -- the column came back UNCHANGED
    # (still ciphertext, or whatever the stored config now resolves to) while
    # the caller still reported `reversed_unverified`, a silent failed
    # reversal masquerading as a completed one. Fail closed instead, matching
    # the mask side's codes exactly, so a config edited after the mask run
    # (or a hand-written unmask config) cannot produce a false reversal claim.
    try:
        check_charset_unique(charset_spec, charset)
    except FpeUnencryptableError as exc:
        raise ExecutionError(
            code="fpe_charset_duplicate_symbols",
            message=f"column {column!r} in table {table_name!r}: {exc}",
        ) from exc
    if len(charset) < 2:
        raise ExecutionError(
            code="fpe_charset_degenerate",
            message=(
                f"column {column!r} in table {table_name!r} uses fpe but its "
                f"resolved charset {charset_spec!r} -> {charset!r} has fewer than 2 "
                "distinct characters. A degenerate charset has nothing to permute "
                "over, so this column cannot be reversed."
            ),
        )
    preserve_sep = bool(cfg.get("preserve_separators", True))
    # Codex cross-model review (2026-07-14): forward `checksum` and mirror the
    # encrypt-side resolution EXACTLY (checksum takes priority over validate_luhn).
    # Pre-existing bug (since the 2026-06-12 checksum landing, 07d85368): decrypt
    # forwarded validate_luhn but NOT checksum, so a checksum-mode fpe column ran
    # the plain inverse and silently returned WRONG plaintext (e.g. npi
    # 1234567893 -> 1770507352 -> 5577387655), still reported `reversed`.
    checksum: str | None = cfg.get("checksum") or None
    validate_luhn = (
        checksum is None
        and bool(cfg.get("validate_luhn", False))
        and all(c in "0123456789" for c in charset)
    )

    def _decrypt_one(v: object) -> object:
        if v is None:
            return v
        s = str(v)
        if s == "":
            # Empty string is not null; the mask side now treats it as a
            # missing-data passthrough rather than sending it to the cipher
            # (plan P2: an empty value's domain is below the FF1 floor), so
            # the masked output already carries it unchanged. Mirror that
            # here instead of calling `fpe_decrypt_value` on it.
            return s
        return fpe_decrypt_value(s, key, charset, tweak, preserve_sep, validate_luhn, checksum)

    values = table.column(column).to_pylist()
    decrypted = [_decrypt_one(v) for v in values]
    idx = table.schema.get_field_index(column)
    return table.set_column(idx, column, pa.array(decrypted, type=pa.string()))


def _vault_recover_column(
    table: pa.Table,
    column: str,
    *,
    namespace: str,
    strategy: str | None,
    table_name: str,
    vault_map: dict[tuple[str, str], str],
    vault_ambiguous: int,
) -> tuple[pa.Table, UnmaskColumnReport]:
    """Replace vault hits in `column`; misses keep the masked value."""
    values = table.column(column).to_pylist()
    recovered: list[Any] = []
    hits = 0
    total = 0
    for v in values:
        if v is None:
            recovered.append(None)
            continue
        total += 1
        source = vault_map.get((namespace, str(v)))
        if source is None:
            recovered.append(v)
        else:
            recovered.append(source)
            hits += 1
    misses = total - hits
    if hits == 0 and total > 0:
        detail = "no masked value of this column is in the vault; wrong vault for this output?"
        status = "vault_miss"
    else:
        status = "vault_reversed"
        parts = []
        if misses > 0:
            parts.append(f"{misses} of {total} values not in the vault; left masked")
        if vault_ambiguous > 0:
            parts.append(
                f"vault dropped {vault_ambiguous} ambiguous key(s) at write time "
                "(conflicting sources for one masked value)"
            )
        detail = "; ".join(parts)
    idx = table.schema.get_field_index(column)
    table = table.set_column(idx, column, pa.array(recovered, type=pa.string()))
    return table, UnmaskColumnReport(
        table=table_name, column=column, strategy=strategy, status=status, detail=detail
    )


def unmask_pipeline(
    config: dict[str, Any],
    masked_sources: dict[str, pa.Table],
    *,
    vault_path: str | None = None,
    key_provider: KeyProvider | None = None,
) -> UnmaskResult:
    """Invert the fpe and vaulted columns of `masked_sources` under `config`.

    `config` is the pipeline config the mask run used (validated dump or
    raw dict; only `global_settings.seed` and the per-table `columns`
    entries are consulted). Tables in `masked_sources` that the config
    does not mention pass through unchanged; configured tables absent
    from `masked_sources` are reported `table_missing`, never invented.
    `vault_path` names the encrypted vault artifact the mask run wrote;
    one-way columns declared `vault: true` recover through it (see
    `decoy_engine.vault` for the security model).

    Raises:
        ExecutionError: ``code='fpe_requires_namespace'`` when an fpe
            column has no namespace (the key cannot be derived);
            ``code='vault_crypto_not_installed'``,
            ``code='vault_unreadable'``,
            ``code='vault_format_unsupported'``,
            ``code='vault_protocol_version_mismatch'`` when the vault was
            written under a different ``SEED_PROTOCOL_VERSION``, or
            ``code='vault_key_mismatch'`` when the supplied vault cannot
            be opened under this config.
    """
    job_seed = _normalize_job_seed(config)
    # DE-02: reverse the keyed surface under the SAME mask key the mask run used
    # (a programmatic key_provider wins over global_settings.mask_secret_ref; both
    # absent -> job_seed, byte-identical to pre-DE-02). Reversing under the wrong
    # key surfaces as vault_key_mismatch (vault) or wrong plaintext (fpe).
    from decoy_engine.keyprovider import key_provider_from_ref

    provider: KeyProvider | None = key_provider
    if provider is None:
        ref = (config.get("global_settings") or {}).get("mask_secret_ref")
        if ref:
            provider = key_provider_from_ref(ref)
    mask_key = provider.mask_key() if provider is not None else job_seed
    # DE-02 (Codex MEDIUM 6): unmask reverses KEYED surface (fpe + vaulted
    # columns). Route it through the same fail-closed gate: a resolved key < 32
    # bytes (the 8-byte job_seed fallback, or an empty custom provider) is NOT a
    # real secret. At GA, reversing keyed columns without one hard-errors instead
    # of silently producing wrong plaintext labelled "reversed".
    from decoy_engine.keyprovider import MIN_SECRET_BYTES, KeyedStrategyRequiresSecret
    from decoy_engine.release import is_pre_ga

    _authenticated = len(mask_key) >= MIN_SECRET_BYTES
    _has_fpe = any(
        (c.get("strategy") == "fpe")
        for t in (config.get("tables") or [])
        for c in (t.get("columns") or [])
    )
    _keyed_reversal = vault_path is not None or _has_fpe
    if _keyed_reversal and not _authenticated and not is_pre_ga():
        raise KeyedStrategyRequiresSecret(
            "unmask reverses keyed columns (fpe / vaulted) which at GA require the "
            "mask secret. Supply run(...)'s key_provider or global_settings."
            "mask_secret_ref; the 8-byte job_seed fallback is not accepted."
        )
    vault_map: dict[tuple[str, str], str] | None = None
    vault_ambiguous = 0
    if vault_path is not None:
        from decoy_engine.vault import VaultError, load_vault

        try:
            vault_map, vault_ambiguous = load_vault(vault_path, mask_key)
        except VaultError as exc:
            raise ExecutionError(code=exc.code, message=exc.message) from exc
    reports: list[UnmaskColumnReport] = []
    outputs: dict[str, pa.Table] = {}
    configured_tables: set[str] = set()

    for table_cfg in config.get("tables") or []:
        name = table_cfg.get("name")
        if not name:
            continue
        configured_tables.add(name)
        if name not in masked_sources:
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column="*",
                    strategy=None,
                    status="table_missing",
                    detail="configured table absent from the provided inputs",
                )
            )
            continue
        table = masked_sources[name]

        if table_cfg.get("generate_columns"):
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column="*",
                    strategy=None,
                    status="irreversible",
                    detail="generated synthetic table; no source to recover",
                )
            )
            outputs[name] = table
            continue

        present = set(table.schema.names)
        configured_columns: set[str] = set()
        for col_cfg in table_cfg.get("columns") or []:
            col = col_cfg.get("name")
            if not col or col not in present:
                continue
            configured_columns.add(col)
            strategy = col_cfg.get("strategy")
            if strategy is None:
                reports.append(
                    UnmaskColumnReport(table=name, column=col, strategy=None, status="untouched")
                )
                continue
            if strategy != "fpe":
                if col_cfg.get("vault"):
                    if vault_map is None:
                        reports.append(
                            UnmaskColumnReport(
                                table=name,
                                column=col,
                                strategy=strategy,
                                status="irreversible",
                                detail=(
                                    "vault: true is declared on this column; supply "
                                    "the mask run's vault file to recover it"
                                ),
                            )
                        )
                        continue
                    namespace = col_cfg.get("namespace")
                    if not namespace:
                        # check_vault_columns rejects this at compile; raw-dict
                        # callers fall through to plain irreversible.
                        reports.append(
                            UnmaskColumnReport(
                                table=name,
                                column=col,
                                strategy=strategy,
                                status="irreversible",
                                detail="vault: true without a namespace cannot be looked up",
                            )
                        )
                        continue
                    table, report = _vault_recover_column(
                        table,
                        col,
                        namespace=str(namespace),
                        strategy=strategy,
                        table_name=name,
                        vault_map=vault_map,
                        vault_ambiguous=vault_ambiguous,
                    )
                    reports.append(report)
                    continue
                reports.append(
                    UnmaskColumnReport(
                        table=name,
                        column=col,
                        strategy=strategy,
                        status="irreversible",
                        detail=f"{strategy} does not retain the information needed to invert",
                    )
                )
                continue
            namespace = col_cfg.get("namespace")
            if not namespace:
                raise ExecutionError(
                    code="fpe_requires_namespace",
                    message=(
                        f"column {col!r} in table {name!r} uses fpe but has no "
                        "namespace; the decryption key cannot be derived."
                    ),
                )
            cfg = col_cfg.get("provider_config") or {}
            key = derive(mask_key, namespace, FF1_KEY_LABEL)
            # SP-46: mirror the join-group tweak resolution from _strategies/_fpe.py.
            # When fpe_join_group is set the tweak is the group name, not the column
            # name; using the wrong tweak produces incorrect decryption. The config
            # carries the same fpe_join_group value the mask run used, so this
            # lookup is always safe (same config -> same tweak resolution).
            join_group: str | None = cfg.get("fpe_join_group") or None
            fpe_tweak = build_ff1_tweak(
                FF1_TWEAK_SCOPE_JOIN_GROUP if join_group else FF1_TWEAK_SCOPE_COLUMN,
                join_group or col,
            )
            table = _decrypt_column(table, col, key=key, cfg=cfg, tweak=fpe_tweak, table_name=name)
            # checksum takes priority over validate_luhn (same as encrypt). Both
            # recompute the check digit on decrypt rather than store it, so the
            # round trip is byte-exact iff the source was valid for the scheme.
            checksum = cfg.get("checksum") or None
            if checksum is not None:
                detail = _CHECKSUM_CAVEAT.format(scheme=checksum)
            elif bool(cfg.get("validate_luhn", False)):
                detail = _LUHN_CAVEAT
            else:
                detail = ""
            # Task 5.2 P4 (round-2 finding 3 / BLOCKER-2): FF1 is unauthenticated
            # for every reversal, secret or not. A wrong key yields a plausible
            # but WRONG plaintext with no signal that it is wrong. Pre-FF1, a
            # real secret upgraded this to a plain `reversed` status; that
            # distinction no longer holds, so EVERY fpe reversal now reports
            # `reversed_unverified`, including the secret-keyed path.
            fpe_status = "reversed_unverified"
            caveat = _FPE_UNVERIFIED_CAVEAT
            detail = f"{detail} {caveat}".strip() if detail else caveat
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column=col,
                    strategy="fpe",
                    status=fpe_status,
                    detail=detail,
                )
            )
        for col in sorted(present - configured_columns):
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column=col,
                    strategy=None,
                    status="untouched",
                    detail="column not in the pipeline config",
                )
            )
        outputs[name] = table

    for name, table in masked_sources.items():
        if name not in configured_tables:
            outputs[name] = table
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column="*",
                    strategy=None,
                    status="untouched",
                    detail="table not in the pipeline config",
                )
            )

    return UnmaskResult(outputs=outputs, columns=tuple(reports))
