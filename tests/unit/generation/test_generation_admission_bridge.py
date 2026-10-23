"""Admission bridge from a `GenerationPlan` and the digest-covered `reference_date` field."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine.config._global_settings import GlobalSettings
from decoy_engine.generation._pinned_compile import compile_pinned_plan
from decoy_engine.generation._pinned_plan import PinnedModeError
from decoy_engine.generation._window_admission import GenerationAdmissionError
from decoy_engine.generation.window import classify_window, classify_window_from_generation_plan
from decoy_engine.plan import compile_plan
from decoy_engine.plan._compile import _NON_SEMANTIC_GLOBAL_SETTINGS, _hash_config
from tests.unit._dps_helpers import empty_profile

REF = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
REF_TEXT = "2026-10-22T09:30:00Z"
# (`_hash_config`, plan_hash canonical config sha) for GlobalSettings(seed=7).model_dump() with no
# tables, computed before the field existed. Pins that an unset reference_date migrates neither.
PRE_FIELD_DIGESTS = (
    "bfd5f0f784dcbae9b7c29179023f61ab1447650cb686476702148ecb31a966b6",
    "0ce8c947061883d55d66d6bceb8df24653013c49edc37c2f4fbd47b2a220c053",
)


def _config(*tables: dict[str, Any]) -> dict[str, Any]:
    return {"global_settings": {"seed": 5}, "tables": list(tables)}


def _table(name: str, cols: list[dict[str, Any]], rows: int = 10) -> dict[str, Any]:
    return {"name": name, "row_count": rows, "generate_columns": cols}


def _outcome(fn: Any) -> Any:
    try:
        return ("ok", fn())
    except GenerationAdmissionError as exc:
        return ("refused", exc.code, exc.column, exc.prop)


def _both(config: dict[str, Any], table: str, **kw: Any) -> tuple[Any, Any]:
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    full = _outcome(lambda: classify_window(plan, table, **kw))
    bridge = _outcome(lambda: classify_window_from_generation_plan(plan.generation, table, **kw))
    return full, bridge


# 1. parity ---------------------------------------------------------------------------------


def test_parity_class_a_and_class_b() -> None:
    cfg = _config(
        _table("a", [{"name": "id", "type": "sequence", "start": 1}]),
        _table("b", [{"name": "c", "type": "categorical", "categories": ["x", "y"]}]),
    )
    a_full, a_bridge = _both(cfg, "a")
    b_full, b_bridge = _both(cfg, "b")
    assert a_full == a_bridge and a_full[1].klass == "A"
    assert b_full == b_bridge and b_full[1].klass == "B"
    nullable = _config(
        _table("n", [{"name": "id", "type": "sequence", "start": 1, "null_probability": 0.2}])
    )
    full, bridge = _both(nullable, "n")
    assert full == bridge and full[1].class_b_reasons == (("id", "null_injection"),)


def test_parity_instance_default_locale_changes_verdict() -> None:
    cfg = _config(_table("t", [{"name": "u", "type": "faker", "faker_type": "bs"}], rows=60_000))
    default_full, default_bridge = _both(cfg, "t")
    other_full, other_bridge = _both(cfg, "t", instance_default_locale="ja_JP")
    assert default_full == default_bridge and other_full == other_bridge
    assert default_full[1].klass == "B"  # pooled under the default locale
    assert other_full[1].klass == "A"  # uncertified locale: per-row, not pooled


def test_parity_provider_snapshot_changes_verdict() -> None:
    cfg = _config(_table("t", [{"name": "w", "type": "faker", "faker_type": "word"}]))
    plain_full, plain_bridge = _both(cfg, "t")
    snap_full, snap_bridge = _both(cfg, "t", provider_snapshot={"word": lambda f: "x"})
    assert plain_full == plain_bridge and plain_full[1].klass == "A"
    assert snap_full == snap_bridge
    assert snap_full[:2] == ("refused", "generation_not_reproducible")


def test_parity_window_end_and_max_cell_chars_change_verdict() -> None:
    cfg = _config(
        _table(
            "t", [{"name": "s", "type": "sequence", "start": 1, "step": 10**30, "pad_length": 1}]
        )
    )
    near_full, near_bridge = _both(cfg, "t", max_cell_chars=33, window_end=2)
    far_full, far_bridge = _both(cfg, "t", max_cell_chars=33, window_end=10**6)
    assert near_full == near_bridge and near_full[1].klass == "A"
    assert far_full == far_bridge and far_full[:2] == ("refused", "generation_unbounded_column")
    tight_full, tight_bridge = _both(cfg, "t", max_cell_chars=5, window_end=2)
    assert tight_full == tight_bridge and tight_full[0] == "refused"


# 2. type guard -----------------------------------------------------------------------------


def test_bridge_accepts_only_a_generation_plan() -> None:
    cfg = _config(_table("t", [{"name": "id", "type": "sequence", "start": 1}]))
    plan = compile_plan(cfg, empty_profile(), decoy_engine_version="test")
    pinned = compile_pinned_plan(cfg, REF)
    for bad in (cfg, plan, pinned):
        with pytest.raises(TypeError):
            classify_window_from_generation_plan(bad, "t")


# 3. no statistical decode ------------------------------------------------------------------


def test_non_statistical_admission_builds_no_statistical_specs() -> None:
    cfg = _config(_table("t", [{"name": "id", "type": "sequence", "start": 1}]))
    gen = compile_plan(cfg, empty_profile(), decoy_engine_version="test").generation
    assert gen is not None and not gen.statistical_specs
    assert classify_window_from_generation_plan(gen, "t").klass == "A"


# 4. reference_date validation --------------------------------------------------------------


def test_reference_date_normalizes_aware_inputs() -> None:
    assert "reference_date" not in GlobalSettings(seed=1).model_dump()
    for value in (
        REF,
        REF_TEXT,
        "2026-10-22T11:30:00+02:00",
        REF.astimezone(timezone(timedelta(hours=-5))),
    ):
        dumped = GlobalSettings(seed=1, reference_date=value).model_dump()
        assert dumped["reference_date"] == REF_TEXT
        assert isinstance(dumped["reference_date"], str)


@pytest.mark.parametrize(
    "bad", [datetime(2026, 10, 22, 9, 30), "2026-10-22T09:30:00", "not a date", 5]
)
def test_reference_date_refuses_naive_or_invalid_as_a_field_error(bad: Any) -> None:
    with pytest.raises(ValidationError) as exc:
        GlobalSettings(seed=1, reference_date=bad)
    assert [e["loc"] for e in exc.value.errors()] == [("reference_date",)]
    assert not isinstance(exc.value, PinnedModeError)
    assert "2026" not in str(exc.value.errors()[0]["msg"])


# 5. digest coverage ------------------------------------------------------------------------


def _digests(**settings: Any) -> tuple[str, str]:
    """(pipeline_config_hash input, plan_hash canonical config) for a one-table config."""
    import hashlib

    from decoy_engine.execution.physical._inputs import _canonical_config_json

    config = {"global_settings": GlobalSettings(seed=7, **settings).model_dump(), "tables": []}
    canonical = hashlib.sha256(_canonical_config_json(config).encode()).hexdigest()
    return _hash_config(config), canonical


def test_supplied_reference_date_is_digest_covered_and_json_native() -> None:
    assert "reference_date" not in _NON_SEMANTIC_GLOBAL_SETTINGS
    a = _digests(reference_date=REF)
    b = _digests(reference_date=REF + timedelta(days=1))
    base = _digests()
    for i in (0, 1):
        assert a[i] != b[i] and a[i] != base[i]
    assert _digests(reference_date=REF_TEXT) == a


def test_unset_reference_date_keeps_both_pre_field_digests() -> None:
    assert _digests() == PRE_FIELD_DIGESTS
    assert _digests(reference_date=None) == PRE_FIELD_DIGESTS
    assert "reference_date" not in GlobalSettings(seed=7).model_dump()


def test_none_keys_stay_hashed() -> None:
    gs = GlobalSettings(seed=7).model_dump()
    assert gs["unconfigured_column_policy"] is None and gs["mask_secret_ref"] is None
    base = {"global_settings": gs, "tables": []}
    for key in ("unconfigured_column_policy", "mask_secret_ref"):
        stripped = {k: v for k, v in gs.items() if k != key}
        assert _hash_config({**base, "global_settings": stripped}) != _hash_config(base)


# 6. compile_pinned_plan --------------------------------------------------------------------


def _pinned_cfg(reference_date: Any = None) -> dict[str, Any]:
    cfg = _config(_table("t", [{"name": "id", "type": "sequence", "start": 1}]))
    if reference_date is not None:
        cfg["global_settings"] = GlobalSettings(seed=5, reference_date=reference_date).model_dump()
    return cfg


def test_pinned_plan_reads_the_config_field_when_no_positional() -> None:
    plan = compile_pinned_plan(_pinned_cfg(REF))
    assert plan.reference_date == REF_TEXT


def test_positional_reference_date_wins_over_the_field() -> None:
    later = REF + timedelta(days=3)
    assert compile_pinned_plan(_pinned_cfg(REF), later).reference_date == "2026-10-25T09:30:00Z"


def test_missing_reference_date_is_refused() -> None:
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(_pinned_cfg())
    assert exc.value.code == "pinned_reference_date_invalid"


# 7. import boundary ------------------------------------------------------------------------


def test_config_validates_reference_date_in_a_fresh_interpreter() -> None:
    import subprocess
    import sys

    code = (
        "import decoy_engine.config._global_settings as g; "
        "g.GlobalSettings(seed=1, reference_date='2026-10-22T09:30:00Z')"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr


def test_config_does_not_import_generation_and_error_identity_is_shared() -> None:
    import inspect

    import decoy_engine._reference_date as shared
    import decoy_engine.config._global_settings as settings

    assert "decoy_engine.generation" not in inspect.getsource(settings)
    assert PinnedModeError is shared.PinnedModeError
