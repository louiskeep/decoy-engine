# Dennis Artifacts

Prepared artifacts from Dennis review sessions for use on feature branches.

## us_localities.csv

**Purpose:** Fixes S8 blocker B1. `src/decoy_engine/generation/composite/_city_state_zip.py`
requires `src/decoy_engine/generation/composite/data/us_localities.csv` to exist at package
install time. Without it, every call to `load_locality_table()` (and thus every instantiation
of `CompositeCityStateZip`) raises `FileNotFoundError`.

**How to use:** Copy to `src/decoy_engine/generation/composite/data/us_localities.csv`
on the `engine-v2/s9-execution-adapter` (or `s8`) branch and commit.

**Format:** CSV with headers `city,state,zip`. No `location_id` column. 50 rows; all
verified US municipalities with real ZIP codes from USPS public data.

**Tests that unblock:**
- `TestCityStateZip.test_deterministic_reproducible`
- `TestCityStateZip.test_all_triples_in_locality_table`
- `TestCityStateZip.test_non_deterministic_triples_are_valid`
- `TestCityStateZip.test_null_preservation`
- `TestBundlePool.*` (pool_for runs locality table load)
- `TestCompositeAdapter.test_single_column_generate_raises`
- `TestCompositeAdapter.test_single_column_generate_batch_raises`

**Next step:** After the full ~32,000-entry Census/USPS ingest (a data-population follow-up
item), replace this file with the full table. The loader is table-size-independent.
