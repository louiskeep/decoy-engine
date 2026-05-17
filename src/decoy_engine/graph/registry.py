"""Op-engine registry.

Maps an op `kind` to the engine it wants to run on. The runner reads this
to materialize the cached `pyarrow.Table` into the right type before
calling `apply()`.

Ops declare their preferred substrate via a module-level `NATIVE_ENGINE`
constant. File and cloud source/target ops use "duckdb"; transform ops
(mask, generate, sort, dedupe, derive, filter, unite) use "pandas" because
their strategies rely on per-row Python callbacks. Ops without a declaration
fall back to "pandas".

Engine-mode override: when a graph's top-level `engine:` key is `"pandas"`
(the opt-out safety hatch), every op is forced to pandas regardless of its
declared NATIVE_ENGINE. `engine: "hybrid"` (the runtime default) respects
each op's own declaration. The override is applied in the runner via
`native_engine_for` accepting a graph_engine_mode argument.
"""

from typing import Literal

from decoy_engine.graph.conversion import VALID_ENGINES, EngineType

GraphEngineMode = Literal["pandas", "hybrid"]


def native_engine_for(kind: str, graph_engine_mode: GraphEngineMode = "pandas") -> EngineType:
    """Return the engine the op of this kind wants to run on.

    `graph_engine_mode` mirrors the graph YAML's top-level `engine:` key.
    - "hybrid" (runtime default via _resolve_engine_mode): respect each
      op's NATIVE_ENGINE declaration; ops without a declaration default
      to pandas.
    - "pandas" (opt-out safety hatch): every op runs on pandas, ignoring
      NATIVE_ENGINE declarations.

    The parameter default is "pandas" for backwards compatibility with
    direct callers. The runner always passes the resolved mode, which
    defaults to "hybrid".
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
        # Misdeclared op falls back to pandas rather than raising; the
        # validator could check this in a follow-up.
        return "pandas"
    return declared  # type: ignore[return-value]
