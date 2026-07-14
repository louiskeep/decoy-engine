"""Job E seeded fixture generator: hostile / edge-case data (TH-3.3 / P1-10).

Builds three mask-table source frames -- people, accounts, singleton -- plus
the manifest declares a fourth, standalone GENERATE table (empty_table,
row_count=0; no fixture builder needed).

Planted edge cases (match manifest.yaml invariants exactly):
  - KANA_NAMES: a fixed pool of romanized (ASCII) Japanese names. Every
    character is in-charset for the ALPHANUM charset (with a space separator),
    so `kana_name` is FPE-permuted and round-trips. DE-01 cluster-C (2026-07-14)
    replaced the former fully-non-ASCII names, whose all-out-of-charset path is
    now fail-closed; that behavior is covered by a dedicated negative test.
  - SENTINEL_PHONE = "800-555-0187" embedded in a unicode sentence in the
    `notes` column of person row 0. text_mask (us_phone detector) must
    redact it; the sentinel scan checks absence from all output.
  - SENTINEL_EMAIL = "sentinel.leak.test@e-hostile-decoy-testflight.invalid"
    embedded in a unicode sentence in the `bio` column of person row 0.
    text_redact (email detector) must replace it with "[REDACTED]".
  - `middle_name`: every value is null (all-null column shape).
  - DUPLICATE_FK_COUNT = 40 of the 150 `accounts` rows reference the SAME
    single "hub" person_id (HUB_PERSON_IDX) -- a heavy duplicate-FK spike,
    not the varied fan-out already exercised by Jobs A-D. Not an orphan: the
    hub person_id is a real row in `people`.
  - `singleton`: a single-row (row_count=1) mask table.

Row counts (from manifest.yaml):
  - people: 60
  - accounts: 150 (40 of which share HUB_PERSON_IDX's person_id)
  - singleton: 1

Source format notes:
  - person_id: "PP{n:05d}" (FPE PK; digits charset).
  - kana_name: a romanized (ASCII) Japanese full name (see KANA_NAMES).
  - notes: a unicode sentence containing an embedded US-phone-shaped span.
  - bio: a unicode sentence containing an embedded email-shaped span.
  - middle_name: always null (object dtype, all None).
  - account_id: "AC{n:06d}" (FPE PK; digits charset).
  - person_id (accounts): FK to people.person_id.
  - balance: float.
  - singleton_code: "SG{n:04d}" (FPE PK; digits charset), one row.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from testflight._fixtures import make_rng, verify_fingerprint

# ---------------------------------------------------------------------------
# Constants (match manifest.yaml exactly)
# ---------------------------------------------------------------------------

PEOPLE_COUNT = 60
ACCOUNTS_COUNT = 150
SINGLETON_COUNT = 1

# 40 of the 150 accounts rows reference the SAME hub person_id (index 0 in the
# people pool) -- a heavy duplicate-FK spike distinct from ordinary fan-out.
DUPLICATE_FK_COUNT = 40
HUB_PERSON_IDX = 0

SENTINEL_PHONE = "800-555-0187"
SENTINEL_EMAIL = "sentinel.leak.test@e-hostile-decoy-testflight.invalid"
SENTINEL_PERSON_IDX = 0  # planted in the FIRST people row (notes AND bio)

# Fixed pool of romanized (ASCII) Japanese names. A plain list (not a set), so
# iteration/index order is process-stable regardless of PYTHONHASHSEED --
# required for the cross-process determinism fingerprint gate. Every character is
# in-charset for the ALPHANUM charset (letters) with a space separator, so
# `name` is FPE-permuted and round-trips reversibly.
#
# DE-01 cluster-C (2026-07-14): these were fully non-ASCII (Japanese) names that
# had ZERO in-charset characters and relied on the all-out-of-charset
# covering-hash path. That path is now fail-closed (it was non-invertible: a
# column sold as reversible silently did not round-trip). The fully-non-ASCII
# fail-closed behavior is now asserted by a dedicated negative test
# (tests/unit/execution/test_fpe_fail_closed_pipeline.py); job E keeps unicode
# coverage via the `notes`/`bio` text_mask/text_redact columns (unicode prose
# with embedded ASCII PII spans), which are unaffected by the FPE change.
KANA_NAMES: list[str] = [
    "Tanaka Taro",
    "Suzuki Hanako",
    "Sato Jiro",
    "Takahashi Saburo",
    "Watanabe Shiro",
    "Ito Goro",
    "Yamamoto Rokuro",
    "Nakamura Nanami",
    "Kobayashi Yae",
    "Kato Kokonoe",
    "Yoshida Towa",
    "Yamada Hitomi",
]

# Source fingerprints (SHA-256 of canonical CSV; verified by verify_fingerprint
# inside each fixture build_* function -- update after any fixture change).
# DE-01 cluster-C (2026-07-14): re-fingerprinted after KANA_NAMES became
# romanized (ASCII) so kana_name FPE-permutes and round-trips instead of hitting
# the now-fail-closed all-out-of-charset covering-hash path.
_PEOPLE_FINGERPRINT = "4f4b25e07d0aa5418eb8ba5b9c93b4cf5eabb679c8bd1663f3f988b61f60288f"
_ACCOUNTS_FINGERPRINT = "3e35fb3b1950af73b0adeefb00317ea771b562a9ac86fa95ff373298519f83ff"
_SINGLETON_FINGERPRINT = "5976c8a6c03141c653c7dbbc009b720b4fd937c010631195586f36fe398bc530"


# ---------------------------------------------------------------------------
# build_people
# ---------------------------------------------------------------------------


def build_people(seed: int = 46) -> pd.DataFrame:
    """Build the people source DataFrame (FK parent; unicode + all-null shapes).

    Args:
        seed: Reproducibility seed (matches manifest.seed).

    Returns:
        pandas DataFrame with columns: person_id, kana_name, notes, bio,
        middle_name.
    """
    rng = make_rng(seed)
    rows: list[dict[str, Any]] = []
    for i in range(PEOPLE_COUNT):
        kana_name = KANA_NAMES[int(rng.integers(0, len(KANA_NAMES)))]

        if i == SENTINEL_PERSON_IDX:
            phone = SENTINEL_PHONE
            email = SENTINEL_EMAIL
        else:
            phone = (
                f"{int(rng.integers(200, 999))}-{int(rng.integers(200, 999))}-"
                f"{int(rng.integers(1000, 9999))}"
            )
            email = f"contact{i:03d}@example-decoy-testflight.invalid"

        # Unicode sentences (Japanese / Cyrillic-French mix) with an embedded
        # ASCII PII span. text_mask/text_redact operate on raw Python strings
        # via regex span detection, unaffected by the surrounding script.
        notes = f"お客様情報: 電話番号は{phone}です。ご連絡ください。"
        bio = f"Клиент связался по адресу {email}. Merci beaucoup, château."

        rows.append(
            {
                "person_id": f"PP{i + 1:05d}",
                "kana_name": kana_name,
                "notes": notes,
                "bio": bio,
                # All-null column shape (TH-3.3): every row is null.
                "middle_name": None,
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == PEOPLE_COUNT, f"Expected {PEOPLE_COUNT} people, got {len(df)}"
    assert df["middle_name"].isna().all(), "middle_name must be all-null (TH-3.3 shape)"
    verify_fingerprint(df, _PEOPLE_FINGERPRINT, label="people")
    return df


# ---------------------------------------------------------------------------
# build_accounts
# ---------------------------------------------------------------------------


def build_accounts(seed: int = 46, people_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build the accounts source DataFrame (FK child; duplicate-FK spike).

    Row layout:
      [0 .. DUPLICATE_FK_COUNT - 1]         all reference the HUB person_id.
      [DUPLICATE_FK_COUNT .. ACCOUNTS_COUNT - 1]  reference a person_id drawn
          uniformly from the full people pool (ordinary fan-out, may include
          the hub again incidentally).

    Args:
        seed: Reproducibility seed.
        people_df: Optional pre-built people frame; if None it is built.

    Returns:
        pandas DataFrame with columns: account_id, person_id, balance.
    """
    rng = make_rng(seed + 1)  # offset so accounts rng diverges from people

    if people_df is None:
        people_df = build_people(seed)
    person_pool = people_df["person_id"].tolist()
    hub_person_id = person_pool[HUB_PERSON_IDX]

    rows: list[dict[str, Any]] = []

    # --- Phase 1: DUPLICATE_FK_COUNT rows, all referencing the hub. ---------
    for i in range(DUPLICATE_FK_COUNT):
        rows.append(
            {
                "account_id": f"AC{i + 1:06d}",
                "person_id": hub_person_id,
                "balance": round(float(rng.uniform(0, 10_000)), 2),
            }
        )

    # --- Phase 2: ordinary fan-out over the remaining rows. -----------------
    n_ordinary = ACCOUNTS_COUNT - DUPLICATE_FK_COUNT
    for j in range(n_ordinary):
        i = DUPLICATE_FK_COUNT + j
        person_id = person_pool[int(rng.integers(0, len(person_pool)))]
        rows.append(
            {
                "account_id": f"AC{i + 1:06d}",
                "person_id": person_id,
                "balance": round(float(rng.uniform(0, 10_000)), 2),
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == ACCOUNTS_COUNT, f"Expected {ACCOUNTS_COUNT} accounts, got {len(df)}"
    hub_count = int((df["person_id"] == hub_person_id).sum())
    assert hub_count >= DUPLICATE_FK_COUNT, (
        f"Expected at least {DUPLICATE_FK_COUNT} accounts referencing the hub "
        f"person_id, got {hub_count}."
    )
    verify_fingerprint(df, _ACCOUNTS_FINGERPRINT, label="accounts")
    return df


# ---------------------------------------------------------------------------
# build_singleton
# ---------------------------------------------------------------------------


def build_singleton(seed: int = 46) -> pd.DataFrame:
    """Build the singleton source DataFrame (single-row table shape, TH-3.3).

    Args:
        seed: Reproducibility seed.

    Returns:
        pandas DataFrame with exactly ONE row, column: singleton_code.
    """
    rng = make_rng(seed + 2)
    df = pd.DataFrame(
        {
            "singleton_code": [f"SG{int(rng.integers(0, 10_000)):04d}"],
        }
    )
    assert len(df) == SINGLETON_COUNT, f"Expected {SINGLETON_COUNT} row, got {len(df)}"
    verify_fingerprint(df, _SINGLETON_FINGERPRINT, label="singleton")
    return df
