"""run_storm — scan upstream rows and capture a StormProfile.

This op wraps `decoy_engine.storm.run_storm` so a graph pipeline can refresh
its STORM profile mid-DAG. The dataframe is passed through unchanged; the
profile is captured to `ctx.captured_outputs` for the platform runner to
persist as a StormScan row, after which `${storm.X}` interpolation in
*subsequent* runs of the pipeline picks up the refreshed values.

Config:
    source_label: str          - label for the persisted scan; usually the
                                 pipeline name (the platform fills this in
                                 when the YAML omits it). When set,
                                 ${storm.X} resolves against scans with
                                 this label, matched newest-first.
    sample_strategy: str       - "full" | "head" | "random" | "stratified"
                                 (default "full"; passed straight through)
    sample_row_cap: int        - cap for non-full strategies (optional)
    parent_source_label: str   - optional; declares this scan as derived
                                 from another scan. When set, the platform
                                 runner links the persisted StormScan to
                                 the most recent same-owner scan with this
                                 label via `source_scan_id`. Enables the
                                 source-vs-masked diff pattern: the masked
                                 output is scanned with the source's label
                                 as its parent, and the diff endpoint
                                 compares the two without needing to know
                                 about masking semantics.

Why pass-through, not a sink:
    Downstream nodes still need the dataframe — typical use is
    source → run_storm → mask → target where mask reads ${storm.X} via
    pre-engine variable resolution and the row stream continues unchanged.
    Returning the input dataframe lets users wire run_storm into any
    point of the DAG without breaking flow.
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError


def _attr_or_key(obj: Any, name: str) -> Any:
    """Lookup `name` on `obj` whether it's a dataclass-style object or a dict.

    StormProfile fields normalize to dataclass shape, but tests sometimes
    construct profile dicts directly; handle both."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


KIND = "run_storm"
# Phase 1 Arrow-boundary benchmark on a HIPAA-shaped fixture: ~2.4% overhead.
# Stay on pandas — within the 10% threshold for declaring "arrow".
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (1, 1)
OUTPUT_KIND = "stream"


def validate_config(config: dict[str, Any]) -> None:
    src = config.get("source_label")
    if src is not None and (not isinstance(src, str) or not src.strip()):
        raise ValidationError(
            "'source_label' must be a non-empty string when set",
            "config.source_label",
        )

    strat = config.get("sample_strategy", "full")
    if not isinstance(strat, str) or strat not in (
        "full", "head", "random", "stratified"
    ):
        raise ValidationError(
            "'sample_strategy' must be one of full / head / random / stratified",
            "config.sample_strategy",
        )

    cap = config.get("sample_row_cap")
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0):
        raise ValidationError(
            "'sample_row_cap' must be a positive integer when set",
            "config.sample_row_cap",
        )

    parent = config.get("parent_source_label")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ValidationError(
            "'parent_source_label' must be a non-empty string when set",
            "config.parent_source_label",
        )


def apply(inputs, config, ctx) -> pd.DataFrame:
    df = inputs[0]
    source_label = config.get("source_label") or "graph_run"
    sample_strategy = config.get("sample_strategy", "full")
    sample_row_cap = config.get("sample_row_cap")

    # Local import to avoid a top-level cycle: the storm package imports
    # from internals that may import from graph.ops in some test paths.
    from decoy_engine.storm import run_storm as _run_storm

    try:
        profile = _run_storm(
            df,
            source_label,
            sample_strategy=sample_strategy,
            sample_row_cap=sample_row_cap,
        )
    except Exception as exc:
        raise OpError(f"run_storm failed: {exc}") from exc

    if ctx is not None and getattr(ctx, "captured_outputs", None) is not None:
        entry: dict[str, Any] = {
            "kind": "storm_profile",
            "source_label": source_label,
            "profile": profile.to_dict(),
        }
        parent = config.get("parent_source_label")
        if parent:
            # Hint for the platform runner: link the persisted scan to the
            # most recent same-owner scan with this label. Engine is
            # platform-agnostic and never resolves the link itself.
            entry["parent_source_label"] = parent
        ctx.captured_outputs.append(entry)
        if ctx.logger is not None:
            ctx.logger.info(
                "run_storm: captured profile for source_label=%r (rows=%d)",
                source_label,
                profile.row_count,
            )

    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("rows_scanned", int(profile.row_count))
        entities: dict[str, int] = {}
        k_anon_violations = 0
        for field in getattr(profile, "fields", []) or []:
            for match in getattr(field, "detector_matches", []) or []:
                kind = _attr_or_key(match, "kind")
                if kind:
                    entities[kind] = entities.get(kind, 0) + 1
            for sentinel in getattr(field, "sentinels", []) or []:
                if _attr_or_key(sentinel, "kind") == "k_anonymity":
                    k_anon_violations += 1
        ctx.export("entities_detected", entities)
        ctx.export("reid_risk_score", float(getattr(profile, "reid_risk_score", 0.0) or 0.0))
        ctx.export("k_anonymity_violations", k_anon_violations)
        # Stable hash of the serialized profile body; downstream audit nodes
        # can pin to a specific scan output.
        import hashlib as _hashlib
        import json as _json
        profile_dict = profile.to_dict()
        body = _json.dumps(profile_dict, sort_keys=True, default=str).encode("utf-8")
        ctx.export("profile_hash", _hashlib.sha256(body).hexdigest())

    return df
