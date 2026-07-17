"""Group (c) out-of-core mask strategies (SC4): text_mask, code_set, bucket_perturb.

Ports the full-frame V2 handlers' per-value logic onto the out-of-core streaming
route, byte-identical to the pandas oracle for the admitted shapes. Each kernel
here is per-value deterministic and null-preserving, so masking one RecordBatch
equals the same rows of a whole-column mask -- the single-kernel parity contract
`_mask.py` documents. Split out of `_mask.py` (sibling of `_mask_group_b.py`) by
concern to hold the ~600 LOC orchestration cap (CLAUDE.md engineering best
practices).

Established methodology (CLAUDE.md core rule): none of these invent a new
approach; each reuses the SAME primitive the full-frame handler cites, so the
out-of-core output is byte-identical rather than merely similar.

- text_mask: reuses `transforms.text_mask.mask_cell` (HMAC-SHA256 keyed span
  masking, RFC 2104; the exact primitive `_strategies/_text_mask.TextMaskHandler`
  calls). The mask key is HMAC(job_seed, matched_text) per span -- a pure
  function of (job_seed, cell), with no cross-row state, so it chunks cleanly.
- code_set (mask mode, no chapter_preserve): reuses
  `transforms.code_set.apply_code_set` (HMAC-SHA256-keyed modular selection over
  the code-sorted corpus). Mask mode is `HMAC(salt, value) % candidate_count`, a
  pure per-value function; the compat gate restricts admission to mask mode
  without chapter_preserve (see below).
- bucket_perturb (explicit date_format): reuses
  `transforms.bucket_perturb.apply_bucket_perturb` (HKDF-SHA256 keyed offset via
  `derive(job_seed, namespace, value)`). Per-value once the strptime format is
  fixed; the compat gate requires an explicit `date_format` so no whole-column
  format detection is needed (see below).

Deliberately NOT ported (documented gate-enforced routing MISS in `_compat.py`),
each for a concrete reason that would otherwise cause a route-dependent
divergence. A MISS means the job falls back to sequential/full-frame (which
handle them), never a wrong output:

- code_set gen mode and chapter_preserve. Gen mode threads the GLOBAL row index
  into the RNG seed (`derive_index(..., row_index)`); the out-of-core mask kernel
  masks each batch in isolation with no global row offset, so a batch-local index
  would restart per batch and diverge. chapter_preserve records per-value
  `PlanCompileError`s (`code_set_chapter_absent`, `code_set_sole_member_bucket`)
  that the full-frame path removes via the D8 quarantine pass; the out-of-core
  route returns before that pass and has no row-error/quarantine channel (the
  same wall SC3's `bucketize`/`date_shift` hit).
- bucket_perturb without an explicit `date_format`. `apply_bucket_perturb` falls
  back to `date_shift._detect_format` over the WHOLE series, which does not chunk
  (a batch could detect a different format than the full column). Requiring an
  explicit format keeps the transform per-value and byte-parity-able.
- geo_generalize. Its k-anonymity cascade (ZIP5/ZIP3/state or H3 resolutions)
  thresholds each row on the count of records sharing that generalization ACROSS
  THE WHOLE DATASET; a chunk sees only partial counts, so the cascade decision is
  route-dependent. Not batch-local by construction.
- formula and derived. Both produce a value whose Arrow type is not analytically
  determinable from the plan (the expression can change the column's type), which
  the out-of-core route's fixed-output-schema requirement cannot satisfy; formula
  additionally carries an order-dependent RNG channel (randint/choice/random/gauss
  re-seeded per batch) and derived additionally needs same-row sibling-column
  context the per-column mask kernel does not receive.
- nested. Reuses the full pandas child-strategy dispatch (SCALAR_HANDLERS) plus a
  per-cell JSON round-trip; porting it requires running the pandas handler stack
  per batch and statically bounding the child strategy to the batch-local set --
  an architectural port beyond dispatch-widening, deferred like SC3's `faker`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._strategies._code_set import _PER_VALUE_CODE_SET_ERRORS
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.bucket_perturb import (
    apply_bucket_perturb,
    validate_bucket_perturb_config,
)
from decoy_engine.transforms.code_set import CodeSetConfig, apply_code_set
from decoy_engine.transforms.text_mask import mask_cell

if TYPE_CHECKING:
    import pandas as pd

    from decoy_engine.plan._types import ColumnSeed
    from decoy_engine.transforms._codeset_loader import _CorpusRecord

# Strategies with a per-value out-of-core kernel below. Admission is gated in
# `_compat.py`; code_set and bucket_perturb are admitted only for the config
# shapes the kernel reproduces byte-for-byte (see module docstring + `_compat`).
GROUP_C_STRATEGIES = frozenset({"text_mask", "code_set", "bucket_perturb"})


def group_c_array(
    values: pa.Array | pa.ChunkedArray,
    seed: ColumnSeed,
    job_seed: bytes,
    *,
    column: str | None,
    cfg: dict[str, Any],
    corpus_record: _CorpusRecord | None = None,
) -> pa.Array:
    """Dispatch one admitted Group (c) strategy to its per-value kernel.

    `corpus_record` (Codex round-6 P2 MASKING/EVIDENCE VERSION DIVERGENCE
    remediation) is forwarded only to the `code_set` kernel -- see
    `_code_set_array`'s docstring. The other Group (c) kernels ignore it.
    """
    if seed.strategy == "text_mask":
        return _text_mask_array(values, job_seed=job_seed, cfg=cfg)
    if seed.strategy == "code_set":
        return _code_set_array(
            values,
            job_seed=job_seed,
            namespace=seed.namespace,
            cfg=cfg,
            corpus_record=corpus_record,
        )
    if seed.strategy == "bucket_perturb":
        return _bucket_perturb_array(values, job_seed=job_seed, namespace=seed.namespace, cfg=cfg)
    raise ExecutionError(
        code="out_of_core_strategy_unsupported",
        message=f"strategy {seed.strategy!r} is not a Group (c) out-of-core kernel.",
    )


# ---------------------------------------------------------------------------
# text_mask
# ---------------------------------------------------------------------------


def _text_mask_array(
    values: pa.Array | pa.ChunkedArray, *, job_seed: bytes, cfg: dict[str, Any]
) -> pa.Array:
    """Span-level PII mask each non-null cell, byte-identical to the oracle.

    Mirrors `_strategies/_text_mask.TextMaskHandler.run` exactly: same config
    resolution (detectors, per_detector_strategy, unmatched_span_policy, token,
    min_days/max_days), same non-string -> str coercion, and the same reused
    `mask_cell` primitive. `mask_cell` never raises on bad data (fpe failure ->
    token, unparseable date -> passthrough), so there is no quarantine channel to
    reproduce.
    """
    detectors_raw = cfg.get("detectors")
    detector_ids: list[str] | None
    if isinstance(detectors_raw, (list, tuple)):
        detector_ids = [str(d) for d in detectors_raw] or None
    else:
        detector_ids = None
    per_detector: dict[str, str] = dict(cfg.get("per_detector_strategy") or {})
    policy = str(cfg.get("unmatched_span_policy", "redact"))
    token = str(cfg.get("token", "[REDACTED]"))
    extra: dict[str, Any] = {key: cfg[key] for key in ("min_days", "max_days") if key in cfg}

    out: list[Any] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        text = value if isinstance(value, str) else str(value)
        out.append(
            mask_cell(
                text,
                job_seed,
                detector_ids=detector_ids,
                strategy_map=per_detector or None,
                unmatched_span_policy=policy,
                token=token,
                cfg=extra or None,
            )
        )
    return pa.array(out, type=pa.string())


# ---------------------------------------------------------------------------
# code_set (mask mode, no chapter_preserve)
# ---------------------------------------------------------------------------


def _code_set_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    job_seed: bytes,
    namespace: str | None,
    cfg: dict[str, Any],
    corpus_record: _CorpusRecord | None = None,
) -> pa.Array:
    """Corpus-remap each non-null value, byte-identical to the oracle handler.

    Only mask mode without chapter_preserve is reachable out-of-core (the compat
    gate rejects gen mode and chapter_preserve). Under that shape `apply_code_set`
    is a pure per-value `HMAC(salt, value) % candidate_count` selection with no
    row-index or cross-row state, so it chunks cleanly. A per-value
    `PlanCompileError` here can only be a corpus/config-level defect
    (`code_set_single_row_corpus`) which the oracle re-raises job-fatal too; the
    per-value quarantine codes (`_PER_VALUE_CODE_SET_ERRORS`) are unreachable
    without chapter_preserve, so failing closed (raising) rather than keeping the
    raw value can never leak.

    `corpus_record` (Codex round-6 P2 MASKING/EVIDENCE VERSION DIVERGENCE
    remediation): the out-of-core runner streams a table batch-at-a-time, so
    without a pinned record every batch (and every value within a batch)
    would independently re-resolve the corpus from the cache -- a customer
    corpus file replaced mid-stream could then mask different batches off
    different versions, and disagree with the evidence stamped once per table
    before the first batch. `_runner.py` resolves the record ONCE per table
    (before streaming starts) and threads it down through every batch's
    `mask_batch` call so the whole table stream -- and the evidence -- share
    one version. `None` falls back to a fresh per-value resolve, preserving
    every existing (non-runner) caller.
    """
    mode = str(cfg.get("mode", "mask"))
    if mode != "mask" or cfg.get("chapter_preserve"):
        raise ExecutionError(
            code="out_of_core_code_set_shape_unsupported",
            message="out-of-core code_set requires mask mode without chapter_preserve "
            "(gate should reject).",
        )
    code_cfg = CodeSetConfig.from_dict(cfg)
    out: list[Any] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            out.append(None)
            continue
        try:
            out.append(
                apply_code_set(
                    str(value),
                    code_cfg,
                    mode=mode,
                    job_seed=job_seed,
                    namespace=namespace,
                    corpus_record=corpus_record,
                )
            )
        except PlanCompileError as exc:
            # Defense-in-depth: the admitted shape cannot raise a per-value code
            # (those need chapter_preserve, which the gate rejects). If one ever
            # surfaced, fail closed rather than leak the raw value.
            if exc.code in _PER_VALUE_CODE_SET_ERRORS:
                raise ExecutionError(
                    code="out_of_core_code_set_row_error_unreachable",
                    message=f"code_set raised a per-value defect ({exc.code}) on a shape the "
                    "gate should have rejected; failing closed.",
                ) from exc
            raise  # corpus/config-level defect: job-fatal, same as the oracle.
    return pa.array(out, type=pa.string())


# ---------------------------------------------------------------------------
# bucket_perturb (explicit date_format)
# ---------------------------------------------------------------------------


def _bucket_perturb_array(
    values: pa.Array | pa.ChunkedArray,
    *,
    job_seed: bytes,
    namespace: str | None,
    cfg: dict[str, Any],
) -> pa.Array:
    """Snap each date to a deterministic in-bucket position, oracle-identical.

    Mirrors `_strategies/_bucket_perturb.BucketPerturbStrategyHandler.run`:
    namespace-required, validate the bucket fail-closed, then reuse
    `apply_bucket_perturb` on the batch's values as a pandas Series. The compat
    gate requires an explicit `date_format`, so `apply_bucket_perturb` never runs
    whole-column format detection and the per-batch result equals the whole-column
    result for the same rows. Null / unparseable values pass through unchanged (no
    quarantine channel needed).
    """
    if namespace is None:
        raise StrategyError(
            code="bucket_perturb_requires_namespace",
            strategy="bucket_perturb",
            message="bucket_perturb strategy requires a namespace.",
        )
    bucket = str(cfg.get("bucket", "month"))
    date_format: str | None = cfg.get("date_format") or None
    if date_format is None:
        raise ExecutionError(
            code="out_of_core_bucket_perturb_autodetect_unsupported",
            message="out-of-core bucket_perturb requires an explicit date_format "
            "(gate should reject); whole-column format detection does not chunk.",
        )
    try:
        validate_bucket_perturb_config({**cfg, "bucket": bucket})
    except ValueError as exc:
        raise StrategyError(
            code="bucket_perturb_invalid_config",
            strategy="bucket_perturb",
            message=str(exc),
        ) from exc

    series = _to_series(values)
    perturbed = apply_bucket_perturb(
        series,
        bucket=bucket,
        job_seed=job_seed,
        namespace=namespace,
        date_format=date_format,
    )
    return pa.array(perturbed, from_pandas=True)


# ---------------------------------------------------------------------------
# analytic output types (schema resolved before any batch is seen)
# ---------------------------------------------------------------------------


def group_c_output_type(seed: ColumnSeed, cfg: dict[str, Any]) -> pa.DataType:
    """The Arrow type a Group (c) strategy emits, resolved from the plan alone.

    All three admitted kernels emit a string column (masked free text, a corpus
    code, or a reformatted date). Config is validated fail-closed the same way the
    mask would, so a streaming consumer never fixes a schema for a config the mask
    would reject.
    """
    if seed.strategy == "code_set":
        CodeSetConfig.from_dict(cfg)  # validate-only; a valid code_set emits string.
    elif seed.strategy == "bucket_perturb":
        bucket = str(cfg.get("bucket", "month"))
        try:
            validate_bucket_perturb_config({**cfg, "bucket": bucket})
        except ValueError as exc:
            raise StrategyError(
                code="bucket_perturb_invalid_config",
                strategy="bucket_perturb",
                message=str(exc),
            ) from exc
    return pa.string()


def _to_series(values: pa.Array | pa.ChunkedArray) -> pd.Series:
    """Convert one column (or batch slice) to a pandas Series like the oracle.

    The pandas oracle builds each source frame with `pa.Table.to_pandas()`; a
    single string column converts identically via `Array.to_pandas()`, so the
    Series the Series-based transforms (`apply_bucket_perturb`) see is the same
    one the oracle's handler sees for the same rows.
    """
    if isinstance(values, pa.ChunkedArray):
        values = values.combine_chunks()
    return values.to_pandas()


__all__ = [
    "GROUP_C_STRATEGIES",
    "group_c_array",
    "group_c_output_type",
]
