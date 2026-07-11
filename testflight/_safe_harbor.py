"""Safe Harbor (HIPAA 45 CFR 164.514(b)(2)) invariant for the test-flight suite.

Extracted from _invariants.py (module-split note in that file's docstring).

TH-2.3 / P1-6 hardens two things:

  - Prefix-at-any-length leak scan. The old check only scanned values of
    EXACTLY length 5 (a full ZIP5). With this suite's row counts (<=20,000),
    geo_generalize's own cascade resolves most NON-restricted rows to the
    3-char zip3 level: the zip3 cascade level's effective-count shortcut
    (``max(20000, count) >= k_threshold``) trivially passes for any
    k_threshold <= 20,000 (see
    decoy_engine.transforms.geo_generalize._cascade_one_row), so full ZIP5
    almost never survives in this fixture's output regardless of whether
    Safe Harbor logic is correct. A restricted-skip regression would
    therefore leak a bare 3-char prefix or a zip+4-shaped value
    ("03601-1234") that the old length==5 check could never see. The new
    check flags ANY string of length >= 3 whose first 3 characters are a
    restricted prefix.
  - Independent suppression count. Cross-checks the actual output values
    (counting the literal "" suppression marker geo_generalize writes --
    decoy_engine.transforms.geo_generalize._SUPPRESS_VALUE) against BOTH the
    planted count and the engine's own self-reported cascade_decisions
    detail, rather than trusting the self-reported detail alone (the engine
    grading its own homework).
"""

from __future__ import annotations

from typing import Any

from ._spec import SafeHarborSpec

# HHS-restricted ZIP3 prefixes (HIPAA Safe Harbor; populations < 20,000).
_RESTRICTED_ZIP3_PREFIXES: frozenset[str] = frozenset(
    {
        "036",
        "059",
        "063",
        "102",
        "203",
        "556",
        "692",
        "790",
        "821",
        "823",
        "830",
        "831",
        "878",
        "879",
        "884",
        "890",
        "893",
    }
)


def check_safe_harbor(
    job_name: str,
    spec: list[SafeHarborSpec],
    result: Any,
) -> None:
    """Assert Safe Harbor suppression: no restricted-prefix leak, counts agree.

    For each SafeHarborSpec:
    - Assert no output value STARTS WITH a restricted ZIP3 prefix at any
      length >= 3 (TH-2.3 / P1-6): catches a full ZIP5, a bare zip3, and a
      zip+4 shape alike.
    - Assert an INDEPENDENT count of actual suppressed values ("" -- the
      literal value geo_generalize writes for a suppressed row) in the
      output column equals planted_restricted_zip3_count.
    - Cross-check that independent count against the engine's own
      geo_generalize_cascade QualityWarning detail (cascade_decisions); a
      disagreement between the two means the engine's bookkeeping does not
      match its own output data.
    - Assert expected_suppressions matches both counts.

    Args:
        job_name: Job name for error messages.
        spec: List of SafeHarborSpec from the manifest invariants.
        result: ExecutionResult carrying masked outputs and quality_metrics warnings.

    Raises:
        AssertionError: If a restricted prefix leaks, counts disagree, or
            counts mismatch the planted/expected values.
    """
    geo_warnings = [w for w in result.warnings if w.code == "geo_generalize_cascade"]

    for sh in spec:
        tbl = result.outputs.get(sh.table)
        assert tbl is not None, (
            f"[{job_name}] safe_harbor: table '{sh.table}' not in result.outputs."
        )
        out_values: list[Any] = tbl.column(sh.column).to_pylist()

        # 1. No value at ANY length >= 3 starts with a restricted prefix.
        leaked = [
            v
            for v in out_values
            if isinstance(v, str) and len(v) >= 3 and v[:3] in _RESTRICTED_ZIP3_PREFIXES
        ]
        assert len(leaked) == 0, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"{len(leaked)} restricted-prefix value(s) leaked into output (checked "
            f"at any length >= 3, not only full ZIP5): {leaked[:5]}. "
            f"geo_generalize should have generalized past zip3 or suppressed all "
            f"restricted-prefix rows."
        )

        # 2. Independent suppression count computed from the actual output
        #    data, NOT the engine's self-reported cascade_decisions detail.
        independent_suppressed = sum(1 for v in out_values if isinstance(v, str) and v == "")
        assert independent_suppressed == sh.planted_restricted_zip3_count, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"independent output-value suppressed count={independent_suppressed} != "
            f"planted_restricted_zip3_count={sh.planted_restricted_zip3_count}. "
            f"Counted directly from output values (v == ''), not from engine "
            f"self-reported bookkeeping."
        )

        # 3. Engine self-report cross-check.
        col_warnings = [w for w in geo_warnings if w.column == sh.column]
        assert col_warnings, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"no geo_generalize_cascade QualityWarning found for column '{sh.column}'. "
            f"Expected at least {sh.planted_restricted_zip3_count} suppressed rows. "
            f"Available warnings: {[w.code for w in result.warnings]}"
        )
        cascade_decisions: dict[str, str] = col_warnings[0].detail.get("cascade_decisions", {})
        self_reported_suppressed = sum(1 for v in cascade_decisions.values() if v == "suppressed")

        assert self_reported_suppressed == independent_suppressed, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"engine self-reported suppressed_count={self_reported_suppressed} != "
            f"independent output-value count={independent_suppressed}. "
            f"The cascade_decisions detail disagrees with the actual output data "
            f"(TH-2.3: the engine's own bookkeeping is not trusted alone)."
        )
        assert self_reported_suppressed == sh.expected_suppressions, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"suppressed_count={self_reported_suppressed} != "
            f"expected_suppressions={sh.expected_suppressions}."
        )
