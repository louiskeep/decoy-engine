"""Pinned generation (GEN-API slice 1b-E): determinism, fingerprint, and fail-closed rejections."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation._pinned_anchor import resolve_anchor_cells
from decoy_engine.generation.pinned import (
    PinnedModeError,
    PinnedPlan,
    compile_pinned_plan,
    execute_pinned,
    fingerprint,
    runtime_identity,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from decoy_engine.transforms.windowed_date import WindowedDateError

REF = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
CHILD = Path(__file__).with_name("_pinned_child.py")
SRC = Path(__file__).resolve().parents[3] / "src"


def _write_snapshot(path: Path, loc: float = 100.0) -> str:
    rng = np.random.default_rng(7)
    df = pd.DataFrame({"score": rng.normal(loc, 25.0, size=500).round(2)})
    path.write_text(json.dumps(compute_distribution_snapshot(df)), encoding="utf-8")
    return str(path)


def _config(snapshot: str, seed: int = 11, **overrides: Any) -> dict[str, Any]:
    """Child table sorts BEFORE its parent so order bugs show; covers all 8 pure types + statistical."""
    parent = {
        "name": "zeta_customers",
        "row_count": 40,
        "generate_columns": [
            {"name": "id", "type": "sequence", "start": 100, "step": 3, "prefix": "C-"},
            {
                "name": "region",
                "type": "categorical",
                "categories": ["east", "west", "north"],
                "weights": [0.5, 0.3, 0.2],
            },
            {
                "name": "signup",
                "type": "categorical",
                "categories": ["2024-01-15", "now", "today", "2023-05-01T10:30:00"],
            },
            {
                "name": "renewal",
                "type": "windowed_date",
                "anchor": "signup",
                "min_days": 1,
                "max_days": 90,
                "distribution": "late",
            },
            {
                "name": "score",
                "type": "statistical",
                "snapshot_file": snapshot,
                "null_probability": 0.2,
            },
            {"name": "double", "type": "derived", "expression": "score * 2"},
            {"name": "total", "type": "derived_aggregate", "op": "sum", "column": "score"},
            {
                "name": "acct",
                "type": "group_key",
                "group_by": "region",
                "length": 8,
                "prefix": "A-",
            },
            {
                "name": "walk",
                "type": "grouped_series",
                "group_by": "region",
                "order_by": "id",
                "generator": "monotone_walk",
            },
        ],
    }
    child = {
        "name": "alpha_orders",
        "row_count": 25,
        "generate_columns": [
            {"name": "order_no", "type": "sequence", "start": 1},
            {
                "name": "customer",
                "type": "reference",
                "reference_table": "zeta_customers",
                "reference_column": "id",
                "distribution": "random",
                "min_per_parent": 1,
                "max_per_parent": 3,
                "null_probability": 0.1,
            },
        ],
    }
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": [child, parent],
        "targets": {},
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def snap(tmp_path: Path) -> str:
    return _write_snapshot(tmp_path / "snap.json")


def _digest(tables: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for name, table in tables.items():
        h.update(name.encode())
        for col in table.column_names:
            h.update(col.encode())
            h.update(repr(table.column(col).to_pylist()).encode())
    return h.hexdigest()


def _run_child(
    cfg_path: Path, hash_seed: str, days: int, seed: int = 5, ref: str = "2026-10-22T09:30:00Z"
) -> dict[str, Any]:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(  # noqa: S603 -- fixed argv, own interpreter
        [sys.executable, str(CHILD), str(cfg_path), ref, str(seed), str(days)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=300,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class TestCrossProcessDeterminism:
    def test_identical_across_hash_seeds_and_wall_clock_days(
        self, tmp_path: Path, snap: str
    ) -> None:
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text(json.dumps(_config(snap)), encoding="utf-8")
        a = _run_child(cfg_path, "1", 0)
        b = _run_child(cfg_path, "977", 0)
        c = _run_child(cfg_path, "31337", 400)
        assert a == b == c
        assert a["tables"] == ["alpha_orders", "zeta_customers"]

    def test_in_process_matches_child(self, tmp_path: Path, snap: str) -> None:
        cfg = _config(snap)
        cfg_path = tmp_path / "cfg.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        child = _run_child(cfg_path, "0", 0, seed=5)
        plan = compile_pinned_plan(cfg, REF)
        assert _digest(execute_pinned(plan, 5)) == child["digest"]
        assert fingerprint(plan) == child["fingerprint"]


class TestOrderingAndShape:
    def test_tables_sorted_columns_declared_parent_first(self, snap: str) -> None:
        cfg = _config(snap)
        out = execute_pinned(compile_pinned_plan(cfg, REF), 3)
        assert list(out) == ["alpha_orders", "zeta_customers"]
        assert out["zeta_customers"].column_names == [
            c["name"] for c in cfg["tables"][1]["generate_columns"]
        ]
        parents = set(out["zeta_customers"].column("id").to_pylist())
        assert {v for v in out["alpha_orders"].column("customer").to_pylist() if v} <= parents

    def test_every_supported_type_is_covered(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        kinds = {json.loads(c.config_json)["type"] for t in plan.tables for c in t.columns}
        assert kinds == {
            "sequence",
            "categorical",
            "reference",
            "derived",
            "derived_aggregate",
            "grouped_series",
            "group_key",
            "windowed_date",
            "statistical",
        }


class TestFingerprint:
    def test_seed_independent(self, snap: str) -> None:
        f1 = fingerprint(compile_pinned_plan(_config(snap, seed=1), REF))
        f2 = fingerprint(compile_pinned_plan(_config(snap, seed=999999), REF))
        assert f1 == f2

    def test_caller_seed_changes_output_not_fingerprint(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        assert _digest(execute_pinned(plan, 1)) != _digest(execute_pinned(plan, 2))
        assert _digest(execute_pinned(plan, 1)) == _digest(execute_pinned(plan, 1))

    def test_statistical_snapshot_byte_change_changes_fingerprint(
        self, tmp_path: Path, snap: str
    ) -> None:
        base = fingerprint(compile_pinned_plan(_config(snap), REF))
        other = _write_snapshot(tmp_path / "other.json", loc=250.0)
        assert fingerprint(compile_pinned_plan(_config(other), REF)) != base

    def test_snapshot_path_is_not_identity(self, tmp_path: Path, snap: str) -> None:
        moved = tmp_path / "elsewhere.json"
        shutil.copyfile(snap, moved)
        assert fingerprint(compile_pinned_plan(_config(str(moved)), REF)) == fingerprint(
            compile_pinned_plan(_config(snap), REF)
        )

    def test_reference_date_is_part_of_identity(self, snap: str) -> None:
        a = fingerprint(compile_pinned_plan(_config(snap), REF))
        b = fingerprint(compile_pinned_plan(_config(snap), REF + timedelta(days=1)))
        assert a != b

    def test_row_count_and_column_config_are_part_of_identity(self, snap: str) -> None:
        base = fingerprint(compile_pinned_plan(_config(snap), REF))
        cfg = _config(snap)
        cfg["tables"][0]["row_count"] = 26
        assert fingerprint(compile_pinned_plan(cfg, REF)) != base
        cfg = _config(snap)
        cfg["tables"][1]["generate_columns"][1]["weights"] = [0.4, 0.4, 0.2]
        assert fingerprint(compile_pinned_plan(cfg, REF)) != base

    def test_serialization_round_trip_is_stable(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        again = PinnedPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
        assert again == plan
        assert fingerprint(again) == fingerprint(plan)
        assert _digest(execute_pinned(again, 4)) == _digest(execute_pinned(plan, 4))

    def test_runtime_identity_names_numpy_and_friends(self, snap: str) -> None:
        ident = dict(compile_pinned_plan(_config(snap), REF).runtime_identity)
        assert {"engine", "numpy", "pandas", "pyarrow", "python", "tz"} <= set(ident)
        assert ident["numpy"] == np.__version__
        assert ident["tz"] == "UTC"
        assert dict(runtime_identity()) == ident

    def test_runtime_change_is_refused_at_execution(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        ident = dict(plan.runtime_identity)
        ident["numpy"] = "0.0.0"
        stale = dataclasses.replace(plan, runtime_identity=tuple(sorted(ident.items())))
        assert fingerprint(stale) != fingerprint(plan)
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(stale, 1)
        assert exc.value.code == "pinned_runtime_changed"


class TestReferenceDateAnchors:
    def _anchor_cfg(self, snap: str) -> dict[str, Any]:
        return _config(snap)

    def test_now_and_today_resolve_to_reference_date(self, snap: str) -> None:
        out = execute_pinned(compile_pinned_plan(self._anchor_cfg(snap), REF), 8)["zeta_customers"]
        signup = out.column("signup").to_pylist()
        renewal = out.column("renewal").to_pylist()
        assert "now" in signup and "today" in signup
        for s, r in zip(signup, renewal, strict=True):
            anchor = {"now": "2026-10-22T09:30:00", "today": "2026-10-22"}.get(s, s)
            delta = pd.Timestamp(r) - pd.Timestamp(anchor).normalize()
            assert 1 <= delta.days <= 90

    def test_moving_reference_date_moves_only_now_today_rows(self, snap: str) -> None:
        cfg = self._anchor_cfg(snap)
        a = execute_pinned(compile_pinned_plan(cfg, REF), 8)["zeta_customers"]
        b = execute_pinned(compile_pinned_plan(cfg, REF + timedelta(days=30)), 8)["zeta_customers"]
        # The reference date is part of the plan, so the seed derivation is unchanged and the
        # offsets match; only the resolved anchors differ.
        for s, ra, rb in zip(
            a.column("signup").to_pylist(),
            a.column("renewal").to_pylist(),
            b.column("renewal").to_pylist(),
            strict=True,
        ):
            if s in ("now", "today"):
                assert ra != rb
            else:
                assert ra == rb

    def test_time_only_and_partial_anchors_are_refused_at_compile(self, snap: str) -> None:
        for bad in ("12:00", "tomorrow", "Oct 5", "2024-01-15T10:30:00+02:00"):
            cfg = _config(snap)
            cfg["tables"][1]["generate_columns"][2]["categories"] = ["2024-01-15", bad]
            with pytest.raises(PinnedModeError) as exc:
                compile_pinned_plan(cfg, REF)
            assert exc.value.code == "pinned_windowed_anchor_not_absolute"

    def test_time_only_anchor_is_refused_at_runtime(self) -> None:
        with pytest.raises(PinnedModeError) as exc:
            resolve_anchor_cells(["2024-01-15", "12:00"], "2026-10-22T09:30:00Z")
        assert exc.value.code == "pinned_windowed_anchor_not_absolute"
        assert "12:00" not in str(exc.value)

    def test_hand_edited_plan_with_bad_anchor_is_refused(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        tables = list(plan.tables)
        idx = next(i for i, t in enumerate(tables) if t.name == "zeta_customers")
        cols = list(tables[idx].columns)
        signup = json.loads(cols[2].config_json)
        signup["categories"] = ["12:00"]
        cols[2] = dataclasses.replace(cols[2], config_json=json.dumps(signup, sort_keys=True))
        tables[idx] = dataclasses.replace(tables[idx], columns=tuple(cols))
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(dataclasses.replace(plan, tables=tuple(tables)), 1)
        assert exc.value.code == "pinned_windowed_anchor_not_absolute"

    def test_reference_date_must_be_timezone_aware(self, snap: str) -> None:
        with pytest.raises(PinnedModeError) as exc:
            compile_pinned_plan(_config(snap), datetime(2026, 10, 22, 9, 30))
        assert exc.value.code == "pinned_reference_date_invalid"


class TestRejections:
    @staticmethod
    def _with_column(snap: str, col: dict[str, Any]) -> dict[str, Any]:
        cfg = _config(snap)
        cfg["tables"][1]["generate_columns"].append(col)
        return cfg

    @pytest.mark.parametrize(
        ("col", "code"),
        [
            ({"name": "n", "type": "faker", "faker_type": "name"}, "pinned_faker_unsupported"),
            (
                {"name": "n", "type": "faker", "faker_type": "pyint", "locale": "en_US"},
                "pinned_faker_unsupported",
            ),
            ({"name": "f", "type": "formula", "formula": "1 + 1"}, "pinned_formula_unsupported"),
            (
                {"name": "c", "type": "categorical", "categories": ["a"], "determinism": "fresh"},
                "pinned_determinism_fresh",
            ),
            (
                {
                    "name": "c",
                    "type": "categorical",
                    "categories": ["a"],
                    "snapshot_file": "/tmp/x.json",
                },
                "pinned_snapshot_file_not_statistical",
            ),
        ],
    )
    def test_rejected_construct_is_a_coded_error(
        self, snap: str, col: dict[str, Any], code: str
    ) -> None:
        with pytest.raises(PinnedModeError) as exc:
            compile_pinned_plan(self._with_column(snap, col), REF)
        assert exc.value.code == code
        assert str(exc.value).startswith(f"[{code}]")

    def test_hand_built_plan_with_rejected_column_is_refused(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        tables = list(plan.tables)
        faker = dataclasses.replace(
            tables[0].columns[0],
            config_json=json.dumps({"name": "order_no", "type": "faker", "faker_type": "name"}),
        )
        tables[0] = dataclasses.replace(tables[0], columns=(faker, *tables[0].columns[1:]))
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(dataclasses.replace(plan, tables=tuple(tables)), 1)
        assert exc.value.code == "pinned_faker_unsupported"

    def test_mask_table_is_refused(self, snap: str) -> None:
        cfg = _config(snap)
        cfg["tables"].append({"name": "masked", "source": "src"})
        with pytest.raises(PinnedModeError) as exc:
            compile_pinned_plan(cfg, REF)
        assert exc.value.code == "pinned_not_generation_only"

    @pytest.mark.parametrize("bad", [None, True, -1, 2**64, 1.5, "7"])
    def test_seed_is_required_and_validated(self, snap: str, bad: Any) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(plan, bad)
        assert exc.value.code == "pinned_seed_invalid"

    def test_no_external_derive_key(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        with pytest.raises(TypeError):
            execute_pinned(plan, 1, derive_key=lambda info: b"k" * 32)  # type: ignore[call-arg]


class TestNoAmbientInputs:
    def test_no_snapshot_reread_and_no_entropy(
        self, tmp_path: Path, snap: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _config(snap)
        plan = compile_pinned_plan(cfg, REF)
        expected = _digest(execute_pinned(plan, 9))
        Path(snap).unlink()

        def _boom(_n: int) -> bytes:
            raise AssertionError("pinned execution touched os.urandom")

        monkeypatch.setattr(os, "urandom", _boom)
        assert _digest(execute_pinned(plan, 9)) == expected

    def test_missing_digest_is_a_coded_error_not_a_reread(self, snap: str) -> None:
        plan = compile_pinned_plan(_config(snap), REF)
        broken = dataclasses.replace(
            plan,
            statistical=tuple(dataclasses.replace(s, snapshot_digest="") for s in plan.statistical),
        )
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(broken, 1)
        assert exc.value.code == "pinned_statistical_digest_missing"
        dropped = dataclasses.replace(plan, statistical=())
        with pytest.raises(PinnedModeError) as exc2:
            execute_pinned(dropped, 1)
        assert exc2.value.code == "pinned_statistical_digest_missing"

    def test_null_injection_uses_the_pinned_digest(self, snap: str) -> None:
        # The null mask of the statistical column is derived from the pinned digest, so it must
        # match between a plan whose snapshot file is gone and one whose file is present.
        plan = compile_pinned_plan(_config(snap), REF)
        with_file = execute_pinned(plan, 21)["zeta_customers"].column("score").to_pylist()
        Path(snap).unlink()
        without = execute_pinned(plan, 21)["zeta_customers"].column("score").to_pylist()
        assert with_file == without
        assert any(v is None for v in with_file)


def test_ordinary_generation_is_untouched_by_pinned_mode(snap: str) -> None:
    """The ordinary path still reads global_settings.seed and accepts faker."""
    from tests.unit._dps_helpers import compile_and_generate

    cfg = _config(snap)
    cols = cfg["tables"][1]["generate_columns"]
    cols[2]["categories"] = ["2024-01-15", "2023-05-01"]  # ordinary mode keeps the strict grammar
    cols.append({"name": "who", "type": "faker", "faker_type": "name"})
    a = compile_and_generate(copy.deepcopy(cfg))
    b = compile_and_generate(copy.deepcopy(cfg))
    assert a["zeta_customers"].equals(b["zeta_customers"])

    cols[2]["categories"] = ["2024-01-15", "now"]
    with pytest.raises(WindowedDateError):
        compile_and_generate(copy.deepcopy(cfg))


GOLDEN_ORDINARY_NULL_STATISTICAL = (
    "7c609d6c04f896bde2c5073cf0bf0b2ca9dbc4c9fd5de71778285b32438e2082"
)


def test_ordinary_statistical_null_injection_matches_origin_main_golden() -> None:
    """Digest captured from clean origin/main (b63be004); guards the ordinary null-seed path."""
    from tests.unit._dps_helpers import compile_and_generate

    snap_path = Path(__file__).resolve().parents[2] / "fixtures/phase_f_f0/stat_snapshot.json"
    cfg = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {},
        "targets": {},
        "tables": [
            {
                "name": "t",
                "row_count": 60,
                "generate_columns": [
                    {
                        "name": "amount",
                        "type": "statistical",
                        "snapshot_file": str(snap_path),
                        "null_probability": 0.3,
                    }
                ],
            }
        ],
    }
    col = compile_and_generate(cfg)["t"].column("amount").to_pylist()
    assert hashlib.sha256(repr(col).encode()).hexdigest() == GOLDEN_ORDINARY_NULL_STATISTICAL


def test_pinnable_allowlist_covers_every_config_type() -> None:
    """A new GenerateColumnConfig type must be classified before it can enter pinned mode."""
    import typing

    from decoy_engine.config._tables import GenerateColumnConfig
    from decoy_engine.generation._pinned_compile import PINNABLE_TYPES, REJECTED_TYPES

    known = set(typing.get_args(GenerateColumnConfig.model_fields["type"].annotation))
    unclassified = known - set(PINNABLE_TYPES) - set(REJECTED_TYPES)
    assert not unclassified, f"classify for pinned mode: {sorted(unclassified)}"
    assert set(REJECTED_TYPES) == {"faker", "formula"}


def test_unlisted_type_is_refused_at_compile_and_execute(snap: str) -> None:
    from decoy_engine.generation._pinned_compile import reject_unpinnable

    cfg = {"tables": [{"name": "t", "generate_columns": [{"name": "x", "type": "novel"}]}]}
    with pytest.raises(PinnedModeError) as exc:
        reject_unpinnable(cfg)
    assert exc.value.code == "pinned_type_not_allowlisted"

    plan = compile_pinned_plan(_config(snap), REF)
    tables = list(plan.tables)
    bad = dataclasses.replace(
        tables[0].columns[0], config_json=json.dumps({"name": "order_no", "type": "novel"})
    )
    tables[0] = dataclasses.replace(tables[0], columns=(bad, *tables[0].columns[1:]))
    with pytest.raises(PinnedModeError) as exc2:
        execute_pinned(dataclasses.replace(plan, tables=tuple(tables)), 1)
    assert exc2.value.code == "pinned_type_not_allowlisted"


def test_hybrid_source_table_is_refused(snap: str) -> None:
    cfg = _config(snap)
    cfg["tables"][0]["source"] = "src"
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, REF)
    assert exc.value.code == "pinned_not_generation_only"


def test_cyclic_plan_is_a_coded_error(snap: str) -> None:
    plan = compile_pinned_plan(_config(snap), REF)
    tables = tuple(
        dataclasses.replace(t, depends_on=("alpha_orders",)) if t.name == "zeta_customers" else t
        for t in plan.tables
    )
    with pytest.raises(PinnedModeError) as exc:
        execute_pinned(dataclasses.replace(plan, tables=tables), 1)
    assert exc.value.code == "pinned_plan_malformed"


def _inf_count_config() -> dict[str, Any]:
    return {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {},
        "targets": {},
        "tables": [
            {
                "name": "t",
                "row_count": 2,
                "generate_columns": [
                    {"name": "big", "type": "derived", "expression": "1e308 * 1e308"},
                    {"name": "n", "type": "derived_aggregate", "op": "count", "column": "big"},
                ],
            }
        ],
    }


def test_pinned_output_ignores_ambient_pandas_options() -> None:
    import warnings

    plan = compile_pinned_plan(_inf_count_config(), REF)
    results = []
    for flag in (False, True):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            with pd.option_context("mode.use_inf_as_na", flag):
                results.append(execute_pinned(plan, 1)["t"].column("n").to_pylist())
    assert results[0] == results[1]


def test_column_name_is_part_of_the_fingerprint(snap: str) -> None:
    plan = compile_pinned_plan(_config(snap), REF)
    tables = list(plan.tables)
    renamed = dataclasses.replace(tables[0].columns[0], name="renamed")
    tables[0] = dataclasses.replace(tables[0], columns=(renamed, *tables[0].columns[1:]))
    other = dataclasses.replace(plan, tables=tuple(tables))
    assert fingerprint(other) != fingerprint(plan)
    with pytest.raises(PinnedModeError) as exc:
        execute_pinned(other, 1)
    assert exc.value.code == "pinned_plan_malformed"


@pytest.mark.parametrize("value", ["${env.SECRET}", "${var.x}", "pre-${storm.k}-post"])
def test_live_resolver_tokens_are_refused(snap: str, value: str) -> None:
    cfg = _config(snap)
    cfg["tables"][0]["generate_columns"][0]["prefix"] = value
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, REF)
    assert exc.value.code == "pinned_live_token"
    assert "${" not in str(exc.value)
    assert "SECRET" not in str(exc.value)

    plan = compile_pinned_plan(_config(snap), REF)
    tables = list(plan.tables)
    col = json.loads(tables[0].columns[0].config_json)
    col["prefix"] = value
    bad = dataclasses.replace(tables[0].columns[0], config_json=json.dumps(col))
    tables[0] = dataclasses.replace(tables[0], columns=(bad, *tables[0].columns[1:]))
    with pytest.raises(PinnedModeError) as exc2:
        execute_pinned(dataclasses.replace(plan, tables=tuple(tables)), 1)
    assert exc2.value.code == "pinned_live_token"
    assert "${" not in str(exc2.value)


def test_live_token_in_a_column_name_is_not_echoed(snap: str) -> None:
    cfg = _config(snap)
    cfg["tables"][0]["generate_columns"][0]["name"] = "pre-${env.SECRET}-post"
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, REF)
    assert exc.value.code == "pinned_live_token"
    assert "${" not in str(exc.value) and "SECRET" not in str(exc.value)


def test_tokens_in_plan_names_and_dependencies_are_not_echoed(snap: str) -> None:
    plan = compile_pinned_plan(_config(snap), REF)
    evil = "pre-${env.SECRET}-post"
    child = plan.tables[0]
    col0 = child.columns[0]
    variants = [
        dataclasses.replace(
            child, columns=(dataclasses.replace(col0, name=evil), *child.columns[1:])
        ),
        dataclasses.replace(child, depends_on=(*child.depends_on, evil)),
        dataclasses.replace(child, name=evil),
    ]
    for table in variants:
        bad = dataclasses.replace(plan, tables=(table, *plan.tables[1:]))
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(bad, 1)
        assert exc.value.code == "pinned_live_token"
        assert "${" not in str(exc.value) and "SECRET" not in str(exc.value)


def test_malformed_plan_messages_do_not_echo_names(snap: str) -> None:
    plan = compile_pinned_plan(_config(snap), REF)
    child = plan.tables[0]
    renamed = dataclasses.replace(child.columns[0], name="leaky_name")
    cases = [
        dataclasses.replace(child, columns=(renamed, *child.columns[1:])),
        dataclasses.replace(child, depends_on=("leaky_table",)),
    ]
    for table in cases:
        with pytest.raises(PinnedModeError) as exc:
            execute_pinned(dataclasses.replace(plan, tables=(table, *plan.tables[1:])), 1)
        assert exc.value.code == "pinned_plan_malformed"
        assert "leaky" not in str(exc.value)


def test_token_in_global_settings_seed_is_refused_without_echo(snap: str) -> None:
    cfg = _config(snap)
    cfg["global_settings"]["seed"] = "${env.SECRET}"
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, REF)
    assert exc.value.code == "pinned_live_token"
    assert "${" not in str(exc.value) and "SECRET" not in str(exc.value)


def test_token_in_statistical_spec_is_refused_without_echo(snap: str) -> None:
    plan = compile_pinned_plan(_config(snap), REF)
    stat = plan.statistical[0]
    spec = json.loads(stat.spec_json)
    spec["column"] = "${env.SECRET}"
    bad = dataclasses.replace(
        stat, column="${env.SECRET}", spec_json=json.dumps(spec, sort_keys=True)
    )
    with pytest.raises(PinnedModeError) as exc:
        execute_pinned(dataclasses.replace(plan, statistical=(bad,)), 1)
    assert exc.value.code == "pinned_live_token"
    assert "${" not in str(exc.value) and "SECRET" not in str(exc.value)

    spec_only = dataclasses.replace(
        stat, spec_json=json.dumps({**json.loads(stat.spec_json), "dtype": "${var.x}"})
    )
    with pytest.raises(PinnedModeError) as exc2:
        execute_pinned(dataclasses.replace(plan, statistical=(spec_only,)), 1)
    assert exc2.value.code == "pinned_live_token"
    assert "${" not in str(exc2.value)


def test_token_in_a_config_key_is_not_echoed(snap: str) -> None:
    cfg = _config(snap)
    cfg["tables"][0]["generate_columns"][0]["x-${env.SECRET}"] = 1
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg, REF)
    assert exc.value.code == "pinned_live_token"
    assert "${" not in str(exc.value) and "SECRET" not in str(exc.value)
