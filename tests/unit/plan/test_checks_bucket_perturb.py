"""Compile-time rejection of a bucket_perturb `date_format` that cannot write a date back.

Both engine validation entrypoints (`compile_plan` and `run_config_only_checks`)
must reject it up front, including inside a `nested` child config. A rejected
config previously ran and replaced every parsed date with the format text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from decoy_engine.plan import PlanCompileError, compile_plan, run_config_only_checks
from decoy_engine.profile import ColumnProfile, Profile, TableProfile

CODE = "bucket_perturb_date_format_unsupported"

_REJECTED: list[Any] = [
    "ISO8601",
    "iso8601",
    "mixed",
    "YYYY-MM-DD",
    "foo",
    "%%Y",
    "%",
    "%H:%M:%S",
    "%f",
    "%z",
    "%Z",
    "%Q",
    "%%%%Y",
    "%Y %Q",
    "%Y%",
    0,
    False,
]

_ACCEPTED: list[str | None] = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%Y",
    "%x",
    "%c",
    "100%% %Y",
    "%%%Y",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %f",
    "%Y-%m-%d %Z",
    "%%Q %Y",
    "",
    None,
]


def _config(provider_config: dict[str, Any], *, strategy: str = "bucket_perturb") -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "visits",
                "columns": [
                    {
                        "name": "seen_on",
                        "strategy": strategy,
                        "namespace": "dates",
                        "provider_config": provider_config,
                    }
                ],
            }
        ],
    }


def _nested(child_config: dict[str, Any]) -> dict:
    return _config(
        {
            "target": "$.seen",
            "strategy": "bucket_perturb",
            "strategy_config": child_config,
        },
        strategy="nested",
    )


def _compile(config: dict, profile: Profile, **kw: Any) -> Any:
    return compile_plan(config, profile, decoy_engine_version="0.1.0", **kw)


@pytest.fixture
def profile() -> Profile:
    column = ColumnProfile(
        name="seen_on",
        dtype="object",
        row_count=10,
        null_count=0,
        distinct_count=10,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="visits", row_count=10, columns=(column,)),),
        relationships=(),
        profiled_at=datetime(2026, 5, 27),
        decoy_engine_version="0.1.0",
    )


@pytest.mark.parametrize("fmt", _REJECTED, ids=[repr(f) for f in _REJECTED])
class TestRejectedFormats:
    def test_config_only_entrypoint(self, fmt: Any) -> None:
        cfg = _config({"bucket": "month", "date_format": fmt})
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == CODE
        assert "seen_on" in exc.value.message
        assert exc.value.path == "tables.visits.columns.seen_on.provider_config.date_format"

    def test_compile_entrypoint(self, fmt: Any, profile: Profile) -> None:
        cfg = _config({"bucket": "month", "date_format": fmt})
        with pytest.raises(PlanCompileError) as exc:
            _compile(cfg, profile)
        assert exc.value.code == CODE

    def test_compile_entrypoint_without_a_profile(self, fmt: Any, profile: Profile) -> None:
        cfg = _config({"bucket": "month", "date_format": fmt})
        with pytest.raises(PlanCompileError) as exc:
            _compile(cfg, profile, no_profile=True)
        assert exc.value.code == CODE


@pytest.mark.parametrize("fmt", _ACCEPTED, ids=[repr(f) for f in _ACCEPTED])
class TestAcceptedFormats:
    def test_config_only_entrypoint(self, fmt: str | None) -> None:
        cfg = _config({"bucket": "month", "date_format": fmt})
        assert "bucket_perturb_config" in run_config_only_checks(cfg)

    def test_compile_entrypoint(self, fmt: str | None, profile: Profile) -> None:
        cfg = _config({"bucket": "month", "date_format": fmt})
        plan = _compile(cfg, profile)
        assert "bucket_perturb_config" in plan.plan_compile.checks_passed


def test_absent_date_format_compiles(profile: Profile) -> None:
    plan = _compile(_config({"bucket": "month"}), profile)
    assert "bucket_perturb_config" in plan.plan_compile.checks_passed


def test_a_non_bucket_perturb_column_with_a_date_format_key_is_untouched(
    profile: Profile,
) -> None:
    cfg = _config({"date_format": "mixed"}, strategy="date_shift")
    run_config_only_checks(cfg)  # must not raise bucket_perturb_date_format_unsupported


def test_the_error_asks_for_a_concrete_pattern(profile: Profile) -> None:
    cfg = _config({"bucket": "month", "date_format": "mixed"})
    with pytest.raises(PlanCompileError) as exc:
        run_config_only_checks(cfg)
    assert "%Y-%m-%d" in exc.value.message  # asks for a concrete pattern


class TestNestedChild:
    def test_config_only_entrypoint_rejects_and_names_the_child_path(self) -> None:
        cfg = _nested({"bucket": "month", "date_format": "mixed"})
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == CODE
        assert exc.value.path == (
            "tables.visits.columns.seen_on.provider_config.strategy_config.date_format"
        )

    def test_compile_entrypoint_rejects(self, profile: Profile) -> None:
        cfg = _nested({"bucket": "month", "date_format": "mixed"})
        with pytest.raises(PlanCompileError) as exc:
            _compile(cfg, profile)
        assert exc.value.code == CODE

    def test_valid_child_compiles(self, profile: Profile) -> None:
        cfg = _nested({"bucket": "month", "date_format": "%Y-%m-%d"})
        run_config_only_checks(cfg)
        _compile(cfg, profile)

    def test_a_nested_child_of_another_strategy_is_untouched(self) -> None:
        cfg = _config(
            {"target": "$.x", "strategy": "redact", "strategy_config": {"date_format": "mixed"}},
            strategy="nested",
        )
        run_config_only_checks(cfg)
