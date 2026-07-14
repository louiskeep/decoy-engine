"""Job C seeded fixture generator: HR (self-referential FK + generate-heavy table).

Builds one mask table -- employees -- with EXACT planted edge-case counts so
the invariant assertions compare against known integers.

The self-referential FK:
  employees.manager_id -> employees.employee_id (same table, same namespace)

Topology: self_referential.

Planted edge cases (match manifest.yaml invariants exactly):
  - ROOT_COUNT = 10 root-node rows with manager_id = NULL.
    Null manager_ids pass through the FK remap machinery unchanged.
  - ORPHAN_COUNT = 1 row with manager_id = "emp99999" (not in employee_id set).
    "emp99999" is in-charset for the FPE alphanum charset
    ("0123456789abcdefghijklmnopqrstuvwxyz"), so orphan_policy:remap re-applies
    FPE and permutes it to a different in-charset value (reversible, no leak).
    The fk_integrity invariant verifies expected_orphans:1 AND that the remapped
    output value != "emp99999" (remap genuinely masked it). DE-01 cluster-C
    (2026-07-14) reverted this from the all-out-of-charset "EMP-ORPHAN": that
    value now fails closed (non-round-trip), so the orphan must be in-charset.
  - SENTINEL_PHONE = "800-555-0100" planted in the notes column of employee row 0.
    text_mask with phone_number detector must redact it; sentinel scan verifies absence.

Correlations (deliberate, non-trivial for the distribution invariant):
  - salary correlates with department: Engineering highest, Finance/HR lowest.
    department is categorical (preserved); salary is bucketized (coarsened).
    The joint (department, salary_bucket) is intentionally not declared as a joint
    because salary_bucket is a coarsen column; its value changes relative to
    salary and the TVD metric cannot track correlation through that transformation.
    department is waived from joint requirement with an explicit reason.

Row counts (from manifest.yaml):
  - employees: 2500 (10 root + 1 orphan + 2489 valid non-root)

Source format notes:
  - employee_id: "EMP-{n:05d}" for n in 1..2500.
  - manager_id: None for roots, "emp99999" for the orphan row, otherwise
    a valid employee_id from the root set (depth-2 tree, no cycles).
  - department: one of Engineering/Sales/Marketing/HR/Finance.
  - salary: float; department-correlated.
  - hire_date: ISO date string YYYY-MM-DD; spread over 2014..2023.
  - notes: short free-text string; row 0 has SENTINEL_PHONE embedded.
  - division_hash: department mapped through _DIVISION_MAP (TH-3.4 hash-hash
    masked_correlations partner for dept_hash).
"""

from __future__ import annotations

import datetime
from typing import Any

import numpy as np
import pandas as pd

from testflight._fixtures import make_rng, verify_fingerprint

# ---------------------------------------------------------------------------
# Constants (match manifest.yaml exactly)
# ---------------------------------------------------------------------------

EMPLOYEE_COUNT = 2500

# Exactly 10 root employees have manager_id = NULL.
ROOT_COUNT = 10

# Exactly 1 employee has manager_id = ORPHAN_SOURCE_KEY (not in employee_id set).
# The FK remap machinery handles this via orphan_policy:remap.
ORPHAN_COUNT = 1

# Source FK value for the orphan row. In-charset for the FPE alphanum charset
# (0-9 + a-z) so orphan_policy:remap re-applies FPE and produces a permuted,
# reversible value that differs from the source key.
# DE-01 cluster-C (2026-07-14): reverted from the all-out-of-charset "EMP-ORPHAN"
# back to "emp99999". Fix #42 had changed it TO "EMP-ORPHAN" to exercise the
# covering-hash path; DE-01 closes that path (an all-out-of-charset value now
# fails closed as non-round-trip), so the orphan must be an in-charset value the
# cipher can actually permute. The all-out-of-charset fail-closed behavior is
# covered by the dedicated negative test (test_fpe_fail_closed_pipeline.py).
ORPHAN_SOURCE_KEY = "emp99999"

# Source fingerprint (SHA-256 of canonical CSV). verify_fingerprint is called
# inside build_employees so a faker/numpy version bump that shifts the fixture
# output fails loudly with a re-baseline instruction.
# Baseline: run compute_fingerprint(build_employees(seed=44)) and update here.
# Updated (fix #42): ORPHAN_SOURCE_KEY changed from "emp99999" to "EMP-ORPHAN".
# Updated (TH-3.4): added division_hash column (hash-hash masked_correlations pair).
# Updated (DE-01 cluster-C, 2026-07-14): ORPHAN_SOURCE_KEY reverted to "emp99999"
# (in-charset, round-trips) because all-out-of-charset FPE now fails closed.
_EMPLOYEES_FINGERPRINT = "beafc60902e1436dc8127450e98c45c838b129e47d35a150d9be25c5194aa6bc"

# The sentinel phone number planted in the notes column of row 0.
# text_mask with phone_number detector must redact it; sentinel scan checks absence.
SENTINEL_PHONE = "800-555-0100"

# Department choices and their salary ranges (USD annual).
# Engineering is high; Finance/HR are low; Sales/Marketing in between.
_DEPT_NAMES = ["Engineering", "Sales", "Marketing", "HR", "Finance"]
_DEPT_WEIGHTS = np.array([0.35, 0.25, 0.20, 0.10, 0.10])
_DEPT_SALARY_LO = np.array([90_000.0, 55_000.0, 50_000.0, 45_000.0, 60_000.0])
_DEPT_SALARY_HI = np.array([180_000.0, 110_000.0, 100_000.0, 80_000.0, 120_000.0])

# TH-3.4 (P1-9): division is a deterministic many-to-one function of department
# (a real organisational grouping), giving (dept_hash, division_hash) genuine
# source association to survive through TWO INDEPENDENT hash masks -- the
# hash-hash masked_correlations pair. A fixed dict (not a set) keeps iteration
# order process-stable regardless of PYTHONHASHSEED.
_DIVISION_MAP: dict[str, str] = {
    "Engineering": "Product",
    "Sales": "Product",
    "Marketing": "GTM",
    "HR": "Corporate",
    "Finance": "Corporate",
}

# Hire date range: 2014-01-01 .. 2023-12-31 (3651 days).
_HIRE_BASE = datetime.date(2014, 1, 1)
_HIRE_SPAN_DAYS = 3651


def _random_date(rng: np.random.Generator) -> str:
    """Return a random ISO date string within the 2014-2023 hire window."""
    offset = int(rng.integers(0, _HIRE_SPAN_DAYS, endpoint=False))
    return (_HIRE_BASE + datetime.timedelta(days=offset)).isoformat()


def build_employees(seed: int = 44, **_kwargs: Any) -> pd.DataFrame:
    """Build the employees mask table with planted edge cases.

    All non-root employees reference one of the 10 root nodes as manager,
    keeping the FK tree at depth 2 and ensuring no cycles in the source.

    Args:
        seed: Deterministic seed (default matches manifest.yaml seed: 44).

    Returns:
        pandas DataFrame with EMPLOYEE_COUNT rows. verify_fingerprint is
        called before returning once the baseline constant is set.
    """
    rng = make_rng(seed)

    n = EMPLOYEE_COUNT
    # Pre-generate all department indices in one vectorized draw.
    dept_idxs = rng.choice(len(_DEPT_NAMES), size=n, p=_DEPT_WEIGHTS)
    # Pre-generate all salaries: uniform within per-department range.
    u = rng.uniform(size=n)
    salaries = _DEPT_SALARY_LO[dept_idxs] + u * (
        _DEPT_SALARY_HI[dept_idxs] - _DEPT_SALARY_LO[dept_idxs]
    )
    salaries = np.round(salaries, 2)
    # Pre-generate all hire dates.
    day_offsets = rng.integers(0, _HIRE_SPAN_DAYS, size=n, endpoint=False)
    hire_dates = [(_HIRE_BASE + datetime.timedelta(days=int(d))).isoformat() for d in day_offsets]

    # Build employee IDs.
    emp_ids = [f"EMP-{i:05d}" for i in range(1, n + 1)]

    # Build manager_ids:
    # - Rows 0..ROOT_COUNT-1: None (root nodes).
    # - Row ROOT_COUNT: ORPHAN_SOURCE_KEY (in-charset orphan; not in emp_ids set).
    # - Rows ROOT_COUNT+1..: reference a root node (cycle-free depth-2 tree).
    root_ids = emp_ids[:ROOT_COUNT]
    manager_ids: list[Any] = [None] * ROOT_COUNT + [ORPHAN_SOURCE_KEY]
    # For remaining rows, sample from root pool.
    n_remaining = n - ROOT_COUNT - ORPHAN_COUNT
    root_choices = rng.integers(0, ROOT_COUNT, size=n_remaining, endpoint=False)
    manager_ids.extend(root_ids[int(idx)] for idx in root_choices)

    # Build notes column (generic text; row 0 gets the sentinel phone).
    notes = ["Standard employee record."] * n
    notes[0] = f"Contact: {SENTINEL_PHONE}. Reports directly to board."

    dept_names = [_DEPT_NAMES[int(i)] for i in dept_idxs]

    # Phase 4: extra columns to exercise redact, truncate, hash, and shuffle
    # strategies in the coverage guard without changing existing FK/fidelity checks.
    #
    # badge_id: a unique badge number per employee; the pipeline redacts it to
    #   "REDACTED" so the raw badge does not appear in output.
    badge_ids = [f"BADGE-{n:05d}" for n in range(1, len(emp_ids) + 1)]
    #
    # dept_code: the full department name is the source; the pipeline truncates to
    #   the first 3 characters for a short display code ("Eng", "Sal", etc.).
    dept_codes = list(dept_names)  # same values as department
    #
    # dept_hash: the department name source for a one-way hash (for analytics
    #   systems that need a consistent anonymous department token without
    #   storing the real name).
    dept_hash_src = list(dept_names)
    #
    # dept_shuffle: the department name source for a deterministic column-level
    #   shuffle that preserves the marginal distribution but decouples assignment.
    dept_shuffle_src = list(dept_names)

    # division_hash: TH-3.4 hash-hash masked_correlations pair partner for
    # dept_hash. division is a deterministic function of department (see
    # _DIVISION_MAP), so (dept_hash, division_hash) carries the SAME real
    # association as (department, division) before either is masked.
    division_hash_src = [_DIVISION_MAP[d] for d in dept_names]

    df = pd.DataFrame(
        {
            "employee_id": emp_ids,
            "manager_id": manager_ids,
            "department": dept_names,
            "salary": salaries.tolist(),
            "hire_date": hire_dates,
            "notes": notes,
            "badge_id": badge_ids,
            "dept_code": dept_codes,
            "dept_hash": dept_hash_src,
            "dept_shuffle": dept_shuffle_src,
            "division_hash": division_hash_src,
        }
    )

    # --- Sanity assertions (fail-fast if fixture logic drifts) ---
    assert len(df) == EMPLOYEE_COUNT, f"Expected {EMPLOYEE_COUNT} employees, got {len(df)}."
    null_mgr = int(df["manager_id"].isna().sum())
    assert null_mgr == ROOT_COUNT, (
        f"Expected {ROOT_COUNT} root (null manager_id) rows, got {null_mgr}."
    )
    orphan_mgr = int((df["manager_id"] == ORPHAN_SOURCE_KEY).sum())
    assert orphan_mgr == ORPHAN_COUNT, f"Expected {ORPHAN_COUNT} orphan FK row, got {orphan_mgr}."
    # Verify all non-orphan manager refs exist in the employee_id set.
    valid_ids = set(df["employee_id"])
    bad = df[
        df["manager_id"].notna()
        & (df["manager_id"] != ORPHAN_SOURCE_KEY)
        & ~df["manager_id"].isin(valid_ids)
    ]
    assert len(bad) == 0, f"Unexpected unresolved manager_ids: {bad['manager_id'].tolist()[:5]}"

    verify_fingerprint(df, _EMPLOYEES_FINGERPRINT, label="employees")

    return df
