"""D7c: attack-based metrics contract + opt-in extras gate tests.

Sprint requirement (verbatim): "Optional attack-based metrics only
through approved extras." This file pins the security-critical
half of that contract:

  - attacks NEVER auto-run; the caller must opt in explicitly
  - missing extras package -> 'extras_not_installed' block, not an
    exception, and definitely not a silent omission
  - the extras module's failure must NOT take down the rest of the
    SynthReport assembly
  - the SynthReport's disclaimer block tells the operator that
    'no attack block != survived attack', so audit reviewers can
    tell apart 'we ran attacks and they failed' from 'we never
    ran attacks'

Tests use sys.modules monkeypatching to simulate an installed
extras package without needing one to actually exist on disk.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from decoy_engine.quality.synth_report import (
    SYNTH_REPORT_SCHEMA_VERSION,
    assemble_synth_report,
    compute_attack_metrics,
)


# ── default-off contract ──────────────────────────────────────────────────


class TestDefaultOff:
    def test_default_call_returns_not_enabled_block(self):
        """No enable_attacks flag -> never runs attacks, even if
        the extras module happens to be importable."""
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [4, 5, 6]})
        result = compute_attack_metrics(source, synth)
        assert result["available"] is False
        assert result["reason"] == "not_enabled_by_caller"

    def test_default_call_does_not_import_extras_module(self, monkeypatch):
        """When enable_attacks=False the implementation must NOT
        even try to import the extras package. We assert this by
        monkeypatching the importer to raise if called."""
        called = {"count": 0}
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fail_on_extras(name, *args, **kwargs):
            if name == "decoy_engine_privacy_attacks":
                called["count"] += 1
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fail_on_extras)
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [4, 5, 6]})
        compute_attack_metrics(source, synth)
        assert called["count"] == 0, (
            "extras module was imported even though enable_attacks=False"
        )


# ── opt-in but missing ────────────────────────────────────────────────────


class TestExtrasMissing:
    def test_enabled_without_extras_installed(self):
        """enable_attacks=True but extras module absent -> the
        unavailable block records the right reason."""
        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [4, 5, 6]})
        # Use a name that definitely is not installed.
        result = compute_attack_metrics(
            source, synth,
            enable_attacks=True,
            extras_module="definitely_not_installed_pkg_xyz",
        )
        assert result["available"] is False
        assert result["reason"] == "extras_not_installed"
        assert result["extras_module"] == "definitely_not_installed_pkg_xyz"


# ── opt-in WITH extras: contract dispatch ─────────────────────────────────


class TestExtrasInstalled:
    """Stub an importable module via sys.modules so we don't need a
    real extras package on disk."""

    def _install_stub(self, monkeypatch, name, run_fn):
        mod = types.ModuleType(name)
        if run_fn is not None:
            mod.run_privacy_attacks = run_fn
        monkeypatch.setitem(sys.modules, name, mod)

    def test_enabled_with_stub_runs_extras_and_returns_results(self, monkeypatch):
        captured = {"called": False}
        def fake_run(source, output, *, holdout=None):
            captured["called"] = True
            captured["holdout"] = holdout
            return {"mia_auc": 0.55, "shadow_auc": 0.51}
        self._install_stub(monkeypatch, "fake_attacks_pkg", fake_run)

        source = pd.DataFrame({"v": [1, 2, 3]})
        synth = pd.DataFrame({"v": [4, 5, 6]})
        result = compute_attack_metrics(
            source, synth,
            enable_attacks=True,
            extras_module="fake_attacks_pkg",
        )
        assert result["available"] is True
        assert result["results"] == {"mia_auc": 0.55, "shadow_auc": 0.51}
        assert captured["called"] is True

    def test_holdout_pass_through(self, monkeypatch):
        """The extras module receives the holdout frame so it can
        run MIA-style attacks without re-resolving holdout itself."""
        captured = {}
        def fake_run(source, output, *, holdout=None):
            captured["holdout_len"] = len(holdout) if holdout is not None else None
            return {"x": 1}
        self._install_stub(monkeypatch, "fake_attacks_pkg_2", fake_run)

        holdout = pd.DataFrame({"v": [7, 8, 9, 10]})
        compute_attack_metrics(
            pd.DataFrame({"v": [1, 2]}),
            pd.DataFrame({"v": [3, 4]}),
            enable_attacks=True,
            extras_module="fake_attacks_pkg_2",
            holdout=holdout,
        )
        assert captured["holdout_len"] == 4

    def test_extras_module_missing_entry_point(self, monkeypatch):
        """Module imports but doesn't define run_privacy_attacks ->
        explicit reason rather than AttributeError surface."""
        self._install_stub(monkeypatch, "fake_attacks_no_entry", None)
        result = compute_attack_metrics(
            pd.DataFrame({"v": [1]}),
            pd.DataFrame({"v": [2]}),
            enable_attacks=True,
            extras_module="fake_attacks_no_entry",
        )
        assert result["available"] is False
        assert result["reason"] == "extras_module_missing_entry_point"

    def test_extras_runtime_error_does_not_propagate(self, monkeypatch):
        """The extras module raising must NOT propagate. The SynthReport
        assembly must keep going; the attacks block records the failure."""
        def boom(source, output, *, holdout=None):
            raise RuntimeError("simulated extras crash")
        self._install_stub(monkeypatch, "fake_attacks_crash", boom)
        result = compute_attack_metrics(
            pd.DataFrame({"v": [1]}),
            pd.DataFrame({"v": [2]}),
            enable_attacks=True,
            extras_module="fake_attacks_crash",
        )
        assert result["available"] is False
        assert result["reason"] == "extras_runtime_error"
        assert result["error_type"] == "RuntimeError"

    def test_extras_runtime_error_does_not_leak_exception_message(
        self, monkeypatch,
    ):
        """The raw exception message can carry sensitive bits (file
        paths, data values). Only the type name is preserved."""
        def boom(source, output, *, holdout=None):
            raise RuntimeError("/etc/secrets/api_key=XXX leaked")
        self._install_stub(monkeypatch, "fake_attacks_leak", boom)
        result = compute_attack_metrics(
            pd.DataFrame({"v": [1]}),
            pd.DataFrame({"v": [2]}),
            enable_attacks=True,
            extras_module="fake_attacks_leak",
        )
        flat = str(result)
        assert "api_key" not in flat
        assert "XXX" not in flat


# ── disclaimer + assemble wiring ──────────────────────────────────────────


class TestAttacksDisclaimer:
    def test_disclaimer_explains_no_attempt_vs_survived(self):
        """The synth-report disclaimer must spell out the difference
        between 'no attack block' and 'survived an attack'."""
        report = assemble_synth_report(new_row_synthesis=None)
        joined = " ".join(report["disclaimers"]).lower()
        # Both halves must be present.
        assert "off by default" in joined
        assert "no attack was attempted" in joined or "no attack was attempt" in joined

    def test_assemble_accepts_attacks_block(self):
        attacks_block = {
            "metric": "attack_based_metrics",
            "available": True,
            "results": {"mia_auc": 0.5},
            "extras_module": "fake",
        }
        report = assemble_synth_report(
            new_row_synthesis=None,
            attacks=attacks_block,
        )
        assert report["attacks"]["available"] is True
        assert report["attacks"]["results"] == {"mia_auc": 0.5}

    def test_assemble_default_attacks_none(self):
        report = assemble_synth_report(new_row_synthesis=None)
        assert report["attacks"] is None

    def test_full_report_with_attacks_round_trips_json(self):
        import json
        attacks = compute_attack_metrics(
            pd.DataFrame({"v": [1, 2]}),
            pd.DataFrame({"v": [3, 4]}),
        )  # default-off path produces a serializable block
        report = assemble_synth_report(
            new_row_synthesis=None,
            attacks=attacks,
            job_id=99,
        )
        decoded = json.loads(json.dumps(report))
        assert decoded["schema_version"] == SYNTH_REPORT_SCHEMA_VERSION
        assert decoded["attacks"]["reason"] == "not_enabled_by_caller"


# ── public surface ────────────────────────────────────────────────────────


class TestExports:
    def test_quality_package_exports_compute_attack_metrics(self):
        from decoy_engine import quality
        assert hasattr(quality, "compute_attack_metrics")
