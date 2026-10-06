"""C8-i acceptance test 7: the public `ColumnConfig.when` field.

The platform validates a config with `PipelineConfig.model_validate` and catches only
pydantic's `ValidationError` (`api/pipelines/v2_validation.py`), so a grammar refusal must
surface there, with the error type `when_outside_closed_grammar` and its location, and not
as the engine's own `ValidationError` (which is not a `ValueError` and would escape).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from pydantic import ValidationError as PydanticValidationError

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ValidationError as EngineValidationError
from decoy_engine.plan import compile_plan
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from tests.unit.test_c8_i_when_grammar import ACCEPTED, REJECTED

ENGINE_VERSION = "c8-i-config"


def _raw(when: Any = "s == 'a'", *, strategy: str = "redact") -> dict[str, Any]:
    column: dict[str, Any] = {"name": "s", "strategy": strategy}
    if when is not ...:
        column["when"] = when
    return {
        "version": 1,
        "global_settings": {"seed": 7, "post_validation": False},
        "sources": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
        "tables": [{"name": "t", "columns": [column, {"name": "n", "strategy": "passthrough"}]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "/dev/null"}},
    }


@pytest.mark.parametrize("expr", ACCEPTED)
def test_a_valid_predicate_validates_and_is_stored_stripped(expr: str) -> None:
    config = PipelineConfig.model_validate(_raw(expr))
    assert config.tables[0].columns[0].when == expr.strip()


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_predicate_becomes_none(blank: str) -> None:
    config = PipelineConfig.model_validate(_raw(blank))
    assert config.tables[0].columns[0].when is None


def test_the_field_defaults_to_none_and_other_columns_are_unaffected() -> None:
    config = PipelineConfig.model_validate(_raw(...))
    assert [c.when for c in config.tables[0].columns] == [None, None]


@pytest.mark.parametrize("expr", REJECTED)
def test_every_grammar_refusal_is_a_pydantic_error_at_the_when_location(expr: str) -> None:
    if not expr.strip():
        pytest.skip("a blank predicate is normalized to None, not refused")
    with pytest.raises(PydanticValidationError) as info:
        PipelineConfig.model_validate(_raw(expr))
    errors = [e for e in info.value.errors() if e["loc"][-1] == "when"]
    assert len(errors) == 1, info.value.errors()
    assert errors[0]["type"] == "when_outside_closed_grammar"
    assert errors[0]["loc"] == ("tables", 0, "columns", 0, "when")


def test_the_platform_catch_pattern_sees_the_refusal() -> None:
    """Reproduce `v2_validation`'s handler: catch pydantic's ValidationError only."""
    caught: list[dict[str, Any]] = []
    try:
        PipelineConfig.model_validate(_raw("s.notnull()"))
    except PydanticValidationError as exc:
        for err in exc.errors():
            caught.append({"path": ".".join(str(p) for p in err["loc"]), "hint": err["type"]})
    except EngineValidationError:  # pragma: no cover - the failure this test guards
        pytest.fail("the engine's ValidationError escaped pydantic")
    assert caught == [{"path": "tables.0.columns.0.when", "hint": "when_outside_closed_grammar"}]


def test_a_non_string_predicate_is_a_pydantic_error_too() -> None:
    with pytest.raises(PydanticValidationError):
        PipelineConfig.model_validate(_raw(5))


def test_the_model_dump_carries_the_field_and_round_trips() -> None:
    config = PipelineConfig.model_validate(_raw("s == 'a' and n > 1"))
    dumped = config.model_dump()
    assert dumped["tables"][0]["columns"][0]["when"] == "s == 'a' and n > 1"
    assert PipelineConfig.model_validate(dumped).model_dump() == dumped


def test_the_manifest_round_trips_the_predicate() -> None:
    from decoy_engine.execution._chunked_profile import first_chunk_profile

    config = PipelineConfig.model_validate(_raw("s != 'a'")).model_dump()
    first = pa.table({"s": ["a", "b"], "n": [1, 2]})
    profile = first_chunk_profile(first, table="t", engine_version=ENGINE_VERSION)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION, no_profile=True)
    seeds = dict(dict(plan.seed_envelope.per_table)["t"].per_column)
    assert seeds["s"].when == "s != 'a'" and seeds["n"].when is None
    again = plan_from_yaml(plan_to_yaml(plan))
    seeds_again = dict(dict(again.seed_envelope.per_table)["t"].per_column)
    assert seeds_again["s"].when == "s != 'a'" and seeds_again["n"].when is None


def test_a_validated_config_with_when_runs_end_to_end_on_the_chunked_native_route(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution import run_pipeline
    from decoy_engine.keyprovider import SecretKeyProvider

    n = 12
    source = pa.table(
        {
            "s": pa.array([f"name-{i}" for i in range(n)]),
            "n": pa.array(range(n), pa.int64()),
        }
    )
    import pyarrow.parquet as pq

    raw = _raw("s in ['name-1', 'name-4', 'name-7'] or s == 'name-10'")
    path = str(tmp_path / "source.parquet")
    pq.write_table(source, path)
    raw["sources"]["t"] = {"type": "file", "format": "parquet", "path": path}
    config = PipelineConfig.model_validate(raw).model_dump()
    kwargs: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "key_provider": SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
    }
    auto = run_pipeline(
        config,
        {"t": source},
        auto_chunk_threshold_rows=3,
        chunk_size_rows=5,
        **kwargs,
    )
    full = run_pipeline(config, {"t": source}, auto_chunk=False, **kwargs)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    route = auto.quality_metrics["chunked_route"]
    assert route["native_admitted"] is True, route
    got = auto.outputs["t"].column("s").to_pylist()
    assert got == full.outputs["t"].column("s").to_pylist()
    masked = [i for i, v in enumerate(got) if v != f"name-{i}"]
    assert masked == [1, 4, 7, 10]


# Pinned on engine main before `when` existed: a config without `when` must keep hashing
# byte-identically, so adding the field changes no fingerprint for existing jobs.
_PRE_WHEN_PIPELINE_HASH = "9af1bc73bf23b67df77fe643f60028ac19407f7f18f67ea46ab5f374e9390cd1"
_PRE_WHEN_CANONICAL_SHA = "79edebace4df538a6e9cc1bf0749a0f713b40d7bb5ac3e3fcdead052f43290c1"


def _hash_fixture(**column_extra: object) -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {"t": {"type": "file", "format": "csv", "path": "/tmp/x.csv"}},
        "targets": {"t": {"type": "file", "format": "csv", "path": "/tmp/y.csv"}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "a", "strategy": "hash", **column_extra},
                    {"name": "b", "strategy": "redact"},
                ],
            }
        ],
    }


def _hashes(raw: dict) -> tuple[str, str]:
    import hashlib

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution.physical._inputs import _canonical_config_json
    from decoy_engine.plan._compile import _hash_config

    dumped = PipelineConfig.model_validate(raw).model_dump()
    canonical = hashlib.sha256(_canonical_config_json(dumped).encode()).hexdigest()
    return _hash_config(dumped), canonical


def test_a_config_without_when_hashes_exactly_as_before_the_field_existed() -> None:
    assert _hashes(_hash_fixture()) == (_PRE_WHEN_PIPELINE_HASH, _PRE_WHEN_CANONICAL_SHA)


def test_an_unset_when_is_omitted_from_the_dump() -> None:
    from decoy_engine.config import PipelineConfig

    dumped = PipelineConfig.model_validate(_hash_fixture()).model_dump()
    assert all("when" not in column for column in dumped["tables"][0]["columns"])


def test_a_set_when_moves_both_hashes() -> None:
    pipeline, canonical = _hashes(_hash_fixture(when="b == 'x'"))
    assert pipeline != _PRE_WHEN_PIPELINE_HASH
    assert canonical != _PRE_WHEN_CANONICAL_SHA
