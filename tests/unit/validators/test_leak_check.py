"""S2 (Sprint 2 honesty pack): leak_check, the load-bearing validator.

TDD: written before the implementation, per the guide's D3 + S2 acceptance
list. Covers: real-leak detection, cell-tier partial leaks, the
false-positive regression pin over legitimately-masked data, column-tier
boundary detection + exempt, when/FK-child/passthrough exclusion, the
missing-source fail-loud rule, the drift sentry, and no-values-in-findings.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.validators import validate
from decoy_engine.validators._leak_check import (
    _COMPOSITE_PROVIDERS,
    EXCLUDED,
    SHAPE_PRESERVING,
    TRANSFORMATIVE,
    validate_leak_check,
)


def _cfg(
    tables: list[dict[str, Any]],
    *,
    lc_columns: dict[str, list[str]] | None = None,
    lc_params: dict[str, Any] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": "leak_check"}
    if lc_columns is not None:
        entry["columns"] = lc_columns
    if lc_params is not None:
        entry["params"] = lc_params
    return {
        "validators": [entry],
        "tables": tables,
        "relationships": relationships or [],
    }


class TestDriftSentry:
    """Every registered scalar handler is classified in exactly one class."""

    def test_every_handler_classified_exactly_once(self) -> None:
        from decoy_engine.execution._strategies import SCALAR_HANDLERS

        names = set(SCALAR_HANDLERS)
        classified = TRANSFORMATIVE | SHAPE_PRESERVING | EXCLUDED
        assert names <= classified, f"unclassified handlers: {names - classified}"
        overlaps = (
            (TRANSFORMATIVE & SHAPE_PRESERVING)
            | (TRANSFORMATIVE & EXCLUDED)
            | (SHAPE_PRESERVING & EXCLUDED)
        )
        assert overlaps == set(), f"handlers classified in more than one class: {overlaps}"

    def test_fake_unclassified_handler_is_not_silently_accepted(self) -> None:
        """A hypothetical new handler name must not already be classified."""
        assert "totally_new_strategy_xyz" not in (TRANSFORMATIVE | SHAPE_PRESERVING | EXCLUDED)

    def test_composite_providers_match_registry(self) -> None:
        """MEDIUM M1 (dennis review 2026-07-04): the composite drift ratchet.

        `_COMPOSITE_PROVIDERS` is a hardcoded mirror of the composite
        dispatch. Assert it equals the ACTUAL set of composite providers in
        the registry (every provider whose capability backend_type is
        "composite" -- the same signal `build_work_list` uses to route a
        node to the composite handler). A future composite provider added
        without updating `_COMPOSITE_PROVIDERS` would otherwise fall into
        leak_check's defensive branch unchecked; this fails the build first.
        """
        from decoy_engine.providers_v2 import get_default_registry

        reg = get_default_registry()
        actual_composites = {
            name
            for name in reg.known_providers()
            if reg.get_capabilities(name).backend_type == "composite"
        }
        assert actual_composites == _COMPOSITE_PROVIDERS, (
            "leak_check._COMPOSITE_PROVIDERS is out of sync with the registry's "
            f"composite providers. Missing: {actual_composites - _COMPOSITE_PROVIDERS}; "
            f"stale: {_COMPOSITE_PROVIDERS - actual_composites}"
        )


class TestRealLeakDetection:
    def test_no_op_handler_raises_validator_failed(self) -> None:
        """A declared-sensitive column whose handler is a no-op is caught."""
        outputs = {"t": pa.table({"ssn": ["111-22-3333", "222-33-4444", "333-44-5555"]})}
        sources = {"t": pa.table({"ssn": ["111-22-3333", "222-33-4444", "333-44-5555"]})}
        config = _cfg(
            [{"name": "t", "columns": [{"name": "ssn", "strategy": "hash"}]}],
        )
        report = validate(outputs, config, sources=sources)
        assert report.passed is False
        finding = next(f for f in report.findings if f.column == "ssn")
        assert finding.table == "t"
        assert "1.00" in finding.detail or "ratio 1.0" in finding.detail


class TestCellTierPartialLeak:
    def test_ten_percent_identical_fires_at_default_threshold(self) -> None:
        src = ["v" + str(i) for i in range(10)]
        # Row 0 leaks (identical); rest masked (append suffix).
        out = [src[0]] + [v + "_masked" for v in src[1:]]
        outputs = {"t": pa.table({"col": out})}
        sources = {"t": pa.table({"col": src})}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "hash"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is False
        finding = report.findings[0]
        assert finding.failing_row_indices == (0,)

    def test_one_percent_identical_passes_at_default_threshold(self) -> None:
        src = ["v" + str(i) for i in range(100)]
        out = [src[0]] + [v + "_masked" for v in src[1:]]  # 1/100 = 1%
        outputs = {"t": pa.table({"col": out})}
        sources = {"t": pa.table({"col": src})}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "hash"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is True


class TestNoFalsePositiveOnLegitimateMasking:
    def test_coincidence_prone_fixture_passes(self) -> None:
        """hash/fpe (cell-tier, TRANSFORMATIVE): a handful of coincidental
        collisions among 100 rows stays under the 2% default threshold.
        bucketize/truncate (column-tier only, SHAPE_PRESERVING): SOME rows
        legitimately land on a bucket boundary / stay short enough that
        truncate is a no-op, but the column as a WHOLE is not 100% identical
        (ratio < 1.0), so no column-tier finding fires either. This is the
        false-positive regression pin: none of these legitimate coincidences
        should be flagged.
        """
        n = 100
        # hash: no coincidental collisions.
        h_src = [f"person_{i}" for i in range(n)]
        h_out = [f"h_masked_{i}" for i in range(n)]
        # fpe_digits: 1 fixed point in 100 (1%), under the 2% default.
        fpe_src = [f"{i:04d}" for i in range(n)]
        fpe_out = [f"{i:04d}" if i == 0 else f"{(i + 7777) % 10000:04d}" for i in range(n)]
        # bucket: every 10th row happens to land on a bucket boundary
        # (legitimate coincidence); the rest genuinely change. Ratio < 1.0.
        bucket_src = [str(i) for i in range(n)]
        bucket_out = [str((i // 10) * 10) for i in range(n)]
        # short: half the values are short enough that truncate(6) is a
        # no-op; the other half are long and genuinely truncated.
        short_src = [("ab" if i % 2 == 0 else f"long_value_{i}") for i in range(n)]
        short_out = [(v if i % 2 == 0 else v[:6]) for i, v in enumerate(short_src)]

        outputs = {
            "t": pa.table(
                {"h": h_out, "fpe_digits": fpe_out, "bucket": bucket_out, "short": short_out}
            )
        }
        sources = {
            "t": pa.table(
                {"h": h_src, "fpe_digits": fpe_src, "bucket": bucket_src, "short": short_src}
            )
        }
        config = _cfg(
            [
                {
                    "name": "t",
                    "columns": [
                        {"name": "h", "strategy": "hash"},
                        {"name": "fpe_digits", "strategy": "fpe"},
                        {"name": "bucket", "strategy": "bucketize"},
                        {"name": "short", "strategy": "truncate"},
                    ],
                }
            ]
        )
        report = validate(outputs, config, sources=sources)
        assert report.passed is True, report.findings


class TestColumnTierBoundaryAndExempt:
    def test_all_boundary_bucketize_column_flagged(self) -> None:
        outputs = {"t": pa.table({"b": ["0", "10", "20"]})}
        sources = {"t": pa.table({"b": ["0", "10", "20"]})}  # every value already on boundary
        config = _cfg([{"name": "t", "columns": [{"name": "b", "strategy": "bucketize"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is False
        assert report.findings[0].column == "b"

    def test_exempt_knob_suppresses_the_finding(self) -> None:
        outputs = {"t": pa.table({"b": ["0", "10", "20"]})}
        sources = {"t": pa.table({"b": ["0", "10", "20"]})}
        config = _cfg(
            [{"name": "t", "columns": [{"name": "b", "strategy": "bucketize"}]}],
            lc_params={"exempt": {"t": ["b"]}},
        )
        report = validate(outputs, config, sources=sources)
        assert report.passed is True


class TestExclusions:
    def test_when_bearing_column_no_findings(self) -> None:
        outputs = {"t": pa.table({"col": ["alice", "bob"]})}
        sources = {"t": pa.table({"col": ["alice", "bob"]})}
        config = _cfg(
            [{"name": "t", "columns": [{"name": "col", "strategy": "hash", "when": "x > 1"}]}]
        )
        report = validate(outputs, config, sources=sources)
        assert report.passed is True

    def test_fk_child_column_no_findings(self) -> None:
        outputs = {
            "parents": pa.table({"id": ["p1", "p2"]}),
            "children": pa.table({"parent_id": ["p1", "p2"]}),
        }
        sources = {
            "parents": pa.table({"id": ["p1", "p2"]}),
            "children": pa.table({"parent_id": ["p1", "p2"]}),
        }
        config = _cfg(
            [
                {"name": "parents", "columns": [{"name": "id", "strategy": "passthrough"}]},
                {"name": "children", "columns": [{"name": "parent_id", "strategy": "hash"}]},
            ],
            relationships=[
                {
                    "parent": {"table": "parents", "columns": ["id"]},
                    "children": [{"table": "children", "columns": ["parent_id"]}],
                    "orphan_policy": "fail",
                }
            ],
        )
        report = validate(outputs, config, sources=sources)
        assert report.passed is True

    def test_passthrough_column_no_findings(self) -> None:
        outputs = {"t": pa.table({"col": ["alice", "bob"]})}
        sources = {"t": pa.table({"col": ["alice", "bob"]})}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "passthrough"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is True

    def test_explicit_scope_naming_generate_table_raises(self) -> None:
        outputs = {"g": pa.table({"col": ["a", "b"]})}
        config = _cfg(
            [
                {
                    "name": "g",
                    "generate_columns": [{"name": "col", "type": "faker", "faker_type": "name"}],
                    "row_count": 2,
                }
            ],
            lc_columns={"g": ["col"]},
        )
        with pytest.raises(ValueError, match="leak_check"):
            validate(outputs, config, sources={})


class TestMissingSourceFailsLoud:
    def test_default_scope_missing_source_raises(self) -> None:
        outputs = {"t": pa.table({"col": ["alice"]})}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "hash"}]}])
        with pytest.raises(ValueError, match="leak_check"):
            validate(outputs, config, sources={})

    def test_default_scope_sources_none_raises(self) -> None:
        outputs = {"t": pa.table({"col": ["alice"]})}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "hash"}]}])
        with pytest.raises(ValueError, match="leak_check"):
            validate(outputs, config)


class TestNoValuesInFindings:
    def test_detail_string_has_no_cell_values(self) -> None:
        outputs = {"t": pa.table({"ssn": ["111-22-3333-secret"]})}
        sources = {"t": pa.table({"ssn": ["111-22-3333-secret"]})}
        config = _cfg([{"name": "t", "columns": [{"name": "ssn", "strategy": "hash"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is False
        for finding in report.findings:
            assert "111-22-3333-secret" not in finding.detail


class TestUnclassifiedStrategyFailsClosed:
    """LOW L1 (dennis review 2026-07-04): an unclassified strategy must RAISE,
    not silently skip the column (fail-closed, consistent with the sprint's
    theme). This can only happen if the drift sentry was bypassed."""

    def test_unclassified_strategy_raises(self) -> None:
        outputs = {"t": pa.table({"c": ["alice", "bob"]})}
        sources = {"t": pa.table({"c": ["alice", "bob"]})}
        # A strategy name that is not in any classification set.
        config = _cfg(
            [{"name": "t", "columns": [{"name": "c", "strategy": "made_up_strategy_zzz"}]}]
        )
        with pytest.raises(ValueError, match="leak_check"):
            validate(outputs, config, sources=sources)


class TestQuarantineComposition:
    def test_leak_findings_quarantinable_under_validation_fail(self) -> None:
        from decoy_engine.quarantine import apply_quarantine

        outputs = {"t": pa.table({"ssn": ["leak1", "masked2", "leak3"]})}
        sources = {"t": pa.table({"ssn": ["leak1", "src2", "leak3"]})}
        config = _cfg([{"name": "t", "columns": [{"name": "ssn", "strategy": "hash"}]}])
        report = validate(outputs, config, sources=sources)
        assert report.passed is False
        filtered, summary = apply_quarantine(
            outputs,
            report,
            {"enabled": True, "output_path": "/dev/null", "triggers": ["validation_fail"]},
        )
        assert filtered["t"].num_rows == 1
        assert filtered["t"].column("ssn").to_pylist() == ["masked2"]


class TestDirectUnitCallable:
    """validate_leak_check is directly callable per the registry contract."""

    def test_direct_call_matches_registry_dispatch(self) -> None:
        outputs = {"t": pa.table({"col": ["a", "b"]})}
        sources = {"t": pa.table({"col": ["a", "b"]})}
        entry = {"name": "leak_check"}
        config = _cfg([{"name": "t", "columns": [{"name": "col", "strategy": "passthrough"}]}])
        findings = validate_leak_check(outputs, entry, config, sources=sources)
        assert findings == ()
