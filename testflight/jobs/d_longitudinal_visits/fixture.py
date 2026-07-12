"""Job D seeded fixture generator: longitudinal patient visits.

Builds two mask source tables -- providers, patients -- with EXACT planted
edge-case counts, plus a distribution snapshot used by the `visits` generate
table's `statistical` columns.

The `visits` table itself is a generate table (kind="generate"); the engine
synthesises its rows from the generate_columns spec in manifest.yaml. It has no
source builder here. Its `statistical` columns read a distribution-snapshot/v1
artifact built at run time by ``build_charge_snapshot`` (below) and written to a
temp file by the runner (see testflight/_builder.build_snapshot_files); the
manifest references it via the ``snapshot:build_charge_snapshot`` placeholder.

Planted edge cases (match manifest.yaml invariants exactly):
  - INVALID_LUHN_COUNT = 12 patient rows with invalid-Luhn card_no values, at
    the LAST 12 positions. The luhn validator fires on them and they are
    quarantined. They carry a VALID provider_id (from the real pool) so removing
    them creates no new FK orphans.
  - ORPHAN_PATIENT_COUNT = 8 patient rows referencing fictional provider_ids
    (PRV9999xx) not in the providers pool. orphan_policy:warn keeps them in the
    output; the fk_integrity invariant asserts exactly 8 orphans. These carry a
    VALID Luhn card_no so they survive quarantine.

Source format notes:
  - provider_id: "PRV{n:06d}" (FPE PK; charset digits, "PRV" preserved).
  - specialty:   one of five clinical specialties (categorical mask column).
  - patient_id:  "PAT{n:06d}" (FPE PK).
  - card_no:     16-digit string; valid Luhn except the 12 quarantine rows.
  - region:      one of five regions (categorical mask column).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import stdnum.luhn as _luhn

from testflight._fixtures import make_rng, verify_fingerprint

# ---------------------------------------------------------------------------
# Constants (match manifest.yaml exactly)
# ---------------------------------------------------------------------------

PROVIDER_COUNT = 60
PATIENT_COUNT = 900

# Exactly 12 patient rows have invalid-Luhn card_no -- quarantined by the luhn
# validator. These are the LAST 12 rows (so the distribution row-parity trim,
# which drops the source tail, lines up with the quarantined rows).
INVALID_LUHN_COUNT = 12

# Exactly 8 patient rows reference fictional provider_ids -> orphans under warn.
ORPHAN_PATIENT_COUNT = 8

SPECIALTIES = ["Cardiology", "Oncology", "Pediatrics", "Neurology", "Radiology"]
REGIONS = ["North", "South", "East", "West", "Central"]

# Source fingerprints (SHA-256 of canonical CSV). verify_fingerprint is called
# inside each build_* function so a faker/numpy version bump that shifts the
# fixture output fails loudly with a re-baseline instruction. Re-baseline
# deliberately by running compute_fingerprint() on the new output and updating
# these constants.
_PROVIDERS_FINGERPRINT = "ff3a99b1ed020641c339a04ae9613416264de9a59c623cd69ca266c56ed14483"
_PATIENTS_FINGERPRINT = "20ca676a20e421730171853cdb1e2bce97198d8d02d9eaf87fee7f654cbc5d72"


# ---------------------------------------------------------------------------
# Helper: valid and invalid Luhn digit strings
# ---------------------------------------------------------------------------


def _make_luhn_valid(body: str) -> str:
    """Return body + valid Luhn check digit."""
    return body + _luhn.calc_check_digit(body)


def _make_luhn_invalid(body: str) -> str:
    """Return body + a check digit guaranteed INVALID for Luhn."""
    valid_check = _luhn.calc_check_digit(body)
    bad_check = str((int(valid_check) + 1) % 10)
    result = body + bad_check
    assert not _luhn.is_valid(result), f"Expected invalid Luhn for {result!r}"
    return result


# ---------------------------------------------------------------------------
# build_providers
# ---------------------------------------------------------------------------


def build_providers(seed: int = 45) -> pd.DataFrame:
    """Build the providers source DataFrame (FK parent).

    Args:
        seed: Reproducibility seed (matches manifest.seed).

    Returns:
        pandas DataFrame with columns: provider_id, specialty.
    """
    rng = make_rng(seed)
    rows: list[dict[str, Any]] = []
    for i in range(PROVIDER_COUNT):
        rows.append(
            {
                "provider_id": f"PRV{i + 1:06d}",
                "specialty": SPECIALTIES[int(rng.integers(0, len(SPECIALTIES)))],
            }
        )
    df = pd.DataFrame(rows)
    assert len(df) == PROVIDER_COUNT, f"Expected {PROVIDER_COUNT} providers, got {len(df)}"
    verify_fingerprint(df, _PROVIDERS_FINGERPRINT, label="providers")
    return df


# ---------------------------------------------------------------------------
# build_patients
# ---------------------------------------------------------------------------


def build_patients(seed: int = 45, providers_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build the patients source DataFrame (FK child).

    Row layout:
      [0 .. PATIENT_COUNT - INVALID_LUHN_COUNT - 1]  valid patients; a fixed set
          of ORPHAN_PATIENT_COUNT of them carry a fictional provider_id.
      [tail INVALID_LUHN_COUNT]                      quarantine patients (invalid
          Luhn card_no, VALID provider_id).

    Args:
        seed: Reproducibility seed.
        providers_df: Optional pre-built providers frame; if None it is built.

    Returns:
        pandas DataFrame with columns: patient_id, provider_id, card_no, region.
    """
    rng = make_rng(seed + 1)  # offset so patients rng diverges from providers

    if providers_df is None:
        providers_df = build_providers(seed)
    provider_pool = providers_df["provider_id"].tolist()

    n_valid = PATIENT_COUNT - INVALID_LUHN_COUNT

    # Fixed orphan positions within the valid block (deterministic, seeded).
    orphan_positions = set(rng.choice(n_valid, size=ORPHAN_PATIENT_COUNT, replace=False).tolist())
    # Fictional provider_ids NOT in the pool (PRV9999xx).
    fictional_providers = [f"PRV9999{k:02d}" for k in range(ORPHAN_PATIENT_COUNT)]

    rows: list[dict[str, Any]] = []

    # --- Phase 1: valid patients (incl. planted orphans) --------------------
    orphan_rank = 0
    for i in range(n_valid):
        if i in orphan_positions:
            provider_id = fictional_providers[orphan_rank]
            orphan_rank += 1
        else:
            provider_id = provider_pool[int(rng.integers(0, len(provider_pool)))]
        card_body = "4" + "".join(str(int(rng.integers(0, 10))) for _ in range(14))
        rows.append(
            {
                "patient_id": f"PAT{i + 1:06d}",
                "provider_id": provider_id,
                "card_no": _make_luhn_valid(card_body),
                "region": REGIONS[int(rng.integers(0, len(REGIONS)))],
            }
        )

    # --- Phase 2: quarantine patients (last INVALID_LUHN_COUNT rows) --------
    for j in range(INVALID_LUHN_COUNT):
        i = n_valid + j
        provider_id = provider_pool[int(rng.integers(0, len(provider_pool)))]
        card_body = "4" + "".join(str(int(rng.integers(0, 10))) for _ in range(14))
        rows.append(
            {
                "patient_id": f"PAT{i + 1:06d}",
                "provider_id": provider_id,
                "card_no": _make_luhn_invalid(card_body),
                "region": REGIONS[int(rng.integers(0, len(REGIONS)))],
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == PATIENT_COUNT, f"Expected {PATIENT_COUNT} patients, got {len(df)}"
    verify_fingerprint(df, _PATIENTS_FINGERPRINT, label="patients")
    return df


# ---------------------------------------------------------------------------
# build_charge_snapshot -- distribution-snapshot/v1 for the statistical columns
# ---------------------------------------------------------------------------


def build_charge_snapshot(seed: int = 45) -> dict[str, Any]:
    """Return a distribution-snapshot/v1 dict for the `visits` statistical columns.

    Built from a deterministic synthetic source frame (a normal charge_amount
    and a two-year admit_ts date range) via compute_distribution_snapshot. The
    runner writes this dict to a temp JSON file and substitutes its path into
    the manifest config (see testflight/_builder.build_snapshot_files); the
    `statistical` sampler then reproduces this shape (numeric inverse-CDF for
    charge_amount; year-bin + uniform-within-year for admit_ts).

    Determinism: same seed + pinned numpy -> byte-identical snapshot, so the
    sampled columns are stable across runs (and the cross-process fingerprint
    guard catches any drift).
    """
    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    rng = make_rng(seed + 7)
    n = 5000
    src = pd.DataFrame(
        {
            "charge_amount": rng.normal(340.0, 90.0, size=n).round(2),
            "admit_ts": pd.to_datetime("2023-01-01")
            + pd.to_timedelta(rng.integers(0, 730, size=n), unit="D"),
        }
    )
    snapshot: dict[str, Any] = compute_distribution_snapshot(src)
    return snapshot
