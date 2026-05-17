"""Op-engine registry.

Maps an op `kind` to the engine it wants to run on. The runner reads this
to materialize the cached `pyarrow.Table` into the right type before
calling `apply()`.

Defaults to "pandas" for any op that hasn't declared `NATIVE_ENGINE`,
preserving today's behavior. Phases 3 + 4 of the polars-duckdb hybrid plan
flip individual ops to "polars" / "duckdb" by adding the declaration to
the op module.

Engine-mode override: when a graph's top-level `engine:` key is `"pandas"`
(the safety-hatch / opt-out value), every op is forced to pandas regardless
of its declared NATIVE_ENGINE. `engine: "hybrid"` (and the future default)
respects each op's declaration. The override is applied in the runner via
`native_engine_for` accepting a graph_engine_mode argument.
"""

from typing import Literal

from decoy_engine.graph.conversion import VALID_ENGINES, EngineType

GraphEngineMode = Literal["pandas", "hybrid"]


def native_engine_for(kind: str, graph_engine_mode: GraphEngineMode = "pandas") -> EngineType:
    """Return the engine the op of this kind wants to run on.

    `graph_engine_mode` mirrors the graph YAML's top-level `engine:` key.
    - "pandas" (today's default): every op runs on pandas, ignoring
      NATIVE_ENGINE declarations. Existing pipelines are unaffected by
      future op ports.
    - "hybrid": respect each op's NATIVE_ENGINE declaration; ops without
      a declaration still default to pandas.
    """
    if graph_engine_mode == "pandas":
        return "pandas"

    from decoy_engine.graph.ops import OPS

    op = OPS.get(kind)
    if op is None:
        # Unknown kind would have failed validation already; defensive fallback.
        return "pandas"
    declared = getattr(op, "NATIVE_ENGINE", "pandas")
    if declared not in VALID_ENGINES:
        # GraphConfigValidator._validate_nodes catches invalid NATIVE_ENGINE
        # as a validation error before execution reaches this point.
        # This branch is a defensive fallback for callers that skip validation.
        return "pandas"
    return declared  # type: ignore[return-value]
