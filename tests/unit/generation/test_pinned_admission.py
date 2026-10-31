"""GEN-API 1c-E: the pinned-admission entries generate from a bare `GenerationPlan` with the same
bytes as the full-`Plan` entries, and the `_prepare` split keeps the full-Plan contract intact.

Plan: docs/plans/2026-10-23-genapi-1c-e-pinned-admission.md (acceptance tests 1-8).
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from decoy_engine.errors import ValidationError
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.generation import _plan_entry as plan_entry
from decoy_engine.generation._plan_entry import generate_tables_streamed
from decoy_engine.generation.synthesize import (
    generate_tables,
    generate_tables_from_generation_plan,
    generate_tables_streamed_from_generation_plan,
)
from decoy_engine.plan._types import GenerationPlan
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import fingerprints, plan_of

BAD_WHEN = "s.notnull()"


def _plan_with_bad_seed_when(plan: Any) -> Any:
    seed = SimpleNamespace(when=BAD_WHEN)
    env = SimpleNamespace(per_table=(("t", SimpleNamespace(per_column=(("c", seed),))),))
    return dataclasses.replace(plan, seed_envelope=env)


def _streamable() -> dict[str, Any]:
    return fx.cfg_sequence()


def _legacy() -> dict[str, Any]:
    return fx.cfg_statistical()


def _dp_generation(*, statistical: bool, receipt: bool) -> GenerationPlan:
    """Built by hand: the guard under test reads only `config_json` and `dp_verification`."""
    import json

    columns: list[dict[str, Any]] = [{"name": "k", "type": "sequence", "start": 1}]
    if statistical:
        columns.append({"name": "s", "type": "statistical", "snapshot_file": "unused.json"})
    config = {
        "version": 2,
        "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
        "sources": {},
        "tables": [{"name": "t", "row_count": 3, "generate_columns": columns}],
        "targets": {},
    }
    return GenerationPlan(config_json=json.dumps(config))


# ---- 1. byte-identity, resident and legacy ---------------------------------------------------


@pytest.mark.parametrize("cfg_fn", [_streamable, _legacy], ids=["streamed-eligible", "legacy"])
def test_pinned_entry_is_byte_identical_to_the_full_plan_entry(cfg_fn: Any) -> None:
    plan = plan_of(cfg_fn())
    key = fx.pinned_derive_key
    full = generate_tables(plan, derive_key=key, instance_default_locale="en_US")
    pinned = generate_tables_from_generation_plan(
        plan.generation, derive_key=key, instance_default_locale="en_US"
    )
    assert fingerprints(pinned) == fingerprints(full)
    for name, table in full.items():
        assert pinned[name].schema.equals(table.schema)
        assert pinned[name].equals(table)  # values and row order


def test_pinned_entry_honours_provider_snapshot() -> None:
    from decoy_engine.internal.faker_setup import snapshot_custom_faker_providers

    plan = plan_of(fx.cfg_live_faker())
    snap = snapshot_custom_faker_providers()
    full = generate_tables(plan, provider_snapshot=snap)
    pinned = generate_tables_from_generation_plan(plan.generation, provider_snapshot=snap)
    assert fingerprints(pinned) == fingerprints(full)


# ---- 2. byte-identity, streamed --------------------------------------------------------------


def test_streamed_pinned_entry_matches_streamed_full_plan_entry(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    plan = plan_of(_streamable())
    kwargs: dict[str, Any] = {"budget_bytes": 256 * 1024 * 1024, "batch_rows": 2}
    full_dir, pinned_dir = tmp_path / "full", tmp_path / "pinned"
    full = generate_tables_streamed(plan, sink=ParquetTransactionalSink(full_dir), **kwargs)
    pinned = generate_tables_streamed_from_generation_plan(
        plan.generation, sink=ParquetTransactionalSink(pinned_dir), **kwargs
    )
    assert (pinned.mode, pinned.reason, pinned.tables) == (full.mode, full.reason, full.tables)
    assert pinned.mode == "streamed" and pinned.row_counts == full.row_counts
    for path in sorted(full_dir.glob("*.parquet")):
        assert pq.read_table(pinned_dir / path.name).equals(pq.read_table(path))


def test_streamed_pinned_entry_resident_mode_for_a_legacy_plan() -> None:
    plan = plan_of(_legacy())
    full = generate_tables_streamed(plan, sink=None)
    pinned = generate_tables_streamed_from_generation_plan(plan.generation, sink=None)
    assert pinned.mode == full.mode == "resident" and pinned.reason == full.reason
    assert pinned.tables is not None and full.tables is not None
    assert fingerprints(pinned.tables) == fingerprints(full.tables)


# ---- 3. no reopen ----------------------------------------------------------------------------


def test_pinned_entry_reads_only_the_pinned_snapshot_bytes(tmp_path: Path) -> None:
    copy = tmp_path / "snap.json"
    shutil.copy(fx.SNAPSHOT_PATH, copy)
    cfg = _legacy()
    for col in cfg["tables"][0]["generate_columns"]:
        col["snapshot_file"] = str(copy)
    plan = plan_of(cfg)
    expected = generate_tables(plan)
    copy.unlink()
    pinned = generate_tables_from_generation_plan(plan.generation)
    assert fingerprints(pinned) == fingerprints(expected)


# ---- 4. type guard and contract preservation -------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        generate_tables_from_generation_plan,
        lambda gp, **kw: generate_tables_streamed_from_generation_plan(gp, sink=None, **kw),
    ],
    ids=["resident", "streamed"],
)
def test_pinned_entries_reject_a_dict_and_a_full_plan(entry: Any) -> None:
    plan = plan_of(_streamable())
    with pytest.raises(TypeError, match="requires a compiled GenerationPlan"):
        entry({"config_json": plan.generation.config_json})
    with pytest.raises(TypeError, match="got 'Plan'"):
        entry(plan)


@pytest.mark.parametrize("fn", [generate_tables, lambda p: generate_tables_streamed(p, sink=None)])
def test_full_plan_entries_still_reject_a_bare_generation_plan(fn: Any) -> None:
    with pytest.raises(TypeError, match="requires a compiled decoy_engine.plan.Plan"):
        fn(plan_of(_streamable()).generation)


# ---- 5. DP receipt ---------------------------------------------------------------------------


def test_dp_with_statistical_column_and_no_receipt_is_refused() -> None:
    gp = _dp_generation(statistical=True, receipt=False)
    with pytest.raises(TypeError, match="no reproduced DpVerification receipt"):
        generate_tables_from_generation_plan(gp)


def test_dp_refusal_matches_the_full_plan_path() -> None:
    plan = plan_of(_streamable())
    bad = dataclasses.replace(plan, generation=_dp_generation(statistical=True, receipt=False))
    with pytest.raises(TypeError, match="no reproduced DpVerification receipt"):
        generate_tables(bad)


def test_dp_without_statistical_columns_generates() -> None:
    gp = _dp_generation(statistical=False, receipt=False)
    tables = generate_tables_from_generation_plan(gp)
    assert tables["t"].num_rows == 3
    plan = plan_of(_streamable())
    full = generate_tables(dataclasses.replace(plan, generation=gp))
    assert fingerprints(tables) == fingerprints(full)


# ---- 6. budget_bytes -------------------------------------------------------------------------


def test_budget_success_and_refusal_match_the_full_plan_path() -> None:
    from decoy_engine.generation._budget_ledger import GenerationBudgetExceeded

    small = plan_of(_streamable())
    ok = 256 * 1024 * 1024
    assert fingerprints(
        generate_tables_from_generation_plan(small.generation, budget_bytes=ok)
    ) == fingerprints(generate_tables(small, budget_bytes=ok))
    # Refused by row-count arithmetic before any value is produced.
    plan = plan_of(fx.config([fx.table("big", 10_000_000, [{"name": "k", "type": "sequence"}])]))
    with pytest.raises(GenerationBudgetExceeded) as full_exc:
        generate_tables(plan, budget_bytes=1024 * 1024)
    with pytest.raises(GenerationBudgetExceeded) as pinned_exc:
        generate_tables_from_generation_plan(plan.generation, budget_bytes=1024 * 1024)
    assert pinned_exc.value.code == full_exc.value.code


# ---- 7. seed-envelope independence -----------------------------------------------------------


def test_pinned_entry_never_consults_the_seed_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.expressions import _when_parser

    def boom(_plan: Any) -> None:
        raise AssertionError("validate_plan_when must not run on the pinned path")

    monkeypatch.setattr(_when_parser, "validate_plan_when", boom)
    plan = plan_of(_streamable())
    assert generate_tables_from_generation_plan(plan.generation)


def test_full_plan_path_still_validates_the_seed_when() -> None:
    plan = _plan_with_bad_seed_when(plan_of(_streamable()))
    with pytest.raises(ValidationError) as exc:
        generate_tables(plan)
    assert exc.value.code == "when_outside_closed_grammar"
    # The same payload through the pinned entry does not depend on the envelope.
    assert generate_tables_from_generation_plan(plan.generation)


# ---- 8. error ordering and refactor safety ---------------------------------------------------


def test_full_plan_error_order_is_type_then_when_then_payload_then_dp() -> None:
    with pytest.raises(TypeError, match="requires a compiled"):
        generate_tables({"tables": []})

    base = plan_of(_streamable())
    # seed validation precedes the missing-payload guard
    both = dataclasses.replace(_plan_with_bad_seed_when(base), generation=None)
    with pytest.raises(ValidationError):
        generate_tables(both)
    with pytest.raises(TypeError, match="no generation payload"):
        generate_tables(dataclasses.replace(base, generation=None))
    # the payload decode (and so a DP refusal) comes after seed validation
    dp_bad = dataclasses.replace(
        _plan_with_bad_seed_when(base), generation=_dp_generation(statistical=True, receipt=False)
    )
    with pytest.raises(ValidationError):
        generate_tables(dp_bad)


def test_prepare_and_prepare_generation_yield_the_same_prepared_plan() -> None:
    plan = plan_of(_legacy())
    via_plan = plan_entry._prepare(plan, "t")
    via_generation = plan_entry._prepare_generation(plan.generation, "t")
    assert via_plan.config == via_generation.config
    assert via_plan.legacy_kwargs.keys() == via_generation.legacy_kwargs.keys()
    assert (
        via_plan.legacy_kwargs["snapshot_artifacts"]
        == (via_generation.legacy_kwargs["snapshot_artifacts"])
    )
    assert (
        via_plan.legacy_kwargs["snapshot_index_for_column"]
        == (via_generation.legacy_kwargs["snapshot_index_for_column"])
    )
