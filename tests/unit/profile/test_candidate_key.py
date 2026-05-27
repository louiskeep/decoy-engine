"""H6 invariant: is_candidate_key_sampled is only meaningful for full scans.

Per the engine-v2 S1 spec review, a sample-based equality between
distinct_count and row_count is not a candidate-key signal: a 10k-row
sample of a 1M-row table with 100k distinct values would erroneously
trip the equality. The ColumnProfile dataclass rejects the contradictory
combination (sampled=True AND is_candidate_key_sampled=True) at
construction time so the planner never sees an unsafe value.
"""

from __future__ import annotations

import pytest

from decoy_engine.profile import ColumnProfile, PIIClass


class TestCandidateKeySampledInvariant:
    def test_sampled_with_true_candidate_key_raises(self) -> None:
        with pytest.raises(ValueError, match="is_candidate_key_sampled"):
            ColumnProfile(
                name="customer_id",
                dtype="int64",
                row_count=1_000_000,
                null_count=0,
                distinct_count=10_000,
                sampled=True,
                is_candidate_key_sampled=True,
                declared_pk=False,
                is_fk=False,
                fk_target=None,
                pii_class=None,
            )

    def test_sampled_with_false_candidate_key_is_allowed(self) -> None:
        col = ColumnProfile(
            name="customer_id",
            dtype="int64",
            row_count=1_000_000,
            null_count=0,
            distinct_count=10_000,
            sampled=True,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert col.sampled is True
        assert col.is_candidate_key_sampled is False

    def test_full_scan_with_true_candidate_key_is_allowed(self) -> None:
        col = ColumnProfile(
            name="customer_id",
            dtype="int64",
            row_count=1000,
            null_count=0,
            distinct_count=1000,
            sampled=False,
            is_candidate_key_sampled=True,
            declared_pk=True,
            is_fk=False,
            fk_target=None,
            pii_class=None,
        )
        assert col.sampled is False
        assert col.is_candidate_key_sampled is True

    def test_full_scan_with_false_candidate_key_is_allowed(self) -> None:
        col = ColumnProfile(
            name="email",
            dtype="object",
            row_count=1000,
            null_count=12,
            distinct_count=987,
            sampled=False,
            is_candidate_key_sampled=False,
            declared_pk=False,
            is_fk=False,
            fk_target=None,
            pii_class=PIIClass.EMAIL,
        )
        assert col.sampled is False
        assert col.is_candidate_key_sampled is False

    def test_error_message_names_the_column(self) -> None:
        with pytest.raises(ValueError, match="ColumnProfile 'spurious_id'"):
            ColumnProfile(
                name="spurious_id",
                dtype="int64",
                row_count=1_000_000,
                null_count=0,
                distinct_count=10_000,
                sampled=True,
                is_candidate_key_sampled=True,
                declared_pk=False,
                is_fk=False,
                fk_target=None,
                pii_class=None,
            )
