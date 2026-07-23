"""`statistical` generate type (capability-gaps WS3, 2026-06-12).

Samples synthetic columns from a distribution-snapshot/v1 artifact
(quality/snapshot.py) instead of a hand-declared faker/categorical
config: histogram inverse-CDF for numeric, weighted top-k for
categorical, year-bin + uniform-within-year for datetime, declared-pair
conditional sampling via the snapshot's joint contingency tables.
compute_fidelity is the acceptance oracle for distribution shape.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from decoy_engine.generation.statistical import StatisticalSpecError, load_spec, sample_column
from decoy_engine.generation.statistical._spec import _load_snapshot


def _load_spec(col_cfg: dict, **kwargs) -> object:
    """`load_spec` no longer reads `snapshot_file` itself (guide step 5/8:
    the plan compiler pins snapshot bytes once; `load_spec` only consumes
    an already-parsed mapping). This file's tests exercise `load_spec`
    directly against a path-carrying `col_cfg`, so read+parse it here at
    the call site via the same `_load_snapshot` the compiler's read-once
    pass uses, preserving each test's original unreadable/malformed-path
    assertions unchanged."""
    _digest, snapshot = _load_snapshot(col_cfg["snapshot_file"])
    return load_spec(col_cfg, snapshot=snapshot, **kwargs)


def _write_snapshot(tmp_path, df: pd.DataFrame, joints=None) -> str:
    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    snap = compute_distribution_snapshot(df, joint_columns=joints)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _source_df() -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(7)
    n = 2_000
    states = rng.choice(["CA", "NY", "TX", "WA"], size=n, p=[0.5, 0.3, 0.15, 0.05])
    # tier correlates hard with state: CA -> gold, NY -> silver, rest -> bronze.
    tier = [{"CA": "gold", "NY": "silver"}.get(s, "bronze") for s in states]
    return pd.DataFrame(
        {
            "amount": rng.normal(100.0, 25.0, size=n).round(2),
            "age": rng.integers(18, 80, size=n),
            "state": states,
            "tier": tier,
            "joined": pd.to_datetime("2020-01-01")
            + pd.to_timedelta(rng.integers(0, 365 * 5, size=n), unit="D"),
        }
    )


def _col(name: str, snapshot_file: str, **extra) -> dict:
    return {"name": name, "type": "statistical", "snapshot_file": snapshot_file, **extra}


class TestNumericSampling:
    def test_values_in_source_range_and_deterministic(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("amount", snap))
        a = sample_column(spec, 500, col_seed=1234)
        b = sample_column(spec, 500, col_seed=1234)
        assert a == b
        assert all(df["amount"].min() <= v <= df["amount"].max() for v in a)
        assert all(isinstance(v, float) for v in a)

    def test_different_seed_different_values(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        spec = _load_spec(_col("amount", snap))
        assert sample_column(spec, 200, col_seed=1) != sample_column(spec, 200, col_seed=2)

    def test_integer_dtype_emits_ints(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("age", snap))
        out = sample_column(spec, 300, col_seed=9)
        assert all(isinstance(v, int) for v in out)
        assert all(df["age"].min() <= v <= df["age"].max() for v in out)

    def test_distribution_shape_matches_source(self, tmp_path):
        """compute_fidelity is the acceptance oracle: synthetic vs source
        must score clearly above what a uniform stand-in would."""
        from decoy_engine.quality.fidelity import compute_fidelity
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("amount", snap))
        synth = pd.DataFrame({"amount": sample_column(spec, 2_000, col_seed=77)})
        report = compute_fidelity(
            compute_distribution_snapshot(df[["amount"]]),
            compute_distribution_snapshot(synth),
        )
        assert report["overall_score"] >= 0.85, report


class TestCategoricalSampling:
    def test_requires_allow_real_categories(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("state", snap))
        assert exc.value.code == "statistical_real_categories_not_allowed"

    def test_redistribute_emits_only_top_values(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("state", snap, allow_real_categories=True))
        out = sample_column(spec, 1_000, col_seed=5)
        assert set(out) <= {"CA", "NY", "TX", "WA"}
        # Rough shape: CA is the majority class at p=0.5.
        assert 350 <= out.count("CA") <= 650

    def test_emit_mode_emits_other_token(self, tmp_path):
        # Force a tail: top_k=2 collapses TX/WA into other_count.
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = _source_df()
        snap_dict = compute_distribution_snapshot(df, categorical_top_k=2)
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        spec = _load_spec(_col("state", str(path), allow_real_categories=True, other_mode="emit"))
        out = sample_column(spec, 1_000, col_seed=5)
        assert "__other__" in set(out)
        assert set(out) <= {"CA", "NY", "__other__"}

    def test_deterministic(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        spec = _load_spec(_col("state", snap, allow_real_categories=True))
        assert sample_column(spec, 400, col_seed=3) == sample_column(spec, 400, col_seed=3)

    def test_truthy_string_allow_real_categories_rejected(self, tmp_path):
        """HIGH-1: `bool("false")` is True -- a string value must not sail
        through the consent gate. Only a literal bool `true` permits real
        categories."""
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("state", snap, allow_real_categories="false"))
        assert exc.value.code == "statistical_allow_real_categories_invalid_type"

    def test_truthy_int_allow_real_categories_rejected(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("state", snap, allow_real_categories=1))
        assert exc.value.code == "statistical_allow_real_categories_invalid_type"


class TestHighCardinalitySampling:
    """HC-5: full-vocabulary retention opt-in. The sampler itself needs no
    high_cardinality-specific code -- it draws over whatever `top_values`
    list the snapshot carries, so these tests exercise load_spec's
    validation plus the resulting full-vocab draw end to end."""

    def _high_card_df(self, n_codes: int = 40) -> pd.DataFrame:
        import numpy as np

        rng = np.random.default_rng(9)
        codes = [f"C{i:03d}" for i in range(n_codes)]
        return pd.DataFrame({"code": rng.choice(codes, size=1_000)})

    def test_requires_allow_real_categories(self, tmp_path):
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = self._high_card_df()
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("code", str(path), high_cardinality=True))
        assert exc.value.code == "statistical_high_cardinality_requires_real_categories"

    def test_truthy_string_allow_real_categories_rejected(self, tmp_path):
        """HIGH-1: a high_cardinality column with a stringy truthy
        allow_real_categories must still be rejected as a type error, not
        silently pass the consent gate."""
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = self._high_card_df()
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(
                _col(
                    "code",
                    str(path),
                    high_cardinality=True,
                    allow_real_categories="false",
                )
            )
        assert exc.value.code == "statistical_allow_real_categories_invalid_type"

    def test_snapshot_without_high_cardinality_marker_rejected(self, tmp_path):
        """HIGH-2: an ordinary top-K categorical snapshot (fit WITHOUT
        high_cardinality_columns) paired with a generate spec that declares
        high_cardinality: true must be rejected -- the tail is already
        gone from the artifact, so accepting this would silently drop it
        on every redistribute draw."""
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(25)]})
        snap_dict = compute_distribution_snapshot(df, categorical_top_k=20)
        assert snap_dict["columns"]["code"]["stats"]["other_count"] > 0
        assert "high_cardinality" not in snap_dict["columns"]["code"]["stats"]
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(
                _col(
                    "code",
                    str(path),
                    high_cardinality=True,
                    allow_real_categories=True,
                )
            )
        assert exc.value.code == "statistical_high_cardinality_snapshot_mismatch"

    def test_snapshot_with_high_cardinality_marker_accepted(self, tmp_path):
        """HIGH-2 counterpart: a snapshot actually fit with
        high_cardinality_columns carries the marker and load_spec accepts
        it."""
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = self._high_card_df(n_codes=40)
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        assert snap_dict["columns"]["code"]["stats"]["high_cardinality"] is True
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        spec = _load_spec(
            _col("code", str(path), high_cardinality=True, allow_real_categories=True)
        )
        assert spec.kind == "categorical"

    def test_invalid_type_rejected(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("state", snap, allow_real_categories=True, high_cardinality="yes"))
        assert exc.value.code == "statistical_high_cardinality_invalid_type"

    def test_non_categorical_kind_rejected(self, tmp_path):
        # "amount" snapshots as numeric; high_cardinality only applies to a
        # categorical snapshot kind.
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("amount", snap, allow_real_categories=True, high_cardinality=True))
        assert exc.value.code == "statistical_high_cardinality_kind_invalid"

    def test_full_vocabulary_retained_and_sampled(self, tmp_path):
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = self._high_card_df(n_codes=40)
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        assert snap_dict["columns"]["code"]["stats"]["other_count"] == 0
        assert len(snap_dict["columns"]["code"]["stats"]["top_values"]) == 40
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        spec = _load_spec(
            _col("code", str(path), allow_real_categories=True, high_cardinality=True)
        )
        out = sample_column(spec, 2_000, col_seed=5)
        # Every drawn value is a real observed code; none collapsed to other.
        assert set(out) <= {f"C{i:03d}" for i in range(40)}
        assert len(set(out)) > 20  # broad coverage, not just the head of a top-k cut


class TestDatetimeSampling:
    def test_within_source_range_and_deterministic(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("joined", snap))
        a = sample_column(spec, 300, col_seed=11)
        b = sample_column(spec, 300, col_seed=11)
        assert a == b
        lo, hi = df["joined"].min(), df["joined"].max()
        assert all(lo <= pd.Timestamp(v) <= hi for v in a)


class TestConditionalSampling:
    def test_condition_on_respects_joint(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        parent_spec = _load_spec(_col("state", snap, allow_real_categories=True))
        parents = sample_column(parent_spec, 1_000, col_seed=21)
        child_spec = _load_spec(
            _col("tier", snap, allow_real_categories=True, condition_on="state")
        )
        children = sample_column(child_spec, 1_000, col_seed=22, parent_values=parents)
        # The source correlation is deterministic: CA -> gold, NY -> silver.
        pairs = list(zip(parents, children, strict=True))
        ca = [t for s, t in pairs if s == "CA"]
        ny = [t for s, t in pairs if s == "NY"]
        assert ca and ca.count("gold") / len(ca) > 0.9
        assert ny and ny.count("silver") / len(ny) > 0.9

    def test_condition_on_requires_joint_in_snapshot(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())  # no joints captured
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("tier", snap, allow_real_categories=True, condition_on="state"))
        assert exc.value.code == "statistical_joint_missing"


class TestSpecErrors:
    def test_missing_snapshot_file(self, tmp_path):
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("amount", str(tmp_path / "nope.json")))
        assert exc.value.code == "statistical_snapshot_unreadable"

    def test_unknown_column(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("ghost", snap))
        assert exc.value.code == "statistical_column_not_in_snapshot"

    def test_unknown_kind_rejected(self, tmp_path):
        """Freetext is admitted since deferred follow-up 4; an unknown or
        empty kind still gets the typed rejection."""
        df = pd.DataFrame({"blank": [None] * 10})
        snap = _write_snapshot(tmp_path, df)
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("blank", snap))
        assert exc.value.code == "statistical_kind_unsupported"

    def test_source_column_override(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("renamed_amount", snap, source_column="amount"))
        out = sample_column(spec, 50, col_seed=2)
        assert len(out) == 50


class TestReadOnceSnapshotPinning:
    """C-M1 (round-3 remediation): a `read_and_pin_snapshots` failure must
    be classified and handed to `check_statistical_columns` as `failures`,
    never silently dropped and re-derived by a second `open()` of the same
    path -- the single-read invariant (guide section 4.7, CHANGELOG.md).
    This writes an unreadable snapshot_file, runs the read-once pass
    (recording the classified failure), then SWAPS the file for valid
    content before `check_statistical_columns` runs -- simulating a race
    between the pinning pass and the check. The fixed code must raise the
    ORIGINAL classified failure without ever re-opening the swapped path.
    Separately, calling `resolve_pinned_snapshot` with `failures=None` (the
    pre-C-M1 fallback shape) against the same swapped path DOES succeed --
    proving a reopen at that point would have silently returned the
    swapped bytes instead of refusing, the exact defect this closes."""

    def test_check_statistical_columns_raises_the_classified_read_once_failure_never_reopens(
        self, tmp_path
    ):
        from decoy_engine.plan._checks import check_statistical_columns
        from decoy_engine.plan._errors import PlanCompileError
        from decoy_engine.plan._generation import read_and_pin_snapshots, resolve_pinned_snapshot

        bad_path = tmp_path / "snapshot.json"
        bad_path.write_text("NOT JSON AT ALL", encoding="utf-8")
        cfg = {
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [_col("amount", str(bad_path))],
                }
            ],
        }
        pinned, failures = read_and_pin_snapshots(cfg)
        assert str(bad_path) in failures
        assert failures[str(bad_path)].code == "statistical_snapshot_unreadable"
        assert str(bad_path) not in pinned

        # Swap the file for genuinely valid content AFTER the read-once
        # pass but BEFORE the check runs -- `_write_snapshot` always
        # writes to this same tmp_path/"snapshot.json" path.
        _write_snapshot(tmp_path, _source_df())

        with pytest.raises(PlanCompileError) as exc:
            check_statistical_columns(cfg, pinned, frozenset(), failures=failures)
        assert exc.value.code == "statistical_snapshot_unreadable"

        # The vulnerability class this closes: resolving the SAME path
        # without a failures record (the pre-C-M1 fallback) reopens the
        # file directly and, since it was swapped to valid content in the
        # meantime, succeeds -- silently returning the swapped bytes
        # instead of refusing.
        reopened = resolve_pinned_snapshot(str(bad_path), pinned, None)
        assert reopened["schema_version"]  # the reopen "worked" -- read fresh, swapped content


class TestFreetextSampling:
    """Length-only surrogate text (deferred follow-up 4, 2026-06-12)."""

    def _freetext_df(self) -> pd.DataFrame:
        import numpy as np

        rng = np.random.default_rng(3)
        return pd.DataFrame(
            {"comment": ["x" * int(n) for n in rng.normal(80, 20, size=1_000).clip(10, 160)]}
        )

    def test_lengths_within_source_range_and_deterministic(self, tmp_path):
        df = self._freetext_df()
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("comment", snap))
        a = sample_column(spec, 500, col_seed=11)
        b = sample_column(spec, 500, col_seed=11)
        assert a == b
        lo = df["comment"].str.len().min()
        hi = df["comment"].str.len().max()
        assert all(isinstance(v, str) and lo <= len(v) <= hi for v in a)

    def test_prefix_property_holds(self, tmp_path):
        """Per-row seeding (rng.seed(col_seed + i)): row i is independent of
        n, so any chunking of rows is byte-identical to a serial pass."""
        snap = _write_snapshot(tmp_path, self._freetext_df())
        spec = _load_spec(_col("comment", snap))
        assert sample_column(spec, 50, col_seed=7) == sample_column(spec, 100, col_seed=7)[:50]

    def test_length_distribution_matches_histogram(self, tmp_path):
        import json as _json

        df = self._freetext_df()
        snap_path = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("comment", snap_path))
        out = sample_column(spec, 5_000, col_seed=21)
        with open(snap_path, encoding="utf-8") as fh:
            snap = _json.load(fh)
        stats = snap["columns"]["comment"]["stats"]
        edges = stats["length_bin_edges"]
        counts = stats["length_bin_counts"]
        total = sum(counts)
        lengths = [len(v) for v in out]
        # Empirical per-bin proportion within a loose tolerance of the
        # fitted weights (binomial noise at n=5000 stays well under 0.05).
        for j, expected in enumerate(c / total for c in counts):
            lo_e, hi_e = edges[j], edges[j + 1]
            got = sum(
                1
                for length in lengths
                if (lo_e <= length < hi_e) or (j == len(counts) - 1 and length == hi_e)
            ) / len(lengths)
            assert abs(got - expected) < 0.05

    def test_constant_length_source_emits_constant_lengths(self, tmp_path):
        # Distinct values (so the snapshot classifies freetext, not
        # categorical) that share one exact length.
        df = pd.DataFrame({"comment": [f"fixed length str {i:03d}" for i in range(50)]})
        assert df["comment"].str.len().nunique() == 1
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("comment", snap))
        out = sample_column(spec, 100, col_seed=4)
        assert all(len(v) == len(df["comment"][0]) for v in out)

    def test_no_source_tokens_in_output(self, tmp_path):
        df = pd.DataFrame({"comment": [f"SECRETVALUE{i} appears here" for i in range(40)]})
        snap = _write_snapshot(tmp_path, df)
        spec = _load_spec(_col("comment", snap))
        out = sample_column(spec, 200, col_seed=5)
        assert not any("SECRETVALUE" in v for v in out)

    def test_condition_on_freetext_rejected(self, tmp_path):
        df = self._freetext_df()
        df["state"] = (["CA", "NY"] * 500)[: len(df)]
        snap = _write_snapshot(tmp_path, df)
        with pytest.raises(StatisticalSpecError) as exc:
            _load_spec(_col("comment", snap, condition_on="state"))
        assert exc.value.code == "statistical_condition_kind_invalid"

    def test_compile_check_passes_and_generate_tables_end_to_end(self, tmp_path):
        from decoy_engine.plan._checks import check_statistical_columns
        from tests.unit._dps_helpers import compile_and_generate

        snap = _write_snapshot(tmp_path, self._freetext_df())
        cfg = {
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "synthetic",
                    "row_count": 60,
                    "generate_columns": [
                        {"name": "comment", "type": "statistical", "snapshot_file": snap}
                    ],
                }
            ],
        }
        check_statistical_columns(cfg)
        out = compile_and_generate(cfg)
        values = out["synthetic"].column("comment").to_pylist()
        assert len(values) == 60 and all(isinstance(v, str) for v in values)


class TestGenerateTablesIntegration:
    def test_statistical_column_through_generate_tables(self, tmp_path):
        from decoy_engine.config import PipelineConfig
        from tests.unit._dps_helpers import compile_and_generate

        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "synthetic",
                    "row_count": 200,
                    "generate_columns": [
                        {
                            "name": "amount",
                            "type": "statistical",
                            "snapshot_file": snap,
                        },
                        {
                            "name": "state",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                        },
                        {
                            "name": "tier",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                            "condition_on": "state",
                        },
                    ],
                }
            ],
            "targets": {
                "synthetic": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                }
            },
        }
        validated = PipelineConfig.model_validate(cfg).model_dump()
        out = compile_and_generate(validated)
        tbl = out["synthetic"]
        assert tbl.num_rows == 200
        states = tbl.column("state").to_pylist()
        tiers = tbl.column("tier").to_pylist()
        ca_tiers = [t for s, t in zip(states, tiers, strict=True) if s == "CA"]
        assert ca_tiers and ca_tiers.count("gold") / len(ca_tiers) > 0.9
        # Determinism: same config, same bytes.
        again = compile_and_generate(validated)
        assert again["synthetic"].equals(tbl)

    def test_condition_on_must_reference_earlier_column(self, tmp_path):
        from decoy_engine.plan import compile_plan
        from tests.unit._dps_helpers import empty_profile

        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        cfg = {
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "synthetic",
                    "row_count": 10,
                    "generate_columns": [
                        {
                            # tier conditions on state, but state comes later.
                            "name": "tier",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                            "condition_on": "state",
                        },
                        {
                            "name": "state",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                        },
                    ],
                }
            ],
        }
        # The declared-order rule now surfaces at compile time
        # (`check_statistical_columns`, guide step 5/7): `generate_tables`
        # is Plan-only, so a config that can never compile never reaches
        # it. This specific rule is `check_statistical_columns`'s OWN
        # check (not one `load_spec` raises), so it surfaces directly as
        # `PlanCompileError`, not the wrapped `StatisticalSpecError` a
        # `load_spec` failure would raise.
        from decoy_engine.plan import PlanCompileError

        with pytest.raises(PlanCompileError) as exc:
            compile_plan(cfg, empty_profile(), decoy_engine_version="test")
        assert exc.value.code == "statistical_condition_column_unavailable"


class TestCompileCheck:
    """Row 12 (`statistical_columns`): config-only callers reject a bad
    snapshot/config pairing before a run."""

    def _cfg(self, cols: list[dict]) -> dict:
        return {
            "global_settings": {"seed": 1},
            "tables": [{"name": "t", "row_count": 5, "generate_columns": cols}],
        }

    def test_missing_snapshot_rejected_config_only(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        cfg = self._cfg(
            [
                {
                    "name": "amount",
                    "type": "statistical",
                    "snapshot_file": str(tmp_path / "nope.json"),
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_snapshot_unreadable"

    def test_condition_order_rejected_config_only(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        snap = _write_snapshot(tmp_path, _source_df(), joints=[("state", "tier")])
        cfg = self._cfg(
            [
                {
                    "name": "tier",
                    "type": "statistical",
                    "snapshot_file": snap,
                    "allow_real_categories": True,
                    "condition_on": "state",
                },
                {
                    "name": "state",
                    "type": "statistical",
                    "snapshot_file": snap,
                    "allow_real_categories": True,
                },
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_condition_column_unavailable"

    def test_clean_statistical_config_passes(self, tmp_path):
        from decoy_engine import run_config_only_checks

        snap = _write_snapshot(tmp_path, _source_df())
        cfg = self._cfg([{"name": "amount", "type": "statistical", "snapshot_file": snap}])
        assert "statistical_columns" in run_config_only_checks(cfg)

    def test_high_cardinality_without_consent_rejected_config_only(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(40)]})
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        cfg = self._cfg(
            [
                {
                    "name": "code",
                    "type": "statistical",
                    "snapshot_file": str(path),
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_high_cardinality_requires_real_categories"

    def test_high_cardinality_on_non_statistical_column_rejected(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        cfg = self._cfg(
            [{"name": "w", "type": "faker", "faker_type": "word", "high_cardinality": True}]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_high_cardinality_wrong_type"

    def test_high_cardinality_clean_config_passes(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = pd.DataFrame({"code": [f"C{i:03d}" for i in range(40)]})
        snap_dict = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        cfg = self._cfg(
            [
                {
                    "name": "code",
                    "type": "statistical",
                    "snapshot_file": str(path),
                    "high_cardinality": True,
                    "allow_real_categories": True,
                }
            ]
        )
        assert "statistical_columns" in run_config_only_checks(cfg)
