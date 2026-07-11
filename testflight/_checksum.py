"""Independent checksum validation for the test-flight suite (TH-2.4 / P1-4).

Extracted from _invariants.py (module-split note in that file's docstring).

Validates fpe-checksum output with python-stdnum directly -- already a
harness dependency, used today only in fixture generation (see
testflight/jobs/a_healthcare_claims/fixture.py and
testflight/jobs/b_retail_m2m/fixture.py) -- instead of calling
decoy_engine.checksums.validate. Before this change the harness asked the
ENGINE'S OWN validator whether the engine's own masked output was valid: a
bug that broke check-digit computation and validate() the same way would
agree with itself and ship green (the engine grading its own homework).

Per-scheme independence:

  luhn -- delegates straight to stdnum.luhn.is_valid (Luhn 1954, US Patent
          2,950,048). This is the same underlying primitive the engine's own
          luhn scheme wraps (decoy_engine.checksums._luhn_validate), but
          called directly here rather than through decoy_engine.checksums,
          so a bug in the engine's checksums module (dispatch, wrapping, or
          a monkeypatched/broken validate()) cannot blind the harness.
  npi  -- python-stdnum has no NPI module (the same reason the engine hand-
          rolls it; see decoy_engine.checksums module docstring), so this is
          a from-spec reimplementation using stdnum.luhn as the Luhn
          primitive: the CMS NPPES NPI Check Digit Procedure (2008) defines
          the check digit as the Luhn check digit of '80840' + the 10-digit
          NPI itself, i.e. stdnum.luhn.is_valid('80840' + npi) -- a different
          code path than the engine's hand-rolled _luhn_cd loop
          (decoy_engine.checksums._npi_validate / _npi_calc_check_digit).

Only luhn and npi are implemented: those are the two schemes any current
test-flight job declares in `checksums` (see testflight/_coverage.py's
_CHECKSUM_SCHEME_ALLOWLIST for the five schemes -- iban, vin, isbn13, ean13,
gtin -- not yet exercised by a job). An unimplemented scheme raises rather
than silently falling back to the engine's own validator, which would
reintroduce the exact anti-pattern this module closes.
"""

from __future__ import annotations

from typing import Any

from ._spec import ChecksumSpec


def _harness_validate(scheme: str, value: str) -> bool:
    """Validate one checksum value independently of decoy_engine.checksums.

    Raises:
        NotImplementedError: If `scheme` has no independent harness
            implementation yet (fail loudly rather than silently trusting
            the engine's own validator).
    """
    import stdnum.luhn as _luhn

    if scheme == "luhn":
        return _luhn.is_valid(value)
    if scheme == "npi":
        if len(value) != 10 or not value.isdigit():
            return False
        if value[0] not in ("1", "2"):  # NPPES allocates 1- and 2-prefixed NPIs only
            return False
        return _luhn.is_valid("80840" + value)
    raise NotImplementedError(
        f"harness-independent checksum validation not implemented for scheme "
        f"{scheme!r}. Only 'luhn' and 'npi' are implemented (TH-2.4); add an "
        f"independent implementation here rather than falling back to "
        f"decoy_engine.checksums.validate."
    )


def check_checksums(
    job_name: str,
    spec: list[ChecksumSpec],
    result: Any,
) -> None:
    """Assert every output value in checksum columns validates independently.

    Calls the harness's OWN validator (_harness_validate, above) per row in
    each declared (table, column) pair -- NOT decoy_engine.checksums.validate
    -- so the engine is not grading its own homework (TH-2.4 / P1-4).

    Args:
        job_name: Job name for error messages.
        spec: List of ChecksumSpec from the manifest invariants.
        result: ExecutionResult carrying masked output tables.

    Raises:
        AssertionError: If any output value fails independent checksum validation.
    """
    for cs in spec:
        tbl = result.outputs.get(cs.table)
        assert tbl is not None, f"[{job_name}] checksums: table '{cs.table}' not in result.outputs."
        values: list[Any] = tbl.column(cs.column).to_pylist()
        for i, v in enumerate(values):
            if v is None:
                continue
            str_v = str(v)
            ok = _harness_validate(cs.scheme, str_v)
            assert ok, (
                f"[{job_name}] checksums: {cs.table}.{cs.column} row {i}: "
                f"harness-independent validate('{cs.scheme}', {str_v!r}) == False "
                f"(validated via python-stdnum, NOT decoy_engine.checksums.validate). "
                f"Column uses a checksum-producing strategy (fpe + checksum:{cs.scheme}) "
                f"but the output value failed the check-digit assertion. "
                f"This indicates the FPE checksum-recomputation path was not taken."
            )
