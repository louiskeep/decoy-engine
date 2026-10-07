"""The shipped reference tables: the one registry a bare table name must be in.

A bare `joint_mask.reference` name is a lookup key, never a path. Before this registry the
loader joined any bare name onto the data directory, so `../../x` or an absolute path read an
arbitrary Parquet file (`reference: ../../x`). A name outside the registry is now refused
without touching the filesystem; `customer:<locator>` is the only path form and always goes
through the file-access policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Directory holding the shipped Parquet files (package data).
DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class ShippedTable:
    name: str
    # Checked against the file's `decoy_table_version` metadata; a mismatch warns.
    version: str
    # Offered to `joint_mask` out of the box.
    joint_mask: bool = True


SHIPPED_TABLES: dict[str, ShippedTable] = {
    t.name: t
    for t in (
        ShippedTable("us_zip5_city_state", "1.0"),
        ShippedTable("vehicle_make_model_year", "1.0"),
        # Restricted ZIP3 prefix list for the geo_generalize Safe Harbor cascade
        # (45 CFR 164.514(b)(2)(i)(B)); not a joint_mask reference.
        ShippedTable("us_zip3_population", "1.0", joint_mask=False),
        # FDA NDC Database (public domain), abbreviated seed set; operators swap in a full
        # export through the `customer:` prefix.
        ShippedTable("ndc_labeler_drug_strength", "1.0"),
        # ISO 18245 merchant category codes, abbreviated seed set.
        ShippedTable("mcc_category_description", "1.0"),
    )
}

JOINT_MASK_TABLES: frozenset[str] = frozenset(
    name for name, table in SHIPPED_TABLES.items() if table.joint_mask
)


def shipped_path(name: str) -> Path:
    """The package-data file of a registered shipped table. `name` must be in the registry."""
    return DATA_DIR / f"{SHIPPED_TABLES[name].name}.parquet"
