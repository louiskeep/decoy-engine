"""Faker pool determinism harness (plan_faker_determinism_harness_v2.md, C2):
one fresh-process build of one (faker_type, locale, kwargs) candidate.

Constructs ONE production-equivalent `GenDeriveContext` (the same
`GenDeriveContext.for_column` call `_faker` makes, over a column config
shaped exactly like a real pooled column), derives the build and selection
seeds from it, and calls `_faker_pool._build_and_sample_returning_pool`
EXACTLY ONCE -- the internal helper `build_and_sample` itself delegates to
(`decoy_engine/generation/_faker_pool.py`), so this worker exercises the
real production code path, not a reimplementation. Digesting a real
`build_and_sample` output would only prove selection determinism; digesting
`build_pool_values`'s full ordered pool (produced by the SAME call) also
catches drift in a pool position `PoolSampler` never happened to draw.

Before any of that, this worker deliberately perturbs the `random` module
and numpy's LEGACY global RNG (`numpy.random.seed`) with process-local
entropy. Production's pool build and selection never read either -- the
build seeds a fresh `Faker` instance directly, and `PoolSampler` uses
`numpy.random.default_rng`, a separate stream. If a future change
accidentally introduced a dependency on either global generator, K
subprocesses with different perturbations would produce K different
digests, and `check_determinism.py` would correctly report the candidate as
non-deterministic -- this perturbation exists to make that leak visible,
not to change any real output.

Usage: python pool_determinism_worker.py <faker_type> <locale|-> [kwargs_json]

Prints one line: `POOL_DETERMINISM_JSON <json>`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import subprocess
import sys
from typing import Any

import numpy as np

# `digest_codec` is a sibling file in this same directory; the interpreter
# already put this script's own directory at sys.path[0] before running it,
# so this import needs no path setup of its own. Everything under
# `decoy_engine` needs the DRIVER-supplied `PYTHONPATH` (explicit env, not a
# sys.path hack here) -- matching `scripts/bench-generation-pool/
# bench_worker_gen.py`'s convention, so the same env-controlled-subprocess
# pattern check_determinism.py already knows how to drive is the only place
# `src` gets onto the path.
from digest_codec import CODEC_VERSION, _pool_digest

# Perturb both global generators BEFORE importing/using anything else, so a
# hidden dependency on either shows up as soon as it is read, not only if
# some later import path happens to touch them first.
random.seed(os.urandom(8))
np.random.seed(int.from_bytes(os.urandom(4), "big"))  # legacy global RNG only

from faker import VERSION as _FAKER_VERSION  # noqa: E402

from decoy_engine import __version__ as _engine_version  # noqa: E402
from decoy_engine.generation import _faker_pool  # noqa: E402
from decoy_engine.generation.pool._runtime_pool_size import DEFAULT_POOL_SIZE  # noqa: E402
from decoy_engine.generators.derivation import GenDeriveContext  # noqa: E402
from decoy_engine.internal.faker_setup import make_faker  # noqa: E402

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fixed across every invocation of this worker, in every process, forever:
# the unkeyed GenDeriveContext fallback path mixes this into the column
# root (GenDeriveContext.for_column), so changing it would reseed every
# candidate the harness has ever certified. Not a job seed a caller can
# override -- this harness proves determinism for ONE frozen workload.
_FALLBACK_SEED = 20260918

# The output-selection sample size. Independent of pool_size (which mirrors
# production's DEFAULT_POOL_SIZE exactly, since a pool built at a different
# size is not the same build); this only needs to be big enough that a
# `PoolSampler` bug touching most of the selection stream would surface.
_N_SELECTED = 2_000


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 -- fixed console-script invocation
            cwd=ENGINE_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except OSError:
        pass
    return "unknown"


def _hex_digest_of_pool(values: list[Any] | None) -> str | None:
    if values is None:
        return None
    return hashlib.sha256(_pool_digest(values)).hexdigest()


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit("usage: pool_determinism_worker.py <faker_type> <locale|-> [kwargs_json]")
    faker_type = sys.argv[1]
    requested_locale: str | None = None if sys.argv[2] == "-" else sys.argv[2]
    kwargs_raw = sys.argv[3] if len(sys.argv) == 4 else "{}"
    try:
        kwargs = json.loads(kwargs_raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"kwargs_json is not valid JSON: {exc}") from exc
    if not isinstance(kwargs, dict):
        raise SystemExit(f"kwargs_json must decode to a JSON object, got {type(kwargs).__name__}")

    effective_locale = requested_locale  # this harness never sets an instance-default locale

    col = {
        "name": "val",
        "type": "faker",
        "faker_type": faker_type,
        "locale": requested_locale,
        "faker_kwargs": kwargs,
    }
    gen_ctx = GenDeriveContext.for_column(
        derive_key=None, column_config=col, fallback_seed=_FALLBACK_SEED
    )
    build_seed = gen_ctx.family_bytes(_faker_pool._BUILD_FAMILY)[:8]
    selection_seed = gen_ctx.family_bytes(_faker_pool._SELECTION_FAMILY)[:8]

    # Read-only metadata probe: which locale Faker actually resolved to.
    # `make_faker` falls back to en_US on an invalid locale string; this
    # construction is never seeded and never fed into the real build below,
    # so it cannot influence the digested pool or output -- see the module
    # docstring's "ONE build" contract.
    resolved_locale = make_faker(effective_locale).locales[0]

    pool_values, sampled_output, exact_name_available, custom_override_present = (
        _faker_pool._build_and_sample_returning_pool(
            faker_type=faker_type,
            faker_kwargs=kwargs,
            n=_N_SELECTED,
            build_seed=build_seed,
            selection_seed=selection_seed,
            effective_locale=effective_locale,
            pool_size=DEFAULT_POOL_SIZE,
        )
    )

    record: dict[str, Any] = {
        "pool_digest": _hex_digest_of_pool(pool_values),
        "output_digest": _hex_digest_of_pool(sampled_output),
        "exact_name_available": exact_name_available,
        "custom_override_present": custom_override_present,
        "meta": {
            "engine_version": _engine_version,
            "engine_commit": _git_commit(),
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "faker_version": _FAKER_VERSION,
            "numpy_version": np.__version__,
            "platform": platform.platform(),
            "arch": platform.machine(),
            "requested_locale": requested_locale,
            "effective_locale": effective_locale,
            "resolved_locale": resolved_locale,
            "faker_type": faker_type,
            "kwargs": kwargs,
            "build_seed_hex": build_seed.hex(),
            "selection_seed_hex": selection_seed.hex(),
            "pool_size": DEFAULT_POOL_SIZE,
            "n": _N_SELECTED,
            "codec_version": CODEC_VERSION,
            "fallback_seed": _FALLBACK_SEED,
        },
    }
    print("POOL_DETERMINISM_JSON " + json.dumps(record))


if __name__ == "__main__":
    main()
