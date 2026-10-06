"""Guard for the forced-oracle test stand-in (R1 E7).

`tests/native/_chunked_entry_support.py::force_oracle` builds a deterministic categorical
with NUMERIC categories so a test table is never native-admitted and runs on the oracle.
If a later slice makes that config native-admissible, every forced-oracle comparison
silently becomes native-vs-native. This test fails first, and says how to migrate.
It calls `_static_route_decision`, which does no compiled-extension probe, so a missing
companion cannot make it pass for the wrong reason.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native._dispatch import _static_route_decision
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    FORCE_ORACLE_VALUE,
    TABLE,
    force_oracle,
    forced_reason,
)

_MIGRATE = (
    "The forced-oracle stand-in (numeric-category categorical, force_oracle in "
    "tests/native/_chunked_entry_support.py) is now native-admissible, so tests using it no "
    "longer exercise the oracle. Migrate force_oracle to a config that is still never "
    "native-admitted (pick another python_only column shape), update forced_reason and every "
    "docstring naming the stand-in, and re-run the chunked entry suites before relaxing this guard."
)


def _decide(col: dict[str, Any]) -> Any:
    config = {"tables": [{"name": TABLE, "columns": [col]}]}
    source = pa.table({"c": pa.array([FORCE_ORACLE_VALUE] * 3, pa.string())})
    profile = first_chunk_profile(source, table=TABLE, engine_version=ENGINE_VERSION)
    return _static_route_decision(
        config,
        profile,
        table=TABLE,
        engine_version=ENGINE_VERSION,
        registry=get_default_registry(),
    )


def test_numeric_category_standin_declines_with_the_exact_reason() -> None:
    decision = _decide(force_oracle("c"))
    assert decision.native_admitted is False, _MIGRATE
    assert decision.reroute_reason == forced_reason("c"), _MIGRATE
    assert forced_reason("c") == "fallback_policy_not_native:c:python_only"


def test_string_category_twin_is_admitted() -> None:
    twin = force_oracle("c")
    twin["provider_config"] = {"categories": ["a", "b", "c"]}
    decision = _decide(twin)
    assert decision.native_admitted is True, decision.reroute_reason
