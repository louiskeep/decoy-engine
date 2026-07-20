"""DPS-1: data-independent numeric bin ranges (`numeric_domains` / `dp_mode`).

Real module location note: the plan cites `generation/snapshot.py`; the
actual fit-time snapshot module is `decoy_engine.quality.snapshot`
(`generation/statistical` only CONSUMES the artifact). Reconciled against
`src/decoy_engine/quality/snapshot.py:114` (compute_distribution_snapshot)
and `:373` (_numeric_stats).
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.quality.snapshot import compute_distribution_snapshot


def test_numeric_domain_overrides_data_min_max():
    df = pd.DataFrame({"age": [31, 42, 55]})  # data range 31..55
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    stats = snap["columns"]["age"]["stats"]
    assert stats["bin_edges"][0] == 0.0
    assert stats["bin_edges"][-1] == 120.0
    assert snap["columns"]["age"]["support_origin"] == "caller"


def test_dp_mode_requires_domain_for_numeric():
    df = pd.DataFrame({"age": [31, 42, 55]})
    with pytest.raises(ValueError, match="numeric_domain"):
        compute_distribution_snapshot(df, dp_mode=True)  # no domain -> fail closed


def test_plain_call_omits_support_origin_key_entirely():
    # Byte-identical-for-existing-callers contract (golden digest test,
    # tests/snapshots/test_distribution_snapshot_baseline.py): a caller
    # that never touches numeric_domains/dp_mode gets the exact prior
    # dict shape, not just the same values -- so the key is absent, not
    # merely "data".
    df = pd.DataFrame({"age": [31, 42, 55]})
    snap = compute_distribution_snapshot(df)
    assert "support_origin" not in snap["columns"]["age"]
    assert snap["columns"]["age"]["stats"]["bin_edges"][0] == 31.0
    assert snap["columns"]["age"]["stats"]["bin_edges"][-1] == 55.0


def test_numeric_domains_without_dp_mode_still_reports_support_origin():
    # numeric_domains alone (without dp_mode) is a legitimate non-DP use
    # (e.g. a caller wants a fixed comparable bin range across two
    # snapshots) and must still surface provenance once it's supplied.
    df = pd.DataFrame({"age": [31, 42, 55]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)})
    assert snap["columns"]["age"]["support_origin"] == "caller"


def test_domain_clamps_out_of_domain_values_instead_of_dropping_them():
    # A value outside the caller's declared domain must not silently vanish
    # from bin_counts (numpy.histogram's default range behavior would drop
    # it, undercounting row_count -- a data-integrity bug for a privacy
    # feature). It is clamped into the nearest edge bin instead.
    df = pd.DataFrame({"age": [5, 50, 500]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    stats = snap["columns"]["age"]["stats"]
    assert sum(stats["bin_counts"]) == 3


def test_dp_mode_does_not_require_domain_for_non_numeric_columns():
    # dp_mode's fail-closed precondition is scoped to numeric bin ranges
    # (the only support this task makes data-independent); categorical
    # label-set independence is a separate mechanism (Task 3, dp.py).
    df = pd.DataFrame({"age": [31, 42, 55], "state": ["CA", "NY", "CA"]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    assert snap["columns"]["state"]["kind"] == "categorical"
