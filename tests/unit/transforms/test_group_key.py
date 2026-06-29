"""Unit tests for group_key strategy (SP-10c TDD).

Written BEFORE the implementation per the TDD contract (testing.md).

group_key derives a deterministic, consistent identifier for every
row sharing the same group_by column value. All rows in the same group
get an identical output; different groups get different outputs.

Config (generate_columns type: group_key):
  group_by  str   Required. Column name whose value defines the group.
                  Rows with the same group_by value share the same
                  derived key.
  length    int   Optional. Output hex-string length (default 16).
                  Must be even and in [8, 64].
  prefix    str   Optional. Constant prefix prepended to the derived
                  key (default "").

Methodology:
  Keyed derivation reuses the engine's HKDF-SHA256 + HMAC-SHA256
  envelope from decoy_engine.determinism._derive.derive() (RFC 5869
  HKDF; RFC 2104 HMAC). The per-group source bytes are the canonical
  UTF-8 encoding of the group_by column value. The namespace is
  "group_key/<column_name>" to keep the derivation isolated from mask
  columns in the same job. This is the same "hash-for-joinability"
  pattern the engine already uses for FK-preserving deterministic
  masking (documented in docs/determinism.md). No custom crypto.

Determinism:
  Same seed + same group_by values -> byte-identical output keys.
"""

from __future__ import annotations

import pytest

from decoy_engine.plan._errors import PlanCompileError


class TestGroupKeyConfig:
    """GroupKeyConfig.from_dict validates group_by + length at parse time."""

    def test_valid_minimal_config_accepted(self) -> None:
        from decoy_engine.transforms.group_key import GroupKeyConfig

        cfg = GroupKeyConfig.from_dict({"group_by": "household_id"})
        assert cfg.group_by == "household_id"
        assert cfg.length == 16
        assert cfg.prefix == ""

    def test_full_config_accepted(self) -> None:
        from decoy_engine.transforms.group_key import GroupKeyConfig

        cfg = GroupKeyConfig.from_dict({"group_by": "cluster_id", "length": 32, "prefix": "HH-"})
        assert cfg.length == 32
        assert cfg.prefix == "HH-"

    def test_missing_group_by_raises(self) -> None:
        from decoy_engine.transforms.group_key import GroupKeyConfig

        with pytest.raises(PlanCompileError, match="group_by"):
            GroupKeyConfig.from_dict({"length": 16})

    def test_odd_length_raises(self) -> None:
        """Length must be even (hex encoding requires even byte count)."""
        from decoy_engine.transforms.group_key import GroupKeyConfig

        with pytest.raises(PlanCompileError, match="length"):
            GroupKeyConfig.from_dict({"group_by": "g", "length": 7})

    def test_length_too_small_raises(self) -> None:
        from decoy_engine.transforms.group_key import GroupKeyConfig

        with pytest.raises(PlanCompileError, match="length"):
            GroupKeyConfig.from_dict({"group_by": "g", "length": 4})

    def test_length_too_large_raises(self) -> None:
        from decoy_engine.transforms.group_key import GroupKeyConfig

        with pytest.raises(PlanCompileError, match="length"):
            GroupKeyConfig.from_dict({"group_by": "g", "length": 80})


class TestGroupKeyConsistency:
    """Rows with the same group_by value receive the same key."""

    def _apply(self, group_vals, group_by="grp", seed=b"\x00" * 8, prefix=""):
        import pandas as pd

        from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key

        df = pd.DataFrame({"grp": group_vals})
        cfg = GroupKeyConfig.from_dict({"group_by": group_by, "prefix": prefix})
        return apply_group_key(cfg, df, seed=seed, namespace=f"group_key/{group_by}")

    def test_same_group_same_key(self) -> None:
        """All rows with group 'A' share one key; all rows with 'B' share another."""
        result = self._apply(["A", "A", "B", "A", "B"])
        assert result[0] == result[1] == result[3], "group A rows should share a key"
        assert result[2] == result[4], "group B rows should share a key"

    def test_different_groups_different_keys(self) -> None:
        """Keys for group 'A' and group 'B' must differ."""
        result = self._apply(["A", "B"])
        assert result[0] != result[1], "Different groups must produce different keys"

    def test_prefix_prepended(self) -> None:
        """Configured prefix appears at the start of every key."""
        result = self._apply(["X", "Y"], prefix="HH-")
        assert all(k.startswith("HH-") for k in result), (
            f"Expected all keys to start with 'HH-', got {result}"
        )

    def test_output_length_matches_config(self) -> None:
        """Keys (minus prefix) are exactly `length` hex characters."""
        import pandas as pd

        from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key

        df = pd.DataFrame({"grp": ["A", "B", "C"]})
        cfg = GroupKeyConfig.from_dict({"group_by": "grp", "length": 24})
        result = apply_group_key(cfg, df, seed=b"\x00" * 8, namespace="group_key/grp")
        for key in result:
            assert len(key) == 24, f"Expected length 24, got {len(key)} for key {key!r}"


class TestGroupKeyDeterminism:
    """Two identical seeded runs produce byte-identical output keys."""

    def _run(self, seed):
        import pandas as pd

        from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key

        df = pd.DataFrame({"grp": ["Alpha", "Beta", "Alpha", "Gamma", "Beta", "Delta"]})
        cfg = GroupKeyConfig.from_dict({"group_by": "grp"})
        return list(apply_group_key(cfg, df, seed=seed, namespace="group_key/grp"))

    def test_same_seed_same_output(self) -> None:
        seed = b"\xca\xfe\xba\xbe\x00\x01\x02\x03"
        r1 = self._run(seed)
        r2 = self._run(seed)
        assert r1 == r2

    def test_different_seeds_differ(self) -> None:
        r1 = self._run(b"\x00" * 8)
        r2 = self._run(b"\x11" * 8)
        # Different seeds feed the HKDF; outputs should differ
        assert r1 != r2


class TestGroupKeyPlanCheck:
    """check_group_key_refs rejects configs with missing group_by columns."""

    def _check(self, config):
        from decoy_engine.plan._checks_group_key import check_group_key_refs

        check_group_key_refs(config)

    def test_valid_config_passes(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "household_id",
                            "type": "categorical",
                            "categories": ["H1", "H2"],
                        },
                        {
                            "name": "household_key",
                            "type": "group_key",
                            "group_by": "household_id",
                        },
                    ],
                }
            ]
        }
        self._check(cfg)  # no raise

    def test_missing_group_by_column_raises(self) -> None:
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "key",
                            "type": "group_key",
                            "group_by": "nonexistent_col",
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="nonexistent_col"):
            self._check(cfg)

    def test_mask_column_bad_group_by_raises(self) -> None:
        """Plan check also covers mask-mode columns with strategy: group_key."""
        cfg = {
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "val", "strategy": "passthrough"},
                        {
                            "name": "key",
                            "strategy": "group_key",
                            "provider_config": {"group_by": "phantom_col"},
                        },
                    ],
                }
            ]
        }
        with pytest.raises(PlanCompileError, match="phantom_col"):
            self._check(cfg)


class TestGroupKeyRegistration:
    """Strategy is registered in SCALAR_HANDLERS."""

    def test_group_key_in_scalar_handlers(self) -> None:
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        assert "group_key" in SCALAR_HANDLERS

    def test_adapter_supports_group_key(self) -> None:
        from decoy_engine.execution import PandasExecutionAdapter

        assert PandasExecutionAdapter().supports_strategy("group_key") is True


class TestGroupKeyGeneratePath:
    """group_key wired into the generate_tables synthesize path."""

    def test_group_key_in_generate_table(self) -> None:
        from decoy_engine.generation.synthesize import generate_tables

        cfg = {
            "version": 1,
            "global_settings": {"seed": 99},
            "sources": {},
            "tables": [
                {
                    "name": "t",
                    "row_count": 6,
                    "generate_columns": [
                        {
                            "name": "cluster",
                            "type": "categorical",
                            "categories": ["X", "Y", "Z"],
                            "weights": [1, 1, 1],
                        },
                        {
                            "name": "cluster_key",
                            "type": "group_key",
                            "group_by": "cluster",
                        },
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
        tbl = generate_tables(cfg)["t"]
        clusters = tbl.column("cluster").to_pylist()
        keys = tbl.column("cluster_key").to_pylist()
        # Build a map: cluster -> set of keys for that cluster
        from collections import defaultdict

        cluster_keys = defaultdict(set)
        for c, k in zip(clusters, keys, strict=True):
            cluster_keys[c].add(k)
        # Each cluster should have exactly one unique key
        for c, ks in cluster_keys.items():
            assert len(ks) == 1, f"Cluster {c!r} has {len(ks)} distinct keys: {ks}"
        # Different clusters should have different keys
        all_keys = [next(iter(ks)) for ks in cluster_keys.values()]
        assert len(set(all_keys)) == len(all_keys), (
            f"Different clusters share a key: {dict(cluster_keys)}"
        )
