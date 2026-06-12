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
| fpe                  | reversed       | keyed Feistel permutation is a bijection; key = derive(seed, ns, FPE_KEY_LABEL) (NIST SP 800-38G FF1 key model) |
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
from typing import Any

import pyarrow as pa

from decoy_engine.determinism import derive
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL
from decoy_engine.plan._compile import _normalize_job_seed
from decoy_engine.transforms.fpe import _CHARSETS, fpe_decrypt_value

_LUHN_CAVEAT = (
    "validate_luhn recomputes the check digit on decrypt; round trip is "
    "byte-exact iff the source was Luhn-valid"
)


@dataclass(frozen=True)
class UnmaskColumnReport:
    """Per-column reversibility verdict for one unmask run."""

    table: str
    column: str
    strategy: str | None
    status: str  # reversed | vault_reversed | vault_miss | irreversible | untouched | table_missing
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
) -> pa.Table:
    charset_spec = cfg.get("charset", "digits")
    charset = "".join(dict.fromkeys(_CHARSETS.get(charset_spec, charset_spec)))
    if len(charset) < 2:
        return table  # degenerate charset was a passthrough on encrypt too
    preserve_sep = bool(cfg.get("preserve_separators", True))
    validate_luhn = bool(cfg.get("validate_luhn", False)) and all(
        c in "0123456789" for c in charset
    )
    values = table.column(column).to_pylist()
    decrypted = [
        v
        if v is None
        else fpe_decrypt_value(str(v), key, charset, tweak, preserve_sep, validate_luhn)
        for v in values
    ]
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
            ``code='vault_format_unsupported'``, or
            ``code='vault_key_mismatch'`` when the supplied vault cannot
            be opened under this config.
    """
    job_seed = _normalize_job_seed(config)
    vault_map: dict[tuple[str, str], str] | None = None
    vault_ambiguous = 0
    if vault_path is not None:
        from decoy_engine.vault import VaultError, load_vault

        try:
            vault_map, vault_ambiguous = load_vault(vault_path, job_seed)
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
            key = derive(job_seed, namespace, FPE_KEY_LABEL)
            table = _decrypt_column(
                table, col, key=key, cfg=cfg, tweak=col.encode("utf-8", errors="replace")
            )
            luhn = bool(cfg.get("validate_luhn", False))
            reports.append(
                UnmaskColumnReport(
                    table=name,
                    column=col,
                    strategy="fpe",
                    status="reversed",
                    detail=_LUHN_CAVEAT if luhn else "",
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
