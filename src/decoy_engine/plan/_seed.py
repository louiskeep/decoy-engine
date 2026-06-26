"""Single shared job-seed normalizer (QA F5, 2026-06-26).

The pipeline profile path (`execution/_pipeline.py`), the plan compiler
(`plan/_compile.py`), generation (`generation/synthesize.py`), unmask, and
the vault all route their config-side `seed` value through these two
functions so a `bool`/`float` seed is rejected identically everywhere
instead of one path coercing `seed: true` to `1` while another raises.

Lives in its own module rather than inside `_compile.py` so the validator
has a single import home and `_compile.py` stays within its size budget.
"""

from __future__ import annotations

from typing import Any

from decoy_engine.plan._errors import PlanCompileError


def _normalize_job_seed_int(config: dict[str, Any]) -> int:
    """Normalize the config-side `seed` value to the validated int form.

    This is the single shared seed validator (QA F5, 2026-06-26). The
    pipeline profile path (`execution/_pipeline.py`), the plan compiler,
    and generation (`generation/synthesize.py`) all route through it so a
    `bool`/`float` seed is rejected identically everywhere instead of one
    path coercing `seed: true` to `1` while another raises. The 8-byte
    `bytes` form that `decoy_engine.determinism.derive(...)` consumes is
    produced by the thin `_normalize_job_seed` wrapper below.

    Per S3 spec §5.5 (resolution of B2 + H1): the int -> bytes conversion
    happens exactly once at the pipeline-config adapter boundary. The
    rest of the engine consumes `bytes` only.

    Default + missing-key handling: when `global_settings.seed` is
    absent OR explicitly set to None, the seed defaults to 0. Any
    other non-numeric value (a string that does not parse as an int,
    a bool, a float, a dict, a list) raises `seed_not_numeric` instead
    of silently coercing to a plausible int. The bool + float guards
    (QA-3 F1, 2026-05-31) block PyYAML's `seed: true/yes/no` (parsed
    as Python bool, `int(True) = 1`) and `seed: 1.5` (`int(1.5) = 1`,
    silent truncation) which had passed the pre-QA-3 `int()` coercion
    silently.

    Raises:
        PlanCompileError(code='seed_not_numeric') when the seed is
            present but cannot be coerced to int.
        PlanCompileError(code='seed_overflow') when the int does not
            fit in unsigned 64-bit (the size of the bytes form).
    """
    global_settings = config.get("global_settings", {}) or {}
    if "seed" not in global_settings:
        seed_int = 0
    else:
        job_seed_raw = global_settings.get("seed")
        if job_seed_raw is None:
            seed_int = 0
        else:
            # QA-3 F1 (2026-05-31): bool + float guards. Python evaluates
            # `int(True) = 1` and `int(False) = 0`, and `int(1.5) = 1`,
            # so without these guards PyYAML's `seed: yes/no/true/false`
            # and `seed: 1.5` silently coerced to plausible integers.
            # Two pipelines with intentionally different malformed seeds
            # would compile to byte-identical plans.
            if isinstance(job_seed_raw, bool):
                raise PlanCompileError(
                    code="seed_not_numeric",
                    path="global_settings.seed",
                    message=(
                        f"seed must be an integer; got bool {job_seed_raw!r}. "
                        "If your YAML has `seed: true` or `seed: yes`, PyYAML "
                        "parses it as a Python bool, not an int. Use an "
                        "explicit integer like `seed: 1` instead."
                    ),
                )
            if isinstance(job_seed_raw, float):
                raise PlanCompileError(
                    code="seed_not_numeric",
                    path="global_settings.seed",
                    message=(
                        f"seed must be an integer; got float {job_seed_raw!r}. "
                        "A float seed would silently truncate to the integer "
                        "part. Use an explicit integer instead."
                    ),
                )
            try:
                seed_int = int(job_seed_raw)
            except (TypeError, ValueError) as exc:
                raise PlanCompileError(
                    code="seed_not_numeric",
                    path="global_settings.seed",
                    message=(
                        f"seed must be numeric (int or int-coercible); got "
                        f"{type(job_seed_raw).__name__} {job_seed_raw!r}."
                    ),
                ) from exc
    if not 0 <= seed_int < (1 << 64):
        raise PlanCompileError(
            code="seed_overflow",
            path="global_settings.seed",
            message=(f"seed must fit in unsigned 64-bit (range [0, 2**64)); got {seed_int}"),
        )
    return seed_int


def _normalize_job_seed(config: dict[str, Any]) -> bytes:
    """Normalize `seed` to the 8-byte big-endian form `derive(...)` consumes."""
    return _normalize_job_seed_int(config).to_bytes(8, "big")


def job_seed_for_config(config: dict[str, Any]) -> bytes:
    """The 8-byte job seed ``config`` resolves to (public).

    The same value `VaultWriter` and `unmask_pipeline` key on, exposed so an
    external caller (e.g. a CLI vault inspector) can run
    ``load_vault(path, job_seed_for_config(config))`` without reaching into the
    private normalizer. A `bool`/`float`/out-of-range seed is rejected here
    exactly as it is on the run path.
    """
    return _normalize_job_seed(config)
