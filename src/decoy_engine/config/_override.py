"""override_sources: job-time source-binding swap.

Per advisory axis 2 = B: `sources` is a job-time parameter. The pipeline
config ships a default binding for CLI / dev workflows; the platform
runner overrides per job. Total replacement, not merge: every logical
table in `config["tables"]` must be a key in the new `sources` mapping,
and the runner cannot leave a stale binding in place.

The replacement is validated by re-running `PipelineConfig.model_validate`
on the result, so the new sources still have to pass Pydantic strict
checks (extras forbidden, type=file format=csv|parquet, etc.).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine.config._errors import PipelineConfigError
from decoy_engine.config._pipeline import PipelineConfig


def override_sources(
    config: dict[str, Any],
    *,
    sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a new config dict with `config['sources']` replaced.

    Args:
        config: a validated config dict (output of
            `PipelineConfig.model_validate(...).model_dump()`).
        sources: the new source binding. Keys must exactly match the
            logical-table names declared in `config['tables']`. Extra
            keys or missing keys raise `PipelineConfigError`. Values
            must be valid SourceDescriptor dicts.

    Returns:
        A new dict (input is not mutated). Validated through
        `PipelineConfig.model_validate` before return.

    Raises:
        PipelineConfigError: if source-binding coverage is wrong or the
            new dict fails strict validation.
    """
    declared_tables = {t["name"] for t in config.get("tables", []) if isinstance(t, dict)}
    provided_sources = set(sources.keys())

    missing = declared_tables - provided_sources
    extra = provided_sources - declared_tables
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing source binding for tables: {sorted(missing)!r}")
        if extra:
            parts.append(f"extra source keys not in tables: {sorted(extra)!r}")
        raise PipelineConfigError(
            "override_sources: source binding does not match declared tables: " + "; ".join(parts)
        )

    new_config = dict(config)
    new_config["sources"] = {k: dict(v) for k, v in sources.items()}

    try:
        return PipelineConfig.model_validate(new_config).model_dump()
    except Exception as exc:
        raise PipelineConfigError(
            f"override_sources: replacement failed strict validation: {exc}",
            validation_error=exc,
        ) from exc
