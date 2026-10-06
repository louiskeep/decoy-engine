"""Exception-taxonomy sentry (R0 item 1).

``DecoyError`` documents itself as the base class of every decoy_engine
exception. Before R0, 40-odd engine-defined families inherited ``Exception``
(or a stdlib error) directly, so ``except DecoyError`` silently missed most
runtime failures. This file pins the contract two ways: an explicit table of
the family roots, and a walk over every engine module that fails when a NEW
engine-defined exception class lands outside ``DecoyError`` without an
allowlist entry.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import decoy_engine
from decoy_engine.errors import DecoyError

# (module, class, stdlib ancestor that must be kept, or None)
_REPARENTED_ROOTS: tuple[tuple[str, str, type | None], ...] = (
    ("decoy_engine.errors", "ValidationError", None),
    ("decoy_engine.vault", "VaultError", None),
    ("decoy_engine.keyprovider", "MaskSecretError", None),
    ("decoy_engine.keyprovider", "KeyedStrategyRequiresSecret", None),
    ("decoy_engine.keyprovider", "MissingMaskSecret", None),
    ("decoy_engine.keyprovider", "WeakMaskSecret", None),
    ("decoy_engine.execution._errors", "ExecutionError", None),
    ("decoy_engine.execution._errors", "StrategyError", None),
    ("decoy_engine.execution._transforms", "TransformError", None),
    ("decoy_engine.execution._isolated_commit", "CommitError", None),
    ("decoy_engine.plan._errors", "PlanCompileError", None),
    ("decoy_engine.relationships._namespace", "NamespaceConfigError", None),
    ("decoy_engine.determinism._derive", "DeterminismError", None),
    ("decoy_engine.generation.pool._errors", "GenerationError", None),
    ("decoy_engine.generation.pool._errors", "PoolCapacityError", None),
    ("decoy_engine.generation.composite._errors", "CompositeError", None),
    ("decoy_engine.generation.statistical._spec", "StatisticalSpecError", None),
    ("decoy_engine.providers_v2._errors", "ProviderError", None),
    ("decoy_engine.providers_v2._errors", "AdapterError", None),
    ("decoy_engine.providers_v2.identifiers._errors", "IdentifierError", None),
    ("decoy_engine.providers_v2.identifiers._errors", "IdentifierFormatError", None),
    ("decoy_engine.quality.dp", "DpError", None),
    ("decoy_engine.quality.dp_budget", "DpBudgetError", None),
    ("decoy_engine.quality.snapshot", "DistributionSnapshotError", None),
    ("decoy_engine.quality.dp_provenance", "ProvenanceError", None),
    ("decoy_engine.quality.carriers", "CarrierError", None),
    ("decoy_engine.storm.ner", "NerUnavailableError", None),
    ("decoy_engine.storm.name_hints.loader", "NameHintLoaderError", None),
    ("decoy_engine.storm.model_pack.loader", "ModelPackLoadError", ValueError),
    ("decoy_engine.config._errors", "PipelineConfigError", ValueError),
    ("decoy_engine.transforms._ff1", "Ff1Error", ValueError),
    ("decoy_engine.execution.native._draw_site_providers", "DrawSiteProtocolError", RuntimeError),
)

# Lane-internal control-flow carriers that never reach a caller, so the
# DecoyError contract does not apply to them.
_ALLOWLIST: dict[tuple[str, str], str] = {
    ("decoy_engine.execution.physical._shadow_diff_codes", "ShadowDifference"): (
        "shadow-lane internal signal, caught inside the lane"
    ),
    ("decoy_engine.execution.physical._shadow_diff_codes", "PoolBuildFailed"): (
        "shadow-lane internal carrier, caught inside the lane"
    ),
    ("decoy_engine.quality.dp_budget", "_InfeasibleAtEpsQError"): (
        "private search-loop signal inside dp_budget"
    ),
}


def _load(module: str, name: str) -> type:
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:  # optional extra not installed
        pytest.skip(f"{module} unavailable: {exc}")
    return getattr(mod, name)


@pytest.mark.parametrize(("module", "name", "stdlib"), _REPARENTED_ROOTS)
def test_reparented_root_is_decoy_error(module: str, name: str, stdlib: type | None) -> None:
    cls = _load(module, name)
    assert issubclass(cls, DecoyError)
    if stdlib is not None:
        assert issubclass(cls, stdlib)


def test_pool_capacity_error_is_independent_of_generation_error() -> None:
    from decoy_engine.generation.pool._errors import GenerationError, PoolCapacityError

    assert not issubclass(PoolCapacityError, GenerationError)
    assert issubclass(PoolCapacityError, DecoyError)
    assert issubclass(GenerationError, DecoyError)


def test_namespace_config_error_still_a_plan_compile_error() -> None:
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.relationships._namespace import NamespaceConfigError

    assert issubclass(NamespaceConfigError, PlanCompileError)
    assert issubclass(NamespaceConfigError, DecoyError)


def test_native_chunk_schema_drift_keeps_both_parents() -> None:
    from decoy_engine.execution._errors import ExecutionError
    from decoy_engine.execution.native._chunk_schema import NativeChunkSchemaDriftError

    assert issubclass(NativeChunkSchemaDriftError, ExecutionError)
    assert issubclass(NativeChunkSchemaDriftError, DecoyError)


def test_allowlisted_carriers_stay_outside_decoy_error() -> None:
    for (module, name), _reason in _ALLOWLIST.items():
        assert not issubclass(_load(module, name), DecoyError), (module, name)


def _walk_engine_exceptions() -> tuple[list[tuple[str, str]], list[str]]:
    """Return (offenders, skipped modules) for engine-defined exceptions."""
    offenders: list[tuple[str, str]] = []
    skipped: list[str] = []
    for info in pkgutil.walk_packages(decoy_engine.__path__, prefix="decoy_engine."):
        try:
            mod = importlib.import_module(info.name)
        except ImportError:
            skipped.append(info.name)
            continue
        for name, obj in vars(mod).items():
            if not (isinstance(obj, type) and issubclass(obj, BaseException)):
                continue
            if obj.__module__ != mod.__name__:
                continue  # re-export or stdlib/third-party class
            if issubclass(obj, DecoyError):
                continue
            if (mod.__name__, name) in _ALLOWLIST:
                continue
            offenders.append((mod.__name__, name))
    return offenders, skipped


def test_every_engine_exception_inherits_decoy_error() -> None:
    offenders, skipped = _walk_engine_exceptions()
    # Skipped modules (optional extras not installed here) are reported so a
    # reviewer can see what the walk did not cover.
    print("exception sentry skipped modules:", sorted(skipped))
    assert not offenders, f"engine exceptions outside DecoyError: {offenders}"
