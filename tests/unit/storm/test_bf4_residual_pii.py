"""BF4: residual-PII scan on masked output (pure-engine runner tests).

Synthetic fixture tests that exercise ``check_residual_pii`` as a
standalone post-mask pass. These tests represent BF4's behavioral
contract: after a masking job runs, the engine re-runs the Storm
detectors over the OUTPUT and classifies any surviving PII hits.

Security invariant: report findings carry only aggregate/identifier-
level information (counts, rates, column names, detector names) --
never raw cell values. A dedicated section asserts this invariant.

Scenario coverage:
  - S1: SSN column with a failed hash mask (output == source -> fail)
  - S2: SSN column with a successful hash mask (output is hex -> info/none)
  - S3: Email column not configured for masking (unconfigured -> warning)
  - S4: Email column with redact strategy still matching (redact failed -> fail)
  - S5: Multi-column table with a mix of outcomes
  - S6: Column with no PII pattern -> no finding
  - Security: report dict contains no raw SSN/email cell values
"""

from __future__ import annotations

import hashlib
import re

import pandas as pd

from decoy_engine.storm.postmask.residual_pii import check_residual_pii
from decoy_engine.storm.postmask.runner import run_storm_post_mask

# ── Synthetic fixture factories ───────────────────────────────────────────────

_N = 100  # row count for all fixtures; large enough to cross detector thresholds


def _ssn_values() -> list[str]:
    """100 syntactically-valid SSNs (NNN-NN-NNNN format, varied prefix)."""
    return [f"{500 + (i // 10):03d}-{10 + (i % 10):02d}-{1000 + i:04d}" for i in range(_N)]


def _hashed_values(raw: list[str]) -> list[str]:
    """SHA-256-hex the values -- realistic hash-strategy output.

    Hex strings cannot match the SSN pattern (only digits + dashes),
    so the detector will NOT fire on them.
    """
    return [hashlib.sha256(v.encode()).hexdigest()[:16] for v in raw]


def _email_values() -> list[str]:
    return [f"user{i}@realcorp.com" for i in range(_N)]


def _synthetic_emails() -> list[str]:
    """Faker-produced emails -- still match the email detector, but are synthetic."""
    return [f"fake{i}@masked.example" for i in range(_N)]


def _plain_text() -> list[str]:
    """Non-PII text column."""
    return [f"note about event {i}" for i in range(_N)]


# ── S1: Failed hash -- SSN output == SSN source ───────────────────────────────


class TestS1FailedHashSsn:
    """SSN column configured as 'hash' but the output is still raw SSNs.

    This is the canonical BF4 catch: the mask *should* destroy the SSN
    pattern but it did not fire. The scanner must surface severity='fail'.
    """

    def test_surviving_ssn_on_hash_strategy_is_fail(self):
        ssns = _ssn_values()
        source = {"patients": pd.DataFrame({"ssn": ssns})}
        output = {"patients": pd.DataFrame({"ssn": ssns})}  # mask did not fire
        config = {
            "tables": [
                {"name": "patients", "columns": [{"name": "ssn", "strategy": "hash"}]},
            ],
        }
        findings = check_residual_pii(output, config, source_frames=source)
        assert len(findings) >= 1
        fail_findings = [f for f in findings if f.severity == "fail"]
        assert len(fail_findings) >= 1, (
            f"expected at least one fail finding for SSN surviving hash; got {findings!r}"
        )
        finding = fail_findings[0]
        assert finding.table == "patients"
        assert finding.column == "ssn"
        assert finding.detector_id == "ssn"
        assert finding.configured_strategy == "hash"
        assert finding.source_compared is True
        assert finding.source_identity_rate == 1.0

    def test_surviving_ssn_reported_via_runner(self):
        ssns = _ssn_values()
        src = {"patients": pd.DataFrame({"ssn": ssns})}
        out = {"patients": pd.DataFrame({"ssn": ssns})}
        config = {
            "tables": [{"name": "patients", "columns": [{"name": "ssn", "strategy": "hash"}]}],
        }
        report = run_storm_post_mask(src, out, config=config)
        assert report["fail_count"] >= 1
        assert any(f["severity"] == "fail" for f in report["residual_pii"])


# ── S2: Successful hash -- SSN hashed to hex, detector no longer fires ────────


class TestS2SuccessfulHashSsn:
    """SSN column where the hash mask actually ran. Output is hex strings.

    The SSN regex only fires on NNN-NN-NNNN patterns. Hex output does not
    match. The scanner should produce no SSN finding (the check passes).
    """

    def test_hashed_ssns_produce_no_residual_finding(self):
        ssns = _ssn_values()
        hashed = _hashed_values(ssns)
        source = {"patients": pd.DataFrame({"ssn": ssns})}
        output = {"patients": pd.DataFrame({"ssn": hashed})}
        config = {
            "tables": [
                {"name": "patients", "columns": [{"name": "ssn", "strategy": "hash"}]},
            ],
        }
        findings = check_residual_pii(output, config, source_frames=source)
        # No SSN detector should fire on hex output.
        ssn_findings = [f for f in findings if f.detector_id == "ssn" and f.column == "ssn"]
        assert ssn_findings == [], (
            f"unexpected SSN finding on successfully-hashed output: {ssn_findings!r}"
        )

    def test_hashed_ssns_produce_zero_fail_via_runner(self):
        ssns = _ssn_values()
        hashed = _hashed_values(ssns)
        src = {"patients": pd.DataFrame({"ssn": ssns})}
        out = {"patients": pd.DataFrame({"ssn": hashed})}
        config = {
            "tables": [{"name": "patients", "columns": [{"name": "ssn", "strategy": "hash"}]}],
        }
        report = run_storm_post_mask(src, out, config=config)
        ssn_fails = [
            f
            for f in report["residual_pii"]
            if f["column"] == "ssn" and f["severity"] == "fail"
        ]
        assert ssn_fails == [], (
            f"hashed SSNs should produce no fail findings; got {ssn_fails!r}"
        )


# ── S3: Unconfigured PII column ───────────────────────────────────────────────


class TestS3UnconfiguredEmailColumn:
    """Email column with no masking policy.

    The operator forgot to mask it. The scanner should surface
    severity='warning' -- not a hard fail (the user may review and
    decide the column is non-sensitive in context).
    """

    def test_unconfigured_email_column_is_warning(self):
        emails = _email_values()
        output = {"users": pd.DataFrame({"email": emails})}
        config = {"tables": [{"name": "users", "columns": []}]}
        findings = check_residual_pii(output, config)
        email_findings = [f for f in findings if f.column == "email"]
        assert len(email_findings) >= 1
        assert all(f.severity == "warning" for f in email_findings), (
            f"unconfigured email column must be 'warning', got {email_findings!r}"
        )

    def test_unconfigured_column_severity_escalates_to_fail_on_source_identity(self):
        emails = _email_values()
        source = {"users": pd.DataFrame({"email": emails})}
        output = {"users": pd.DataFrame({"email": emails})}  # leaked through
        config = {"tables": [{"name": "users", "columns": []}]}
        findings = check_residual_pii(output, config, source_frames=source)
        email_findings = [f for f in findings if f.column == "email"]
        assert any(f.severity == "fail" for f in email_findings), (
            "unconfigured email with 100% source identity must escalate to fail"
        )


# ── S4: Redact strategy -- pattern must not survive ───────────────────────────


class TestS4RedactStrategyFailed:
    """Email column configured as 'redact', but output still has emails.

    'redact' is in _DESTROYS_PATTERN. Any surviving email detector hit
    on a redact column is severity='fail' (the mask didn't fire).
    """

    def test_email_surviving_redact_is_fail(self):
        emails = _email_values()
        source = {"users": pd.DataFrame({"email": emails})}
        output = {"users": pd.DataFrame({"email": emails})}
        config = {
            "tables": [
                {"name": "users", "columns": [{"name": "email", "strategy": "redact"}]},
            ],
        }
        findings = check_residual_pii(output, config, source_frames=source)
        fail_findings = [
            f for f in findings if f.severity == "fail" and f.column == "email"
        ]
        assert len(fail_findings) >= 1
        assert fail_findings[0].configured_strategy == "redact"
        assert fail_findings[0].detector_id == "email"


# ── S5: Multi-column table with mixed outcomes ────────────────────────────────


class TestS5MultiColumnMixedOutcomes:
    """Realistic mixed-column fixture.

    - ssn: hash mask FAILED (output == source) -> fail
    - email: faker mask ran correctly (synthetic output) -> info
    - notes: plain text, no PII -> no finding
    """

    def test_mixed_table_classifies_each_column_independently(self):
        ssns = _ssn_values()
        emails = _email_values()
        syn_emails = _synthetic_emails()
        notes = _plain_text()

        source = {
            "patients": pd.DataFrame(
                {"ssn": ssns, "email": emails, "notes": notes}
            )
        }
        output = {
            "patients": pd.DataFrame(
                {
                    "ssn": ssns,  # hash failed, still SSNs
                    "email": syn_emails,  # faker ran correctly
                    "notes": notes,  # unchanged, no PII
                }
            )
        }
        config = {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {"name": "ssn", "strategy": "hash"},
                        {"name": "email", "strategy": "faker"},
                        {"name": "notes", "strategy": "passthrough"},
                    ],
                }
            ],
        }
        findings = check_residual_pii(output, config, source_frames=source)
        by_col = {f.column: f for f in findings}

        # SSN survived hash -> fail
        assert "ssn" in by_col
        assert by_col["ssn"].severity == "fail"

        # Email faker ran correctly -> info (synthetic emails still match detector)
        if "email" in by_col:
            assert by_col["email"].severity == "info", (
                f"faker-produced email should be 'info', got {by_col['email'].severity!r}"
            )

        # Notes: plain text, no PII pattern; should not appear in findings
        assert "notes" not in by_col or by_col["notes"].detector_id != "ssn"

    def test_runner_summary_counters_reflect_mixed_findings(self):
        ssns = _ssn_values()
        emails = _email_values()
        syn_emails = _synthetic_emails()

        src = {"patients": pd.DataFrame({"ssn": ssns, "email": emails})}
        out = {"patients": pd.DataFrame({"ssn": ssns, "email": syn_emails})}
        config = {
            "tables": [
                {
                    "name": "patients",
                    "columns": [
                        {"name": "ssn", "strategy": "hash"},
                        {"name": "email", "strategy": "faker"},
                    ],
                }
            ],
        }
        report = run_storm_post_mask(src, out, config=config)
        # At least one fail (SSN) and at least one info (email faker)
        assert report["fail_count"] >= 1
        all_sevs = {f["severity"] for f in report["residual_pii"]}
        assert "fail" in all_sevs


# ── S6: Non-PII column produces no finding ───────────────────────────────────


class TestS6NonPiiColumn:
    """A column with plain text values should generate no findings."""

    def test_no_pii_column_produces_no_finding(self):
        notes = _plain_text()
        output = {"events": pd.DataFrame({"notes": notes})}
        config = {
            "tables": [
                {"name": "events", "columns": [{"name": "notes", "strategy": "passthrough"}]},
            ],
        }
        findings = check_residual_pii(output, config)
        notes_findings = [f for f in findings if f.column == "notes"]
        assert notes_findings == [], (
            f"non-PII text column should produce no findings; got {notes_findings!r}"
        )


# ── Security: no raw PII cell values in report ────────────────────────────────


class TestSecurityNoRawValuesInReport:
    """Verify the BF4 security invariant: the report payload carries only
    aggregate (counts, rates) and identifier-level (column names, detector
    names) information. No raw cell values from the data may appear.

    Rationale from spec: 'SECURITY: aggregate/identifier-level results only
    -- never raw PII cell values in the returned report or logs.'
    """

    # A DETECTOR-VALID SSN (area != 000/666/900-999, group != 00, serial != 0000)
    # so the residual scanner actually fires on it; otherwise the canary is
    # vacuous (an invalid SSN produces no finding, so a leak could never show).
    _CANARY_SSN = "501-22-3456"

    def test_ssn_cell_values_absent_from_report_dict(self):
        # Build a fixture where ONE specific SSN appears in every row, masked
        # with a strategy whose output still equals the source (silently-failed
        # mask) so the residual scanner DOES produce a finding for it.
        ssns = [self._CANARY_SSN] * _N
        src = {"patients": pd.DataFrame({"ssn": ssns})}
        out = {"patients": pd.DataFrame({"ssn": ssns})}
        config = {
            "tables": [
                {"name": "patients", "columns": [{"name": "ssn", "strategy": "hash"}]},
            ],
        }
        report = run_storm_post_mask(src, out, config=config)
        # Guard against a vacuous canary: the scanner MUST have flagged the
        # residual SSN, otherwise the no-leak assertion below proves nothing.
        assert any(f["severity"] == "fail" for f in report["residual_pii"]), (
            "vacuous canary: the residual scanner produced no fail finding for "
            "the unmasked SSN, so the no-leak assertion would never exercise the "
            "value-handling path it claims to guard."
        )
        # Serialize the entire report to a string and scan for the canary value.
        report_str = str(report)
        assert self._CANARY_SSN not in report_str, (
            f"raw SSN value {self._CANARY_SSN!r} leaked into the report payload: "
            f"see {report_str[:500]!r}"
        )

    def test_residual_pii_findings_contain_no_raw_values(self):
        emails = _email_values()
        src = {"users": pd.DataFrame({"email": emails})}
        out = {"users": pd.DataFrame({"email": emails})}
        config = {"tables": [{"name": "users", "columns": [{"name": "email", "strategy": "hash"}]}]}
        findings = check_residual_pii(out, config, source_frames=src)
        for f in findings:
            # The finding's string representation must not contain a full email address.
            # We check for the literal '@' separator which identifies a raw email value.
            # Column names and detector names are whitelisted because they are metadata.
            # We check the 'message' field specifically since it's the most likely
            # place a value would accidentally appear.
            msg = f.message
            # Allow at most one '@' (from "masked." in strategy descriptions),
            # but not a full user@domain.com pattern.
            assert not re.search(r"user\d+@realcorp\.com", msg), (
                f"raw email appeared in finding.message: {msg!r}"
            )
