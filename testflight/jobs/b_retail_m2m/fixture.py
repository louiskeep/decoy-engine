"""Job B seeded fixture generator: retail / payments (many-to-many junction).

Builds three tables -- customers, products, orders -- with EXACT planted
edge-case counts so the invariant assertions compare against known integers.

Topology: many_to_many_junction.
  orders.customer_id  -> customers.customer_id  (customer_identity relationship)
  orders.product_id   -> products.product_id    (product_identity relationship)
Each orders row is a JUNCTION carrying TWO FK relationships to two distinct
parent tables. The M2M FK invariant asserts every junction row resolves to a
valid masked customer AND a valid masked product.

Planted edge cases (match manifest.yaml invariants exactly):
  - RESTRICTED_ZIP3_COUNT = 15 customer rows with HHS-restricted ZIP3 prefixes
    for Safe Harbor suppression testing.
  - ORPHAN_CUSTOMER_ORDER_COUNT = 7 orders referencing fictional customer_ids
    not present in the customers table. orphan_policy:warn for customer_identity.
  - ORPHAN_PRODUCT_ORDER_COUNT = 0 orders with invalid product_id.
    orphan_policy:warn for product_identity with 0 expected orphans.
  - SENTINEL_PAN: specific PAN value in customer row 0. FPE+luhn transforms it.
  - SENTINEL_EMAIL: specific email in customer row 0. text_redact masks it.

Correlations (deliberate, non-trivial for the distribution invariant):
  - qty_band and order_total_band are CORRELATED by fixture construction:
    products have a category-driven unit_price range (electronics is high,
    food is low). High unit_price -> high order_total for any qty -> "high"
    order_total_band. Low unit_price -> "low" order_total_band regardless of
    qty. Within a category, qty and order_total scale together. This produces
    a strong joint (qty_band, order_total_band) correlation (sim >= 0.90).
  - Both qty_band and order_total_band are PASSTHROUGH in the pipeline, so
    their values are preserved exactly. Joint similarity = 1.0 in a faithful
    run. A regression where the M2M masking pipeline accidentally corrupts a
    passthrough column (treating it as an FK column) would destroy this
    correlation; the correlation tooth catches it.

Row counts (from manifest.yaml):
  - customers: 3000
  - products: 500
  - orders: 12000 + ORPHAN_CUSTOMER_ORDER_COUNT = 12007

Source format notes:
  - customer_id: "CU{n:06d}"
  - pan: 16-digit Luhn-valid string (Visa-like prefix 4)
  - email: email string (e.g. "user@domain.com")
  - city: string city name
  - state: 2-char US state abbreviation
  - zip5: 5-digit string; 15 rows use HHS-restricted ZIP3 prefixes
  - signup_date: ISO date string YYYY-MM-DD
  - segment: "budget" / "standard" / "premium"
  - product_id: "PR{n:05d}"
  - category: "electronics" / "clothing" / "food" / "home" / "sports"
  - mcc: 4-digit MCC code from the shipped corpus
  - order_id: "OR{n:07d}"
  - qty: int 1..8
  - unit_price: float correlated with category
  - qty_band: "low" / "mid" / "high" (passthrough; correlated with order_total_band)
  - order_total_band: "low" / "mid" / "high" (passthrough; correlated with qty_band)
  - order_total: placeholder (derived by engine from qty * unit_price)
  - tier: placeholder (derived case_when over order_total)
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import stdnum.luhn as _luhn

from testflight._fixtures import make_rng, verify_fingerprint

# ---------------------------------------------------------------------------
# Constants (match manifest.yaml exactly)
# ---------------------------------------------------------------------------

CUSTOMER_COUNT = 3000
PRODUCT_COUNT = 500
ORDER_COUNT = 12000

# Orphan orders: reference fictional customer IDs not in customers table.
ORPHAN_CUSTOMER_ORDER_COUNT = 7
ORPHAN_PRODUCT_ORDER_COUNT = 0  # no orphan product orders planted

# HHS-restricted ZIP3 prefixes -> suppressed by geo_generalize (Safe Harbor).
RESTRICTED_ZIP3_COUNT = 15

# Sentinel strings that MUST NOT appear in any output column after masking.
SENTINEL_PAN = "4111111111111110"  # Visa test-like PAN; Luhn-valid (check digit 0)
SENTINEL_EMAIL = "sentinel.leak.test@b-retail-decoy-testflight.invalid"
SENTINEL_CUSTOMER_IDX = 0  # planted in the FIRST customer row

# Source fingerprints (SHA-256 of canonical CSV; computed on first run).
# Re-baseline deliberately when the fixture generator changes.
_CUSTOMERS_FINGERPRINT = "e3c28d3e27dbc27d24adc53370cbe2c4e490b6b562e70ec483c9dcf20f29dd1e"
_PRODUCTS_FINGERPRINT = "8b5f2816591cd58d88566b6ef5d26c41e479c591fab7a6f55df841eb61da5d34"
_ORDERS_FINGERPRINT = "8cc526d6c5749f56a343768664e6461bc0b422820b9be4110b2666918eaf325d"

# HHS-restricted ZIP3 prefixes (same set as Job A).
_RESTRICTED_ZIP3_PREFIXES = [
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
]

# Product categories with associated unit_price ranges (drives correlation).
# Electronics and home are high-price; food is low-price; others are mid.
_CATEGORIES = ["electronics", "clothing", "food", "home", "sports"]
_CATEGORY_WEIGHTS = [0.20, 0.25, 0.20, 0.20, 0.15]

# unit_price range per category (min, max).
_UNIT_PRICE_RANGES: dict[str, tuple[float, float]] = {
    "electronics": (200.0, 1200.0),
    "clothing": (20.0, 150.0),
    "food": (3.0, 30.0),
    "home": (50.0, 500.0),
    "sports": (25.0, 200.0),
}

# order_total_band thresholds.
_ORDER_TOTAL_LOW = 50.0
_ORDER_TOTAL_HIGH = 300.0

# Customer segments.
_SEGMENTS = ["budget", "standard", "premium"]
_SEGMENT_WEIGHTS = [0.30, 0.50, 0.20]

# US state abbreviations for customer city/state/zip.
_US_STATES = [
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
]

# US city names (sample pool for the fixture).
_US_CITIES = [
    "New York",
    "Los Angeles",
    "Chicago",
    "Houston",
    "Phoenix",
    "Philadelphia",
    "San Antonio",
    "San Diego",
    "Dallas",
    "San Jose",
    "Austin",
    "Jacksonville",
    "Fort Worth",
    "Columbus",
    "Charlotte",
    "Indianapolis",
    "San Francisco",
    "Seattle",
    "Denver",
    "Nashville",
    "Oklahoma City",
    "El Paso",
    "Las Vegas",
    "Louisville",
    "Memphis",
    "Portland",
    "Baltimore",
    "Milwaukee",
    "Albuquerque",
    "Tucson",
    "Fresno",
    "Sacramento",
    "Mesa",
    "Omaha",
    "Cleveland",
    "Raleigh",
    "Colorado Springs",
    "Long Beach",
    "Virginia Beach",
    "Minneapolis",
    "Tampa",
    "New Orleans",
    "Arlington",
    "Wichita",
    "Bakersfield",
    "Aurora",
    "Anaheim",
    "Santa Ana",
    "Corpus Christi",
]


# ---------------------------------------------------------------------------
# Helper: Luhn-valid PAN generation
# ---------------------------------------------------------------------------


def _make_luhn_valid(body: str) -> str:
    """Return body + valid Luhn check digit."""
    return body + _luhn.calc_check_digit(body)


# ---------------------------------------------------------------------------
# Helper: qty_band and order_total_band
# ---------------------------------------------------------------------------


def _qty_band(qty: int) -> str:
    if qty <= 2:
        return "low"
    if qty <= 5:
        return "mid"
    return "high"


def _order_total_band(total: float) -> str:
    if total < _ORDER_TOTAL_LOW:
        return "low"
    if total < _ORDER_TOTAL_HIGH:
        return "mid"
    return "high"


# ---------------------------------------------------------------------------
# build_customers
# ---------------------------------------------------------------------------


def build_customers(seed: int = 43) -> pd.DataFrame:
    """Build the customers source DataFrame with exact planted edge cases.

    Row layout:
      [0]                         sentinel customer (PAN + email sentinel planted)
      [1 .. CUSTOMER_COUNT-1]     regular customers (15 with restricted ZIP3)

    Args:
        seed: Reproducibility seed (matches manifest.seed).

    Returns:
        pandas DataFrame with columns:
          customer_id, pan, email, city, state, zip5, signup_date, segment.
    """
    rng = make_rng(seed)

    # Choose which row indices will have restricted ZIP3 prefixes.
    # Exclude row 0 (sentinel row) from restricted positions for clarity.
    non_sentinel = list(range(1, CUSTOMER_COUNT))
    restricted_positions = set(
        rng.choice(len(non_sentinel), size=RESTRICTED_ZIP3_COUNT, replace=False).tolist()
    )
    # Map from choice index to actual row index.
    restricted_row_indices = {non_sentinel[i] for i in restricted_positions}

    rows: list[dict[str, Any]] = []
    for i in range(CUSTOMER_COUNT):
        customer_id = f"CU{i + 1:06d}"

        # Sentinel row: plant known PAN and email.
        if i == SENTINEL_CUSTOMER_IDX:
            pan = SENTINEL_PAN
            email = SENTINEL_EMAIL
        else:
            # Visa-like PAN (prefix 4, 15-digit body + Luhn check).
            pan_body = "4" + "".join(str(int(rng.integers(0, 10))) for _ in range(14))
            pan = _make_luhn_valid(pan_body)
            # Simple email pattern (no real faker; seeded via rng for determinism).
            username_len = int(rng.integers(4, 12))
            username = "u" + str(int(rng.integers(1000, 999999))).zfill(6)[:username_len]
            domains = ["example.com", "test-domain.net", "sample.org", "fixture.io"]
            domain = domains[int(rng.integers(0, len(domains)))]
            email = f"{username}@{domain}"

        # ZIP5 and city/state.
        if i in restricted_row_indices:
            # Use one of the restricted ZIP3 prefixes.
            sorted_restricted = sorted(restricted_row_indices)
            rank = sorted_restricted.index(i)
            z3 = _RESTRICTED_ZIP3_PREFIXES[rank % len(_RESTRICTED_ZIP3_PREFIXES)]
            zip5 = z3 + str(int(rng.integers(0, 100))).zfill(2)
            # Pair with a plausible state (not critical for the test).
            state = _US_STATES[int(rng.integers(0, len(_US_STATES)))]
        else:
            z3 = str(int(rng.integers(100, 900)))
            while z3 in _RESTRICTED_ZIP3_PREFIXES:
                z3 = str(int(rng.integers(100, 900)))
            zip5 = z3 + str(int(rng.integers(0, 100))).zfill(2)
            state = _US_STATES[int(rng.integers(0, len(_US_STATES)))]

        city = _US_CITIES[int(rng.integers(0, len(_US_CITIES)))]

        # signup_date: random date in last 5 years.
        years_ago = int(rng.integers(0, 5))
        month = int(rng.integers(1, 13))
        day = int(rng.integers(1, 29))
        signup_date = f"{2026 - years_ago:04d}-{month:02d}-{day:02d}"

        # segment: weighted random.
        seg_draw = float(rng.uniform(0, 1))
        if seg_draw < 0.30:
            segment = "budget"
        elif seg_draw < 0.80:
            segment = "standard"
        else:
            segment = "premium"

        rows.append(
            {
                "customer_id": customer_id,
                "pan": pan,
                "email": email,
                "city": city,
                "state": state,
                "zip5": zip5,
                "signup_date": signup_date,
                "segment": segment,
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == CUSTOMER_COUNT, f"Expected {CUSTOMER_COUNT} customers, got {len(df)}"
    verify_fingerprint(df, _CUSTOMERS_FINGERPRINT, label="customers")
    return df


# ---------------------------------------------------------------------------
# build_products
# ---------------------------------------------------------------------------


def build_products(seed: int = 43) -> pd.DataFrame:
    """Build the products source DataFrame.

    Products have a category (categorical) and mcc (code_set mcc). The
    category determines the unit_price range used when building orders.

    Args:
        seed: Reproducibility seed.

    Returns:
        pandas DataFrame with columns:
          product_id, category, mcc.
    """
    rng = make_rng(seed + 100)  # offset seed

    mcc_codes = _load_mcc_corpus()

    rows: list[dict[str, Any]] = []
    for i in range(PRODUCT_COUNT):
        product_id = f"PR{i + 1:05d}"

        # Category: weighted random.
        cat_draw = float(rng.uniform(0, 1))
        cumulative = 0.0
        category = _CATEGORIES[-1]
        for cat, wt in zip(_CATEGORIES, _CATEGORY_WEIGHTS, strict=True):
            cumulative += wt
            if cat_draw < cumulative:
                category = cat
                break

        # MCC code: random from corpus.
        mcc = mcc_codes[int(rng.integers(0, len(mcc_codes)))]

        rows.append(
            {
                "product_id": product_id,
                "category": category,
                "mcc": mcc,
            }
        )

    df = pd.DataFrame(rows)
    assert len(df) == PRODUCT_COUNT, f"Expected {PRODUCT_COUNT} products, got {len(df)}"
    verify_fingerprint(df, _PRODUCTS_FINGERPRINT, label="products")
    return df


# ---------------------------------------------------------------------------
# build_orders
# ---------------------------------------------------------------------------


def build_orders(
    seed: int = 43,
    customers_df: pd.DataFrame | None = None,
    products_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the orders junction source DataFrame.

    Each order references a customer_id (FK to customers) AND a product_id
    (FK to products), forming the M2M junction. The fixture plants:
    - Exact ORPHAN_CUSTOMER_ORDER_COUNT orders with fictional customer_ids.
    - ZERO orphan product orders (all product_ids are valid).
    - Deliberate correlation: qty_band and order_total_band are correlated
      via the category-driven unit_price model.

    Args:
        seed: Reproducibility seed.
        customers_df: Optional pre-built customers DataFrame.
        products_df: Optional pre-built products DataFrame.

    Returns:
        pandas DataFrame with columns:
          order_id, customer_id, product_id, qty, unit_price,
          qty_band, order_total_band.
        (order_total and tier are derived by the engine pipeline; the fixture
         uses placeholders 0.0 so the engine's derived strategy computes them.)
    """
    rng = make_rng(seed + 200)  # offset seed

    if customers_df is None:
        customers_df = build_customers(seed)
    if products_df is None:
        products_df = build_products(seed)

    valid_customer_ids = customers_df["customer_id"].tolist()
    valid_product_ids = products_df["product_id"].tolist()
    # Build a product_id -> category mapping for unit_price range lookup.
    product_category: dict[str, str] = dict(
        zip(products_df["product_id"].tolist(), products_df["category"].tolist(), strict=True)
    )

    rows: list[dict[str, Any]] = []

    # --- Regular orders (valid FK references) ---
    for i in range(ORDER_COUNT):
        order_id = f"OR{i + 1:07d}"

        customer_idx = int(rng.integers(0, CUSTOMER_COUNT))
        customer_id = valid_customer_ids[customer_idx]

        product_idx = int(rng.integers(0, PRODUCT_COUNT))
        product_id = valid_product_ids[product_idx]
        category = product_category[product_id]

        qty = int(rng.integers(1, 9))  # 1..8
        price_lo, price_hi = _UNIT_PRICE_RANGES[category]
        unit_price = round(float(rng.uniform(price_lo, price_hi)), 2)

        order_total_src = round(qty * unit_price, 2)
        qty_b = _qty_band(qty)
        total_b = _order_total_band(order_total_src)

        rows.append(
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "product_id": product_id,
                "qty": qty,
                "unit_price": unit_price,
                "qty_band": qty_b,
                "order_total_band": total_b,
            }
        )

    # --- Orphan customer orders (fictional customer_ids) ---
    # These reference customer_ids in range "CU099901".."CU099907" which are
    # NOT present in the customers table (which only goes up to CU003000).
    # They become orphans under orphan_policy:warn for customer_identity.
    for k in range(ORPHAN_CUSTOMER_ORDER_COUNT):
        order_id = f"OR{ORDER_COUNT + k + 1:07d}"
        # Fictional customer ID (out of valid range).
        orphan_customer_id = f"CU{99900 + k + 1:06d}"
        product_idx = int(rng.integers(0, PRODUCT_COUNT))
        product_id = valid_product_ids[product_idx]
        category = product_category[product_id]

        qty = int(rng.integers(1, 9))
        price_lo, price_hi = _UNIT_PRICE_RANGES[category]
        unit_price = round(float(rng.uniform(price_lo, price_hi)), 2)
        order_total_src = round(qty * unit_price, 2)

        rows.append(
            {
                "order_id": order_id,
                "customer_id": orphan_customer_id,
                "product_id": product_id,
                "qty": qty,
                "unit_price": unit_price,
                "qty_band": _qty_band(qty),
                "order_total_band": _order_total_band(order_total_src),
            }
        )

    df = pd.DataFrame(rows)
    expected_total = ORDER_COUNT + ORPHAN_CUSTOMER_ORDER_COUNT
    assert len(df) == expected_total, f"Expected {expected_total} orders, got {len(df)}"
    verify_fingerprint(df, _ORDERS_FINGERPRINT, label="orders")
    return df


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------


def _load_mcc_corpus() -> list[str]:
    """Return MCC code list from the shipped MCC corpus."""
    from pathlib import Path

    import pyarrow.parquet as pq

    corpus_path = (
        Path(__file__).parent.parent.parent.parent
        / "src"
        / "decoy_engine"
        / "codesets"
        / "mcc.parquet"
    )
    tbl = pq.read_table(str(corpus_path))
    return tbl.column("code").to_pylist()
