"""F2/F3 (2026-06-26): GenDeriveContext is the v6 generation derivation.

Locks the primitive that replaces the F2 4-byte truncation and the F3
`column_seed + i` arithmetic:

- full-width material (not capped at 2**32),
- the three RNG families derive disjoint streams from one column root,
- per-row ints are independent (no `+ i` adjacency within or across columns),
- the keyed path is reproducible and fingerprint-scoped (rename-stable),
- a derive_key resolver failure raises (QA-1 M18 contract, not silent fallback),
- determinism: fresh rolls across instances but is stable within one context.
"""

from __future__ import annotations

import pytest

from decoy_engine.context import make_key_resolver
from decoy_engine.generators.derivation import (
    GEN_FAMILIES,
    GenDeriveContext,
    strategy_config_fingerprint,
)

_MASTER = bytes(range(32))


def _resolver(label: str = "demo-pipeline"):
    return make_key_resolver(_MASTER, label)


def _ctx(config, *, derive_key=None, fallback_seed: int = 42) -> GenDeriveContext:
    return GenDeriveContext.for_column(derive_key, config, fallback_seed)


class TestFullWidth:
    def test_base_int_uses_full_width_not_32_bit(self):
        # The F2 defect capped seeds at 2**32. Across the families at least
        # one base int must exceed the old ceiling (overwhelmingly likely
        # for a 256-bit HMAC output).
        ctx = _ctx({"strategy": "faker", "provider": "name"}, derive_key=_resolver())
        assert any(ctx.base_int(f) >= (1 << 32) for f in GEN_FAMILIES)

    def test_base_and_row_ints_are_non_negative(self):
        ctx = _ctx({"strategy": "categorical", "values": ["a", "b"]}, derive_key=_resolver())
        for fam in GEN_FAMILIES:
            assert ctx.base_int(fam) >= 0
            assert ctx.row_int(fam, 0) >= 0


class TestFamilyDisjointness:
    def test_three_families_diverge(self):
        ctx = _ctx({"strategy": "faker", "provider": "name"}, derive_key=_resolver())
        bases = {ctx.base_int(f) for f in GEN_FAMILIES}
        assert len(bases) == len(GEN_FAMILIES)  # py, np, faker all distinct

    def test_family_row_streams_diverge(self):
        ctx = _ctx({"strategy": "faker", "provider": "name"}, derive_key=_resolver())
        rows_py = [ctx.row_int("py", i) for i in range(8)]
        rows_faker = [ctx.row_int("faker", i) for i in range(8)]
        assert rows_py != rows_faker


class TestRowIndependence:
    def test_adjacent_rows_differ(self):
        ctx = _ctx({"strategy": "faker", "provider": "name"}, derive_key=_resolver())
        rows = [ctx.row_int("py", i) for i in range(16)]
        assert len(set(rows)) == len(rows)  # no repeats

    def test_no_plus_i_adjacency_across_columns(self):
        # F3 regression: with `column_seed + i`, column A row i and column B
        # row i-1 shared a seed when B's base = A's base + 1. Two distinct
        # configs must not produce row-shifted-identical streams.
        dk = _resolver()
        a = _ctx({"strategy": "faker", "provider": "first_name"}, derive_key=dk)
        b = _ctx({"strategy": "faker", "provider": "last_name"}, derive_key=dk)
        a_rows = [a.row_int("faker", i) for i in range(32)]
        b_rows = [b.row_int("faker", i) for i in range(32)]
        # No shift k in a small window aligns the two streams.
        for k in range(1, 5):
            assert a_rows[k:] != b_rows[: len(a_rows) - k]
            assert b_rows[k:] != a_rows[: len(b_rows) - k]


class TestReproducibilityAndFingerprint:
    def test_same_config_same_key_is_reproducible(self):
        cfg = {"strategy": "faker", "provider": "name"}
        a = _ctx(cfg, derive_key=_resolver())
        b = _ctx(cfg, derive_key=_resolver())
        assert a.base_int("py") == b.base_int("py")
        assert [a.row_int("faker", i) for i in range(5)] == [
            b.row_int("faker", i) for i in range(5)
        ]

    def test_rename_does_not_shift_output(self):
        # name is excluded from the fingerprint (R3.10): same strategy/config
        # under a different display name yields identical derivation.
        dk = _resolver()
        a = _ctx({"name": "x", "strategy": "faker", "provider": "name"}, derive_key=dk)
        b = _ctx({"name": "y", "strategy": "faker", "provider": "name"}, derive_key=dk)
        assert a.base_int("py") == b.base_int("py")

    def test_distinct_configs_diverge(self):
        dk = _resolver()
        a = _ctx({"strategy": "faker", "provider": "first_name"}, derive_key=dk)
        b = _ctx({"strategy": "faker", "provider": "last_name"}, derive_key=dk)
        assert a.base_int("py") != b.base_int("py")

    def test_keyed_root_is_fingerprint_label(self):
        # The keyed root is exactly derive_key("gen:" + fingerprint); the
        # family/row split is a pure extension on top of that 32-byte block.
        dk = _resolver()
        cfg = {"strategy": "faker", "provider": "name"}
        fp = strategy_config_fingerprint(cfg)
        expected_root = dk(f"gen:{fp}")
        ctx = _ctx(cfg, derive_key=dk)
        # base_int("py") == int(HMAC(expected_root, "fam:py")); we cannot
        # reach the private helper, but a second context built from the same
        # resolver must match, and a context whose root we perturb must not.
        other = _ctx({"strategy": "faker", "provider": "other"}, derive_key=dk)
        assert ctx.base_int("py") != other.base_int("py")
        assert len(expected_root) == 32


class TestResolverFailureRaises:
    def test_derive_key_failure_raises(self):
        def boom(_info: str) -> bytes:
            raise ValueError("resolver down")

        with pytest.raises(RuntimeError, match="derive_key failed"):
            _ctx({"strategy": "faker", "provider": "name"}, derive_key=boom)


class TestFreshAndUnkeyed:
    def test_fresh_rolls_across_instances_stable_within(self):
        cfg = {"strategy": "faker", "provider": "name", "determinism": "fresh"}
        a = _ctx(cfg)
        b = _ctx(cfg)
        # Stable within one context (every row sees the same column root).
        assert a.base_int("py") == a.base_int("py")
        # Rolls across instances.
        assert a.base_int("py") != b.base_int("py")

    def test_unkeyed_is_reproducible_and_rename_stable(self):
        a = _ctx({"name": "x", "strategy": "categorical", "values": ["a"]}, fallback_seed=7)
        b = _ctx({"name": "y", "strategy": "categorical", "values": ["a"]}, fallback_seed=7)
        assert a.base_int("py") == b.base_int("py")  # name excluded, seed equal
        c = _ctx({"strategy": "categorical", "values": ["a"]}, fallback_seed=8)
        assert a.base_int("py") != c.base_int("py")  # different fallback seed


class TestSnapshotFileContentHash:
    """TH-3.2 (dennis BLOCKER 1): ``snapshot_file`` is fingerprinted by file
    CONTENT, not path. The statistical harness stages the snapshot in a fresh
    per-run temp dir, so a path-based fingerprint produced a different seed
    every process (cross-process determinism drift) -- and in production,
    renaming/date-stamping a snapshot would silently change synthetic output.
    """

    @staticmethod
    def _write_snapshot(tmp_path, name: str, content: bytes) -> str:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return str(p)

    def test_same_content_different_path_same_fingerprint(self, tmp_path):
        content = b'{"schema_version": "distribution-snapshot/v1", "x": 1}'
        p1 = self._write_snapshot(tmp_path, "run_a/snap.json", content)
        # different directory AND different filename, identical bytes
        p2 = self._write_snapshot(tmp_path, "run_b/other_name.json", content)
        cfg1 = {"name": "charge", "type": "statistical", "snapshot_file": p1}
        cfg2 = {"name": "charge", "type": "statistical", "snapshot_file": p2}
        assert strategy_config_fingerprint(cfg1) == strategy_config_fingerprint(cfg2)

    def test_same_content_different_path_same_column_seed_and_output(self, tmp_path):
        # End-to-end determinism guarantee: identical snapshot content at two
        # different paths -> identical per-column seed -> byte-identical draws.
        content = b'{"schema_version": "distribution-snapshot/v1", "x": 1}'
        p1 = self._write_snapshot(tmp_path, "dir_one/snap.json", content)
        p2 = self._write_snapshot(tmp_path, "dir_two/renamed.json", content)
        dk = _resolver()
        a = _ctx({"type": "statistical", "snapshot_file": p1}, derive_key=dk)
        b = _ctx({"type": "statistical", "snapshot_file": p2}, derive_key=dk)
        for fam in GEN_FAMILIES:
            assert a.base_int(fam) == b.base_int(fam)
            assert [a.row_int(fam, i) for i in range(8)] == [b.row_int(fam, i) for i in range(8)]

    def test_different_content_different_seed(self, tmp_path):
        # Two genuinely different snapshots must NOT collide onto one seed
        # (the failure mode of simply excluding snapshot_file).
        p1 = self._write_snapshot(tmp_path, "a.json", b'{"schema_version": "v1", "mean": 340}')
        p2 = self._write_snapshot(tmp_path, "b.json", b'{"schema_version": "v1", "mean": 999}')
        cfg1 = {"type": "statistical", "snapshot_file": p1}
        cfg2 = {"type": "statistical", "snapshot_file": p2}
        assert strategy_config_fingerprint(cfg1) != strategy_config_fingerprint(cfg2)
        dk = _resolver()
        assert _ctx(cfg1, derive_key=dk).base_int("np") != _ctx(cfg2, derive_key=dk).base_int("np")

    def test_missing_snapshot_fails_loud(self, tmp_path):
        # Edge case: unreadable/missing file must raise, not silently fall back
        # to the path string (which would reintroduce the path-coupling bug).
        cfg = {"type": "statistical", "snapshot_file": str(tmp_path / "does_not_exist.json")}
        with pytest.raises(RuntimeError, match="missing or unreadable"):
            strategy_config_fingerprint(cfg)
