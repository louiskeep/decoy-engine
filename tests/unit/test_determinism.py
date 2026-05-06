"""Determinism guarantees for keyed masking transforms (Path B).

These tests pin down the contract documented in the deterministic-masking
plan:

  - Same input + same key → same output, every time
  - Reordering rows doesn't change per-input outputs
  - Different keys → different outputs (no cross-pipeline leak)
  - HMAC-derived helpers are referentially transparent

The legacy (un-keyed) path stays covered by existing strategy tests; this
file exercises only the keyed path that's new in this PR.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Callable

import pandas as pd
import pytest

from decoy_engine.internal.helpers import hmac_hex, hmac_seed
from decoy_engine.transforms.date_shift import DateShiftStrategy
from decoy_engine.transforms.faker_based import FakerStrategy
from decoy_engine.transforms.hash import HashStrategy


# ── helpers ────────────────────────────────────────────────────────────────

def make_derive_key(master: bytes) -> Callable[[str], bytes]:
    """Mimic the platform's HKDF-style resolver with a stable byte derivation
    suitable for tests. Stand-in for HKDF-SHA256(master, info)."""
    def derive(info: str) -> bytes:
        return hmac.new(master, info.encode(), hashlib.sha256).digest()
    return derive


MASTER_A = b"\x00" * 32
MASTER_B = b"\x11" * 32


# ── HMAC primitive ──────────────────────────────────────────────────────────

class TestHmacPrimitives:
    def test_hmac_hex_is_stable(self):
        key = b"k" * 32
        assert hmac_hex(key, "alice@x.com") == hmac_hex(key, "alice@x.com")

    def test_hmac_hex_differs_per_key(self):
        assert hmac_hex(b"k" * 32, "x") != hmac_hex(b"j" * 32, "x")

    def test_hmac_hex_differs_per_value(self):
        key = b"k" * 32
        assert hmac_hex(key, "alice") != hmac_hex(key, "bob")

    def test_hmac_seed_is_stable_int(self):
        key = b"k" * 32
        s = hmac_seed(key, "alice@x.com")
        assert isinstance(s, int)
        assert 0 <= s < 2**32
        assert hmac_seed(key, "alice@x.com") == s

    def test_hmac_hex_handles_none(self):
        assert hmac_hex(b"k" * 32, None) is None

    def test_hmac_seed_handles_none(self):
        assert hmac_seed(b"k" * 32, None) == 0


# ── hash strategy (keyed path) ──────────────────────────────────────────────

class TestKeyedHash:
    def setup_method(self):
        self.derive = make_derive_key(MASTER_A)
        self.strategy = HashStrategy(derive_key=self.derive)
        self.col = pd.Series(["alice@x.com", "bob@y.com", None, "alice@x.com"])
        self.rule = {"column": "email", "type": "hash"}

    def test_same_input_same_output_within_run(self):
        out = self.strategy.apply(self.col, self.rule)
        # Index 0 and 3 are the same input → must be the same output.
        assert out.iloc[0] == out.iloc[3]
        # Different input → different output.
        assert out.iloc[0] != out.iloc[1]

    def test_repeat_runs_produce_identical_output(self):
        out1 = self.strategy.apply(self.col, self.rule)
        out2 = HashStrategy(derive_key=self.derive).apply(self.col, self.rule)
        pd.testing.assert_series_equal(out1, out2)

    def test_row_reorder_preserves_per_input_output(self):
        shuffled = self.col.iloc[[2, 0, 3, 1]].reset_index(drop=True)
        out_orig = self.strategy.apply(self.col, self.rule)
        out_shuffled = HashStrategy(derive_key=self.derive).apply(shuffled, self.rule)
        # Pull alice's hash from each — must match.
        alice_orig = out_orig.iloc[0]
        alice_shuffled = out_shuffled.iloc[1]   # alice is now at index 1
        assert alice_orig == alice_shuffled

    def test_different_master_key_yields_different_output(self):
        out_a = self.strategy.apply(self.col, self.rule)
        out_b = HashStrategy(derive_key=make_derive_key(MASTER_B)).apply(
            self.col, self.rule
        )
        assert out_a.iloc[0] != out_b.iloc[0]

    def test_same_value_yields_same_output_across_column_names(self):
        # Pre-2026-05 behavior was per-column-name keyed: two columns with
        # different names produced different hashes for the same input
        # value. We dropped that: mask key derivation is master-only, so
        # FK joins survive masking even when the column names differ
        # (e.g. customers.email_addr vs vendors.contact_email).
        out_email = self.strategy.apply(
            pd.Series(["alice@x.com"]), {"column": "email", "type": "hash"}
        )
        out_other = self.strategy.apply(
            pd.Series(["alice@x.com"]), {"column": "contact_email", "type": "hash"}
        )
        assert out_email.iloc[0] == out_other.iloc[0]

    def test_legacy_path_still_works_when_no_derive_key(self):
        legacy = HashStrategy()  # no derive_key
        out = legacy.apply(self.col, self.rule)
        assert out.iloc[0] == out.iloc[3]   # still per-input deterministic


# ── faker strategy (keyed path: stateless, bitwise stable) ─────────────────

class TestKeyedFaker:
    def setup_method(self):
        self.derive = make_derive_key(MASTER_A)
        self.col = pd.Series(["alice@x.com", "bob@y.com", "alice@x.com"])
        self.rule = {"column": "email", "type": "faker", "faker_type": "email"}

    def test_same_input_same_fake_within_run(self):
        out = FakerStrategy(derive_key=self.derive).apply(self.col, self.rule)
        assert out.iloc[0] == out.iloc[2]
        assert out.iloc[0] != out.iloc[1]

    def test_bitwise_stable_across_runs(self):
        out1 = FakerStrategy(derive_key=self.derive).apply(self.col, self.rule)
        out2 = FakerStrategy(derive_key=self.derive).apply(self.col, self.rule)
        pd.testing.assert_series_equal(out1, out2)

    def test_row_order_does_not_affect_per_input_output(self):
        # The legacy faker path was row-order dependent; the keyed path is not.
        forward = pd.Series(["alice@x.com", "bob@y.com", "carol@z.com"])
        reverse = pd.Series(["carol@z.com", "bob@y.com", "alice@x.com"])
        out_f = FakerStrategy(derive_key=self.derive).apply(forward, self.rule)
        out_r = FakerStrategy(derive_key=self.derive).apply(reverse, self.rule)
        # Bob is the middle row in both — must produce the same fake email.
        assert out_f.iloc[1] == out_r.iloc[1]
        # Alice → first row forward, last row reverse.
        assert out_f.iloc[0] == out_r.iloc[2]

    def test_different_keys_yield_different_fakes(self):
        out_a = FakerStrategy(derive_key=make_derive_key(MASTER_A)).apply(
            self.col, self.rule
        )
        out_b = FakerStrategy(derive_key=make_derive_key(MASTER_B)).apply(
            self.col, self.rule
        )
        assert out_a.iloc[0] != out_b.iloc[0]

    def test_preserve_domain_works_keyed(self):
        rule = {**self.rule, "preserve_domain": True}
        out = FakerStrategy(derive_key=self.derive).apply(self.col, rule)
        # Bob's fake email keeps the @y.com domain.
        assert out.iloc[1].endswith("@y.com")

    def test_legacy_faker_still_works_when_no_derive_key(self):
        legacy = FakerStrategy()
        out = legacy.apply(self.col, self.rule)
        # Within a single run, alice maps consistently.
        assert out.iloc[0] == out.iloc[2]


# ── date_shift (keyed path) ────────────────────────────────────────────────

class TestKeyedDateShift:
    def setup_method(self):
        self.derive = make_derive_key(MASTER_A)
        self.col = pd.Series(["1985-03-15", "1990-07-22", "1985-03-15"])
        self.rule = {
            "column": "dob", "type": "date_shift",
            "min_days": -365, "max_days": 365,
        }

    def test_same_date_same_shift_within_run(self):
        out = DateShiftStrategy(derive_key=self.derive).apply(self.col, self.rule)
        assert out.iloc[0] == out.iloc[2]    # same input → same shifted output

    def test_keyed_shift_stable_across_runs(self):
        out1 = DateShiftStrategy(derive_key=self.derive).apply(self.col, self.rule)
        out2 = DateShiftStrategy(derive_key=self.derive).apply(self.col, self.rule)
        pd.testing.assert_series_equal(out1, out2)

    def test_different_keys_yield_different_shifts(self):
        out_a = DateShiftStrategy(derive_key=make_derive_key(MASTER_A)).apply(
            self.col, self.rule
        )
        out_b = DateShiftStrategy(derive_key=make_derive_key(MASTER_B)).apply(
            self.col, self.rule
        )
        # At least one of the three shifted dates differs.
        assert any(out_a.iloc[i] != out_b.iloc[i] for i in range(len(self.col)))

    def test_legacy_md5_path_still_per_input_deterministic(self):
        legacy = DateShiftStrategy()  # no derive_key → MD5 path
        out1 = legacy.apply(self.col, self.rule)
        out2 = DateShiftStrategy().apply(self.col, self.rule)
        pd.testing.assert_series_equal(out1, out2)


# ── cross-strategy: foreign-key integrity ──────────────────────────────────

class TestForeignKeyIntegrity:
    """The whole point of mask key derivation being master-only: any value
    masks identically across every column, every table, every pipeline on
    the instance. FK joins survive masking by default, no per-column tags
    or namespaces required.
    """

    def test_same_column_name_across_tables_masks_identically(self):
        derive = make_derive_key(MASTER_A)
        rule = {"column": "email", "type": "hash"}
        customers_email = pd.Series(["alice@x.com"])
        orders_email = pd.Series(["alice@x.com"])
        out_c = HashStrategy(derive_key=derive).apply(customers_email, rule)
        out_o = HashStrategy(derive_key=derive).apply(orders_email, rule)
        assert out_c.iloc[0] == out_o.iloc[0]

    def test_different_column_names_still_mask_identically(self):
        # The simplification this PR landed: column-name no longer enters
        # the derivation. customers.email_addr and vendors.contact_email
        # both hash "alice@x.com" to the same bytes.
        derive = make_derive_key(MASTER_A)
        out_a = HashStrategy(derive_key=derive).apply(
            pd.Series(["alice@x.com"]), {"column": "email_addr", "type": "hash"}
        )
        out_b = HashStrategy(derive_key=derive).apply(
            pd.Series(["alice@x.com"]), {"column": "contact_email", "type": "hash"}
        )
        assert out_a.iloc[0] == out_b.iloc[0]

    def test_faker_and_date_shift_also_master_only(self):
        # The other two keyed mask strategies follow the same rule. Same
        # input across differently-named columns → same output.
        derive = make_derive_key(MASTER_A)
        out_f1 = FakerStrategy(derive_key=derive).apply(
            pd.Series(["alice@x.com"]),
            {"column": "email", "type": "faker", "faker_type": "email"},
        )
        out_f2 = FakerStrategy(derive_key=derive).apply(
            pd.Series(["alice@x.com"]),
            {"column": "contact", "type": "faker", "faker_type": "email"},
        )
        assert out_f1.iloc[0] == out_f2.iloc[0]

        out_d1 = DateShiftStrategy(derive_key=derive).apply(
            pd.Series(["1985-03-15"]),
            {"column": "dob", "type": "date_shift", "min_days": -10, "max_days": 10},
        )
        out_d2 = DateShiftStrategy(derive_key=derive).apply(
            pd.Series(["1985-03-15"]),
            {"column": "birthday", "type": "date_shift", "min_days": -10, "max_days": 10},
        )
        assert out_d1.iloc[0] == out_d2.iloc[0]


# ── make_key_resolver: the public helper CLI + platform both use ───────────

class TestMakeKeyResolver:
    """The bytes produced by ``make_key_resolver`` MUST be reproducible across
    callers. CLI passes a master from ``--master-key``; platform pulls it
    from env / file / KMS; both call this helper. Drift here means a CLI
    run and a platform run with the same key produce different masked
    output — silent breakage of the recovery property.
    """

    def test_resolver_returns_32_bytes(self):
        from decoy_engine import make_key_resolver
        resolver = make_key_resolver(MASTER_A, "customers_q4")
        out = resolver("col:email")
        assert len(out) == 32

    def test_same_master_same_label_same_info_yields_same_bytes(self):
        from decoy_engine import make_key_resolver
        a = make_key_resolver(MASTER_A, "customers_q4")
        b = make_key_resolver(MASTER_A, "customers_q4")
        assert a("col:email") == b("col:email")
        assert a("col:phone") == b("col:phone")

    def test_different_label_yields_different_bytes(self):
        from decoy_engine import make_key_resolver
        a = make_key_resolver(MASTER_A, "customers_q4")
        b = make_key_resolver(MASTER_A, "orders_q4")
        assert a("col:email") != b("col:email")

    def test_different_master_yields_different_bytes(self):
        from decoy_engine import make_key_resolver
        a = make_key_resolver(MASTER_A, "customers_q4")
        b = make_key_resolver(MASTER_B, "customers_q4")
        assert a("col:email") != b("col:email")

    def test_rejects_wrong_master_length(self):
        from decoy_engine import make_key_resolver
        with pytest.raises(ValueError, match="32 bytes"):
            make_key_resolver(b"short", "label")
