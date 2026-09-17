"""GP1 generation-throughput probe worker: one arm, one rep.

Times `decoy_engine.generation.synthesize.generate_tables(plan)` masking
one `type: faker` column of `n_rows` rows through EITHER the pooled path
or the per-row path, so `bench_compare_gen.py` (this directory) can measure
the real pooled-vs-per-row crossover GP2 shipped a placeholder
`N_THRESHOLD` for (`src/decoy_engine/generation/_faker_pool.py`). See this
directory's README.md for the full harness design and the load-bearing
threshold-override rationale.

Arm selection (env `GEN_POOL_BENCH_FLAG`, "off"|"on", default "on"):
"off" sets the column's `pooled: false` (always per-row, matching
production's operator opt-out); "on" omits `pooled` entirely, so the
column pools whenever `pool_eligible` says yes. Production only says yes
at `n >= N_THRESHOLD` (currently a 50,000 placeholder), which would make
every tier below that placeholder look identical between arms and defeat
the probe. `GEN_POOL_BENCH_THRESHOLD`, when set, is assigned onto the
`_faker_pool` MODULE object as `N_THRESHOLD` before generation runs, in
THIS process only -- `pool_eligible`'s `n >= N_THRESHOLD` read is a
same-module global lookup, so the reassignment takes effect for every
column `try_pool` gates in this process, with no `src/` change. The sweep
driver sets it to 1 so the "on" arm pools at every measured tier.

Two independent guards make a mismeasurement fail loud instead of quietly
reporting a wrong number:
- a PREFLIGHT resolver check (before timing) that every pool-eligible
  faker type this worker could be asked for resolves under the default
  locale with no custom-provider override -- the frozen-workload
  requirement below;
- a VACUITY guard (around the timed call) that observes whether the pool
  bridge actually ran, rather than trusting the arm's own intent.

Frozen workload: no custom Faker provider is registered and no per-column
locale override is set, so every measurement uses the plain installed
default locale -- the one axis (locale/custom-provider) that can make the
locked resolver silently decline pooling and fall back to per-row.

Usage: python bench_worker_gen.py <n_rows> <faker_type>
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation import _faker_pool
from decoy_engine.generation.pool import _sampler as _pool_sampler
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.internal.faker_setup import make_faker, resolve_pool_provider
from decoy_engine.plan import compile_plan
from decoy_engine.profile import Profile

# Fixed constant (not derived from wall-clock/PID/etc): every worker
# invocation across both arms and every tier masks under the SAME job
# seed, so a wall-time difference reflects the pooled/per-row code path,
# never a seed-dependent data-shape difference.
_SEED = 20260917

_FLAG_ENV = "GEN_POOL_BENCH_FLAG"
_THRESHOLD_ENV = "GEN_POOL_BENCH_THRESHOLD"

# Which arm this invocation measures: "off" (the env's one recognized
# opt-out spelling) is per-row; anything else, INCLUDING the var being
# unset, is "on" -- mirrors bench_worker_unified.py's own default-on
# convention so the driver never relies on an implicit default silently
# agreeing between two independently-read scripts.
_FLAG_ON = os.environ.get(_FLAG_ENV, "on").strip().lower() != "off"


def _empty_profile() -> Profile:
    """No mask columns/relationships to profile for a pure-generate table
    (mirrors `tests/unit/_dps_helpers.py`'s helper of the same shape)."""
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="gp1-bench",
    )


def _build_config(n_rows: int, faker_type: str, *, flag_on: bool) -> dict[str, Any]:
    """One generate table, one `type: faker` column. `pooled: false` on the
    off arm (always per-row, the one operator-facing opt-out); omitted on
    the on arm so `pool_eligible`'s threshold gate is the only decider."""
    column: dict[str, Any] = {"name": "val", "type": "faker", "faker_type": faker_type}
    if not flag_on:
        column["pooled"] = False
    return {
        "version": 1,
        "global_settings": {"seed": _SEED},
        "sources": {},
        "tables": [{"name": "t", "row_count": n_rows, "generate_columns": [column]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def preflight_verify_allowlisted_types() -> None:
    """Every pool-eligible faker type must resolve to a real, non-custom
    provider under the plain default locale -- the frozen-workload
    requirement (module docstring). Runs once per invocation, before any
    timing: a locale/custom-provider drift that would make the locked
    resolver decline mid-sweep is caught here with a clear message,
    instead of surfacing later as a vacuity-guard mismatch that does not
    say WHY the resolver declined.
    """
    faker_inst = make_faker(None)
    for faker_type in sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES):
        provider_callable, exact_name_available, custom_override_present = resolve_pool_provider(
            faker_inst, faker_type
        )
        if custom_override_present or not exact_name_available or provider_callable is None:
            raise SystemExit(
                f"preflight: allowlisted faker type {faker_type!r} does not resolve to a "
                f"real default-locale provider (exact_name_available={exact_name_available!r}, "
                f"custom_override_present={custom_override_present!r}) -- the frozen-workload "
                "assumption this harness requires no longer holds."
            )


def install_vacuity_guards() -> tuple[dict[str, bool], Any]:
    """Worker-local in-process wrappers (module docstring): reassign the
    plain module/class attributes `_faker_pool.build_and_sample` and
    `PoolSampler.sample` to fired-flag-setting wrappers that call straight
    through to the originals. `try_pool` (in `_faker_pool.py`) calls
    `build_and_sample` as a same-module global lookup, and `build_and_sample`
    calls `PoolSampler().sample(...)` through the shared class object, so
    reassigning these two attributes is visible to both call sites with no
    `src/` change -- exactly the same mechanism the `N_THRESHOLD` override
    above relies on. Returns `(fired, restore)`; the caller MUST call
    `restore()` in a `finally` so a later invocation in the same process
    (there is none today, but a future test harness might import this
    module) never inherits a stuck wrapper.
    """
    fired = {"build_and_sample": False, "pool_sample": False}
    original_build_and_sample = _faker_pool.build_and_sample
    original_pool_sample = _pool_sampler.PoolSampler.sample

    def _wrapped_build_and_sample(*args: Any, **kwargs: Any) -> Any:
        fired["build_and_sample"] = True
        return original_build_and_sample(*args, **kwargs)

    def _wrapped_pool_sample(self: Any, *args: Any, **kwargs: Any) -> Any:
        fired["pool_sample"] = True
        return original_pool_sample(self, *args, **kwargs)

    _faker_pool.build_and_sample = _wrapped_build_and_sample
    _pool_sampler.PoolSampler.sample = _wrapped_pool_sample  # type: ignore[method-assign]

    def _restore() -> None:
        _faker_pool.build_and_sample = original_build_and_sample
        _pool_sampler.PoolSampler.sample = original_pool_sample  # type: ignore[method-assign]

    return fired, _restore


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: bench_worker_gen.py <n_rows> <faker_type>")
    n_rows = int(sys.argv[1])
    faker_type = sys.argv[2]

    # Closed allowlist (module docstring / README): a non-allowlisted type
    # is never pool-eligible, so timing it would only ever measure the
    # per-row path under a misleading "pooled" arm label.
    if faker_type not in _faker_pool.POOL_ELIGIBLE_FAKER_TYPES:
        raise SystemExit(
            f"faker_type {faker_type!r} is not in the closed pool-eligible allowlist "
            f"{sorted(_faker_pool.POOL_ELIGIBLE_FAKER_TYPES)}"
        )

    threshold_env = os.environ.get(_THRESHOLD_ENV)
    threshold_override: int | None = None
    if threshold_env is not None:
        threshold_override = int(threshold_env)
        # Same-process, same-module-global reassignment (module docstring);
        # never touches `src/`. Applies regardless of arm -- the off arm's
        # `pooled: false` opt-out is unconditional and does not consult
        # `N_THRESHOLD` at all, so overriding it here cannot change the off
        # arm's behavior.
        _faker_pool.N_THRESHOLD = threshold_override

    preflight_verify_allowlisted_types()

    config = _build_config(n_rows, faker_type, flag_on=_FLAG_ON)
    config_dict = PipelineConfig.model_validate(config).model_dump()
    # compile_plan (validate) then generate_tables (time) -- never
    # run_pipeline, which would add compile/route/pipeline work this probe
    # is not measuring (module docstring).
    plan = compile_plan(config_dict, _empty_profile(), decoy_engine_version="gp1-bench")

    fired, restore_guards = install_vacuity_guards()
    try:
        t0 = time.perf_counter()
        outputs = generate_tables(plan)
        wall_s = time.perf_counter() - t0
    finally:
        restore_guards()

    pooled_activated = fired["build_and_sample"] and fired["pool_sample"]
    if _FLAG_ON and not pooled_activated:
        raise SystemExit(
            "vacuity guard: on-arm run did not activate the faker pool "
            f"(build_and_sample fired={fired['build_and_sample']!r}, "
            f"PoolSampler.sample fired={fired['pool_sample']!r}) -- refusing to record "
            "a fabricated pooled measurement that actually ran per-row."
        )
    if not _FLAG_ON and pooled_activated:
        raise SystemExit(
            "vacuity guard: off-arm run activated the faker pool "
            f"(build_and_sample fired={fired['build_and_sample']!r}, "
            f"PoolSampler.sample fired={fired['pool_sample']!r}) -- the off arm must always "
            "be per-row."
        )

    out = outputs["t"]
    if out.num_rows != n_rows:
        raise SystemExit(f"out_rows {out.num_rows} != requested n_rows {n_rows}")
    if wall_s <= 0:
        raise SystemExit(f"wall_s must be > 0, got {wall_s!r}")

    record: dict[str, Any] = {
        "n_rows": n_rows,
        "faker_type": faker_type,
        "arm": "pooled" if _FLAG_ON else "per_row",
        "wall_s": wall_s,
        "rows_per_s": n_rows / wall_s,
        "out_rows": out.num_rows,
        "pooled_activated": pooled_activated,
        "bench_threshold_override": threshold_override,
        "workload_fingerprint": {
            "n_rows": n_rows,
            "faker_type": faker_type,
            "seed": _SEED,
            "bench_threshold_override": threshold_override,
        },
    }
    print("BENCH_JSON " + json.dumps(record))


if __name__ == "__main__":
    main()
