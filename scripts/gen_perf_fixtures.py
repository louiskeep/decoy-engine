"""Generate the PERF.BASE.2 fixture suite.

Source pattern:

- Faker for synthetic PII (https://faker.readthedocs.io/). Established
  standard; we use it directly here rather than going through the full
  ``decoy_engine.generators.ColumnGenerator`` pipeline because the fixtures
  are static build artifacts, not jobs. Reproducibility comes from
  ``Faker.seed_instance`` + ``numpy.random.default_rng`` + ``random.Random``,
  all seeded from the per-tier ``TierSpec.seed`` (see
  ``tests/perf_fixtures/schema.py``).
- Parquet via pandas / pyarrow as the output format -- matches the
  substrate change PERF.BASE.3 measures against (the Polars/DuckDB
  candidates both consume Parquet natively).

Usage::

    python scripts/gen_perf_fixtures.py small
    python scripts/gen_perf_fixtures.py medium
    python scripts/gen_perf_fixtures.py large    # ~5-10 GB, ~minutes
    python scripts/gen_perf_fixtures.py all

Output goes to ``tests/perf_fixtures/<tier>/data.parquet`` plus a
companion ``fixture.yaml`` describing the schema + generation seed.

Determinism contract:

- Same Decoy engine version + same tier name => byte-identical Parquet.
- Verified by ``tests/perf_fixtures/test_fixture_reproducibility.py``.
- Cross-version drift is permitted (e.g. Faker upgrades shift values);
  regenerate the committed Parquet when the engine bumps Faker.

The script is intentionally CLI-shaped (argparse, prints, exit codes)
rather than a library so the engine code never imports it. Tests
import ``tests/perf_fixtures/schema.py`` + ``tests/perf_fixtures/
loaders.py`` only.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
import textwrap
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from faker import Faker

# Allow ``python scripts/gen_perf_fixtures.py`` from the repo root --
# tests/perf_fixtures/schema.py is the source of truth for what each
# tier contains.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from perf_fixtures.schema import (  # noqa: E402  (path-modified import)
    TIERS,
    TierSpec,
    get_tier,
)

# Output directory for committed + uncommitted fixtures.
_FIXTURES_DIR = _REPO_ROOT / "tests" / "perf_fixtures"


# ---------------------------------------------------------------------------
# Column generators -- one function per ColumnSpec.kind.
#
# Each takes:
#   rows: int -- how many values to emit
#   rng: numpy.random.Generator -- for numeric / index draws
#   faker: faker.Faker -- for string / date draws (already seeded)
#   pyrand: random.Random -- for Python-native choice() calls
#
# Generators MUST be deterministic given the seeded RNGs above.
# Generators MUST NOT call faker.seed_instance / np.random.seed mid-run;
# all seeding happens in build_tier() before any kind handler runs.
# ---------------------------------------------------------------------------


_STATUS_VALUES = ("active", "inactive", "pending", "closed")
_CATEGORY_VALUES = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
_TIER_VALUES = ("bronze", "silver", "gold", "platinum")
_STATE_ABBR = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
)
_FILLER_CAT_VALUES = ("A", "B", "C", "D")


def _gen_id_int(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    # Sequential int64 IDs. Deterministic, distinct, dense.
    return np.arange(1, rows + 1, dtype=np.int64)


def _gen_ssn(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.ssn() for _ in range(rows)]


def _gen_email(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.email() for _ in range(rows)]


def _gen_phone(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.phone_number() for _ in range(rows)]


def _gen_full_name(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.name() for _ in range(rows)]


def _gen_first_name(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.first_name() for _ in range(rows)]


def _gen_street_address(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.street_address() for _ in range(rows)]


def _gen_apt_number(
    rows: int, rng: np.random.Generator, pyrand: random.Random, **_: Any
) -> list[str]:
    return [f"Apt {pyrand.randint(1, 999)}" for _ in range(rows)]


def _gen_city(rows: int, faker: Faker, **_: Any) -> list[str]:
    return [faker.city() for _ in range(rows)]


def _gen_state_abbr(rows: int, pyrand: random.Random, **_: Any) -> list[str]:
    return [pyrand.choice(_STATE_ABBR) for _ in range(rows)]


def _gen_zip5(rows: int, rng: np.random.Generator, **_: Any) -> list[str]:
    arr = rng.integers(low=10_000, high=99_999, size=rows, dtype=np.int64)
    return [f"{v:05d}" for v in arr.tolist()]


def _gen_date_past(rows: int, faker: Faker, **_: Any) -> list[pd.Timestamp]:
    # DOB-shaped distribution: roughly 18-80 years old.
    return [pd.Timestamp(faker.date_of_birth(minimum_age=18, maximum_age=80)) for _ in range(rows)]


def _gen_timestamp_recent(rows: int, rng: np.random.Generator, **_: Any) -> pd.DatetimeIndex:
    # Spread of timestamps over the last 3 years from an arbitrary
    # anchor. Anchor is fixed so the spread is deterministic across
    # runs even if the wall clock moves.
    anchor = pd.Timestamp("2026-01-01T00:00:00")
    seconds_back = rng.integers(low=0, high=3 * 365 * 24 * 3600, size=rows)
    return pd.DatetimeIndex([anchor - pd.Timedelta(seconds=int(s)) for s in seconds_back])


def _gen_amount_float(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    # Log-normal-ish distribution; clamped to 2 decimal places to look
    # like real currency amounts.
    raw = rng.lognormal(mean=5.0, sigma=1.5, size=rows)
    return np.round(raw, 2)


def _gen_score_int(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    # Credit-score-shaped: 300-850 range.
    return rng.integers(low=300, high=851, size=rows, dtype=np.int64)


def _gen_count_int(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    # 0-500 count metric.
    return rng.integers(low=0, high=501, size=rows, dtype=np.int64)


def _gen_filler_int(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    return rng.integers(low=0, high=1_000_000, size=rows, dtype=np.int64)


def _gen_filler_float(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    return np.round(rng.standard_normal(size=rows) * 100.0, 4)


def _gen_filler_cat(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    idx = rng.integers(low=0, high=len(_FILLER_CAT_VALUES), size=rows)
    arr = np.array(_FILLER_CAT_VALUES, dtype=object)
    return arr[idx]


def _gen_category_status(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    idx = rng.integers(low=0, high=len(_STATUS_VALUES), size=rows)
    arr = np.array(_STATUS_VALUES, dtype=object)
    return arr[idx]


def _gen_category_general(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    idx = rng.integers(low=0, high=len(_CATEGORY_VALUES), size=rows)
    arr = np.array(_CATEGORY_VALUES, dtype=object)
    return arr[idx]


def _gen_category_tier(rows: int, rng: np.random.Generator, **_: Any) -> np.ndarray:
    idx = rng.integers(low=0, high=len(_TIER_VALUES), size=rows)
    arr = np.array(_TIER_VALUES, dtype=object)
    return arr[idx]


def _gen_free_text(rows: int, faker: Faker, **_: Any) -> list[str]:
    # Two-sentence chunks; some include embedded PII so text_mask
    # strategies have something to find.
    return [faker.paragraph(nb_sentences=2) for _ in range(rows)]


_KIND_DISPATCH: dict[str, Callable[..., Any]] = {
    "id_int": _gen_id_int,
    "ssn": _gen_ssn,
    "email": _gen_email,
    "phone": _gen_phone,
    "full_name": _gen_full_name,
    "first_name": _gen_first_name,
    "street_address": _gen_street_address,
    "apt_number": _gen_apt_number,
    "city": _gen_city,
    "state_abbr": _gen_state_abbr,
    "zip5": _gen_zip5,
    "date_past": _gen_date_past,
    "timestamp_recent": _gen_timestamp_recent,
    "amount_float": _gen_amount_float,
    "score_int": _gen_score_int,
    "count_int": _gen_count_int,
    "filler_int": _gen_filler_int,
    "filler_float": _gen_filler_float,
    "filler_cat": _gen_filler_cat,
    "category_status": _gen_category_status,
    "category_general": _gen_category_general,
    "category_tier": _gen_category_tier,
    "free_text": _gen_free_text,
}


def _build_columns(
    tier: TierSpec, faker: Faker, rng: np.random.Generator, pyrand: random.Random
) -> dict[str, Any]:
    """Run the kind-dispatch for every column in the tier."""
    out: dict[str, Any] = {}
    for col in tier.columns:
        handler = _KIND_DISPATCH.get(col.kind)
        if handler is None:
            raise KeyError(
                f"no generator handler for column kind {col.kind!r} "
                f"(column {col.name!r}); add it to _KIND_DISPATCH"
            )
        out[col.name] = handler(tier.rows, rng=rng, faker=faker, pyrand=pyrand)
    return out


def _write_fixture_yaml(tier: TierSpec, out_path: Path, sha256: str) -> None:
    """Write the companion manifest describing the fixture."""
    payload = {
        "tier": tier.name,
        "rows": tier.rows,
        "seed": tier.seed,
        "column_count": len(tier.columns),
        "columns": [asdict(c) for c in tier.columns],
        "parquet_sha256": sha256,
    }
    out_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_tier(tier_name: str, *, force: bool = False) -> Path:
    """Generate one tier's Parquet + manifest. Returns the Parquet path.

    ``force`` overwrites an existing fixture; otherwise an existing file
    aborts the run with a hint (avoids accidental regeneration that
    would churn the committed bytes).
    """
    tier = get_tier(tier_name)
    out_dir = _FIXTURES_DIR / tier.name
    out_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = out_dir / "data.parquet"
    if parquet_path.exists() and not force:
        raise SystemExit(f"{parquet_path} already exists; pass --force to regenerate.")

    print(
        f"[gen-perf-fixtures] building tier={tier.name} rows={tier.rows:,} "
        f"cols={len(tier.columns)} seed={tier.seed}"
    )

    # Seed every source of randomness from the same TierSpec seed so
    # regeneration is byte-stable for a given (engine version, tier).
    faker = Faker()
    faker.seed_instance(tier.seed)
    rng = np.random.default_rng(tier.seed)
    pyrand = random.Random(tier.seed)

    columns = _build_columns(tier, faker=faker, rng=rng, pyrand=pyrand)
    df = pd.DataFrame(columns)

    # Snappy is the pandas/pyarrow default; we name it explicitly so a
    # default change does not silently churn the committed bytes.
    df.to_parquet(parquet_path, engine="pyarrow", compression="snappy", index=False)

    sha = _sha256(parquet_path)
    _write_fixture_yaml(tier, out_dir / "fixture.yaml", sha)

    size_mb = parquet_path.stat().st_size / (1024 * 1024)
    print(f"[gen-perf-fixtures] tier={tier.name} ok size={size_mb:.2f}MB sha256={sha[:16]}...")
    return parquet_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen-perf-fixtures",
        description=textwrap.dedent(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "tier",
        choices=[*sorted(TIERS), "all"],
        help="which tier to generate; 'all' runs small + medium + large in order",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing fixture instead of aborting",
    )
    args = parser.parse_args(argv)

    targets = sorted(TIERS) if args.tier == "all" else [args.tier]
    for name in targets:
        build_tier(name, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
