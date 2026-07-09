"""Group (b) out-of-core mask strategies (SC3): fpe, text_redact, categorical.

Ports the full-frame V2 handlers' per-value logic onto the backend-neutral
Arrow kernel, byte-identical to the pandas oracle for the admitted shapes. Each
kernel here is per-value deterministic and null-preserving, so masking one
RecordBatch equals the same rows of a whole-column mask -- the single-kernel
parity contract `_mask.py` documents. Split out of `_mask.py` by concern to
hold the ~600 LOC orchestration cap (CLAUDE.md engineering best practices).

Established methodology (CLAUDE.md core rule): none of these invent a new
approach; each reuses the SAME primitive the full-frame handler cites, so the
out-of-core output is byte-identical rather than merely similar.

- fpe: reuses `transforms.fpe.fpe_encrypt_value` (type-II Feistel + HMAC-SHA256;
  NIST SP 800-38G FF1 single-key/varying-tweak key model) with the exact key
  derivation the `_strategies/_fpe.FpeStrategyHandler` uses
  (`derive(job_seed, namespace, FPE_KEY_LABEL)`, column-or-join_group tweak).
- text_redact: reuses `storm.detectors.iter_spans` + `_text_redact._splice`
  (span-level PII redaction; deterministic pure function of input + config).
- categorical: reuses `determinism.derive_index` over the plan's category pool
  (source-conditioned deterministic remap), including the `_categorical`
  weighted-CDF path.

Out of scope for SC3 (documented routing MISS in `_compat.py`): faker (needs a
registry-backed ValuePool + cross-batch pool cache the registry-free kernel has
no channel for), bucketize and date_shift (both record per-value format errors
that the full-frame path quarantines via the D8 pass but the out-of-core route
has no row-error/quarantine channel for; date_shift additionally needs
whole-column format detection). Admitting any of them would risk a
route-dependent divergence, so they stay a fail-closed MISS.
"""

from __future__ import annotations

import bisect
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.determinism import derive, derive_index
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._strategies._categorical import _WEIGHTED_CDF_RES, _build_cdf
from decoy_engine.execution._strategies._fpe import FPE_KEY_LABEL
from decoy_engine.execution._strategies._text_redact import _DEFAULT_TOKEN, _splice
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing
from decoy_engine.storm.detectors import Span, iter_spans
from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

if TYPE_CHECKING:
    from decoy_engine.plan._types import ColumnSeed

GROUP_B_STRATEGIES = frozenset({"fpe", "text_redact", "categorical"})


# ---------------------------------------------------------------------------
# fpe
# ---------------------------------------------------------------------------


def _fpe_charset(cfg: dict[str, Any]) -> str:
    """Resolve + validate the fpe charset, fail-closed on a degenerate one.

    Mirrors `_strategies/_fpe.FpeStrategyHandler.run` exactly (resolve the
    named charset, dedup preserving order, reject < 2 distinct chars with the
    SAME `fpe_charset_degenerate` code the oracle raises) so the out-of-core
    route rejects the identical configs at the identical point.
    """
    charset_spec = cfg.get("charset", "digits")
    charset = "".join(dict.fromkeys(_CHARSETS.get(charset_spec, charset_spec)))
    if len(charset) < 2:
        raise StrategyError(
            code="fpe_charset_degenerate",
            strategy="fpe",
            message=(
                f"fpe resolved charset {charset_spec!r} -> {charset!r} has fewer than "
                "2 distinct characters; a degenerate charset has nothing to permute over."
            ),
        )
    return charset


def fpe_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    job_seed: bytes,
    namespace: str | None,
    column: str | None,
    cfg: dict[str, Any],
) -> pa.Array:
    """Format-preserving-encrypt each non-null value, byte-identical to the oracle."""
    if namespace is None:
        raise StrategyError(
            code="fpe_requires_namespace",
            strategy="fpe",
            message="fpe strategy requires a namespace.",
        )
    charset = _fpe_charset(cfg)
    preserve_sep = bool(cfg.get("preserve_separators", True))
    checksum: str | None = cfg.get("checksum") or None
    validate_luhn = (
        checksum is None
        and bool(cfg.get("validate_luhn", False))
        and all(c in "0123456789" for c in charset)
    )
    join_group: str | None = cfg.get("fpe_join_group") or None
    tweak_source = join_group or column
    if tweak_source is None:
        # Only reachable if fpe were masked without a column context (e.g. an
        # FK-key/remap caller). The compat gate keeps fpe off the parent-key
        # surface, so this fails closed rather than silently mis-tweaking.
        raise ExecutionError(
            code="out_of_core_fpe_column_required",
            message="fpe out-of-core masking needs a column name (or fpe_join_group) for the tweak.",
        )
    tweak = tweak_source.encode("utf-8", errors="replace")
    key = derive(job_seed, namespace, FPE_KEY_LABEL)

    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        out.append(
            fpe_encrypt_value(
                str(value), key, charset, tweak, preserve_sep, validate_luhn, checksum
            )
        )
    return pa.array(out, type=pa.string())


# ---------------------------------------------------------------------------
# text_redact
# ---------------------------------------------------------------------------


def _text_redact_detector_ids(detectors_cfg: Any) -> tuple[list[str] | None, bool]:
    """Resolve the detector-id list. Returns (ids, ok); ok=False => passthrough.

    Mirrors `_text_redact.TextRedactHandler.run`: None => all detectors; a
    list/tuple (empty coerces to all, fail-safe never "redact nothing"); any
    other type => the handler leaves the column unchanged.
    """
    if detectors_cfg is None:
        return None, True
    if isinstance(detectors_cfg, (list, tuple)):
        return ([str(d) for d in detectors_cfg] or None), True
    return None, False


def text_redact_array(values: pa.Array | pa.ChunkedArray, *, plan: ColumnSeed) -> pa.Array:
    """Redact PII spans per cell, byte-identical to the oracle handler."""
    cfg = provider_config_to_dict(plan.provider_config)
    token = cfg.get("token", _DEFAULT_TOKEN)
    label_token = bool(cfg.get("label_token", False))
    if not isinstance(token, str):
        # Handler passes the column through unchanged for a non-string token.
        return _passthrough_values(values)

    detector_ids, ok = _text_redact_detector_ids(cfg.get("detectors"))
    if not ok:
        return _passthrough_values(values)

    ner_model, ner_entities = _text_redact_ner(cfg, plan)

    out: list[str | None] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        text = value if isinstance(value, str) else str(value)
        extra: list[Span] | None = None
        if ner_model is not None:
            from decoy_engine.storm.ner import iter_ner_spans

            extra = iter_ner_spans(text, model=ner_model, entities=ner_entities)
        spans = iter_spans(text, detector_ids, extra_spans=extra)
        out.append(text if not spans else _splice(text, spans, token, label_token))
    return pa.array(out, type=pa.string())


def _text_redact_ner(cfg: dict[str, Any], plan: ColumnSeed) -> tuple[str | None, list[str] | None]:
    """Resolve NER config + enforce the compile-vs-runtime model-version pin,
    mirroring `_text_redact.TextRedactHandler.run` (same `ner_model_version_mismatch`)."""
    ner_cfg = cfg.get("ner")
    if not ner_cfg:
        return None, None
    from decoy_engine.storm.ner import DEFAULT_NER_MODEL

    ner_model: str
    ner_entities: list[str] | None = None
    if isinstance(ner_cfg, dict):
        ner_model = str(ner_cfg.get("model") or DEFAULT_NER_MODEL)
        raw_entities = ner_cfg.get("entities")
        if isinstance(raw_entities, (list, tuple)) and raw_entities:
            ner_entities = [str(e) for e in raw_entities]
    else:
        ner_model = DEFAULT_NER_MODEL

    if plan.ner_model_version is not None:
        from decoy_engine.storm.ner import installed_model_version

        current_version = installed_model_version(ner_model)
        if current_version is not None and current_version != plan.ner_model_version:
            raise StrategyError(
                code="ner_model_version_mismatch",
                strategy="text_redact",
                message=(
                    f"NER model {ner_model!r} installed at {current_version!r} but the plan "
                    f"was compiled against {plan.ner_model_version!r}; recompile to keep "
                    "redactions reproducible."
                ),
            )
    return ner_model, ner_entities


def _passthrough_values(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    if isinstance(values, pa.ChunkedArray):
        return values.combine_chunks()
    return values


# ---------------------------------------------------------------------------
# categorical
# ---------------------------------------------------------------------------


def _categorical_config(cfg: dict[str, Any]) -> tuple[list[Any], list[float] | None]:
    """Resolve + validate categories/weights, fail-closed with the oracle's codes."""
    raw_categories = cfg.get("categories")
    if (
        not cfg.get("from_profile")
        and raw_categories is not None
        and not isinstance(raw_categories, (list, tuple))
    ):
        raise StrategyError(
            code="categorical_categories_not_list",
            strategy="categorical",
            message=(
                f"categorical categories={raw_categories!r} "
                f"({type(raw_categories).__name__}) is not a list."
            ),
        )
    categories = list(cfg.get("categories", []))
    if not categories:
        raise StrategyError(
            code="categorical_requires_categories",
            strategy="categorical",
            message="categorical strategy provided no categories.",
        )
    weights_raw = cfg.get("weights")
    weights: list[float] | None = None
    if weights_raw is not None:
        if not isinstance(weights_raw, (list, tuple)) or len(weights_raw) != len(categories):
            raise StrategyError(
                code="categorical_weights_shape",
                strategy="categorical",
                message=(
                    f"categorical weights must be a list the same length as categories "
                    f"({len(categories)})."
                ),
            )
        weights = [float(w) for w in weights_raw]
    return categories, weights


def categorical_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    job_seed: bytes,
    namespace: str | None,
    deterministic: bool,
    cfg: dict[str, Any],
) -> pa.Array:
    """Remap each non-null value onto the category pool, byte-identical to the oracle.

    Only the deterministic (source-conditioned) path is reachable out-of-core;
    the compat gate rejects non-deterministic categorical (it draws from an
    unseeded RNG, so it has no cross-run/cross-route parity).
    """
    categories, weights = _categorical_config(cfg)
    if not deterministic:
        raise ExecutionError(
            code="out_of_core_categorical_nondeterministic_unsupported",
            message="out-of-core categorical requires deterministic mode (gate should reject).",
        )
    if namespace is None:
        raise StrategyError(
            code="categorical_requires_namespace",
            strategy="categorical",
            message="deterministic categorical requires a namespace.",
        )

    out: list[Any] = []
    if weights is None:
        pool_size = len(categories)
        for value in _array_to_pylist(values):
            if _is_missing(value):
                out.append(None)
                continue
            idx = derive_index(
                job_seed, namespace, _canonicalize_source(value), pool_size=pool_size
            )
            out.append(categories[idx])
    else:
        cdf = _build_cdf(weights)
        for value in _array_to_pylist(values):
            if _is_missing(value):
                out.append(None)
                continue
            bucket = derive_index(
                job_seed, namespace, _canonicalize_source(value), pool_size=_WEIGHTED_CDF_RES
            )
            cat_idx = bisect.bisect_right(cdf, bucket)
            if cat_idx >= len(categories):
                cat_idx = len(categories) - 1
            out.append(categories[cat_idx])
    return pa.array(out, from_pandas=True)


# ---------------------------------------------------------------------------
# analytic output types (schema resolved before any batch is seen)
# ---------------------------------------------------------------------------


def group_b_output_type(
    seed: ColumnSeed,
    cfg: dict[str, Any],
    source_type: pa.DataType | None,
) -> pa.DataType:
    """The Arrow type a Group (b) strategy emits, resolved from the plan alone.

    Fail-closed the same way the mask would (invalid config raises the oracle's
    codes here too), so a streaming consumer never fixes a schema for a config
    the mask would reject.
    """
    if seed.strategy == "fpe":
        _fpe_charset(cfg)  # validate-only; a valid fpe always emits string.
        return pa.string()
    if seed.strategy == "text_redact":
        token = cfg.get("token", _DEFAULT_TOKEN)
        if not isinstance(token, str):
            if source_type is None:
                raise ExecutionError(
                    code="out_of_core_source_schema_required",
                    message="text_redact with a non-string token passes the source type through.",
                )
            return source_type
        return pa.string()
    # categorical: the masked type is the category values' type.
    categories, _weights = _categorical_config(cfg)
    return pa.array(categories, from_pandas=True).type
