"""Profile construction for `_chunked.run_mask_pipeline_chunked`.

Extracted from `_chunked.py` to keep that module under the orchestration LOC
cap (`tests/sentry/test_module_size.py`); both functions here are
self-contained Profile builders with no state shared with the rest of
`_chunked.py` beyond their return value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pyarrow as pa


def first_chunk_profile(first_chunk: pa.Table, *, table: str, engine_version: str) -> Any:
    """Profile the FIRST chunk so compile_plan can build the seed envelope.

    The envelope iterates `profile.tables` (the table must exist there
    for its columns to mask at all), so a fully-empty --no-profile-style
    Profile silently masks nothing. The first chunk gives real dtypes;
    distinct counts and row_count describe only that chunk, which is
    fine -- admitted strategies consume nothing distribution-dependent.
    Faker pools size from the config-declared pool_size (the admission
    rule requires it explicitly), never from profile distinct counts,
    and the pool-capacity pre-flight lands in checks_skipped under
    no_profile=True, which is correct here: with admission restricted
    to deterministic REUSE, pool capacity is a collision-rate knob, not
    a correctness input. Epoch `profiled_at` keeps the 'not a real
    source profile' sentinel from the --no-profile path."""
    import random

    from decoy_engine.profile import Profile
    from decoy_engine.profile._walk import walk_dataframe

    table_profile = walk_dataframe(
        first_chunk.to_pandas(),
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


def empty_input_profile(config: dict[str, Any], *, table: str, engine_version: str) -> Any:
    """Placeholder Profile for a chunked source with ZERO chunks (no data at all).

    There is no real chunk to derive dtypes from, so this profiles an EMPTY
    frame built from the config's DECLARED column names only (object dtype
    placeholder). That is enough to `compile_plan(..., no_profile=True)` and
    run the DE-02 fail-closed gate (`require_mask_key`) before the
    empty-input short-circuit returns (Codex-found: the gate was skippable
    by handing a keyed job zero rows/batches). `no_profile=True` already
    treats profile-derived dtype/null-count checks as unreliable and skips
    them (see `check_null_bearing_int_unsupported`), so a placeholder here
    goes through the exact same classification path
    (`keyprovider.plan_has_keyed_strategy`) a real chunk would -- no
    duplicated keyed-strategy logic, and no behavior change to the non-empty
    path.
    """
    import random

    import pandas as pd

    from decoy_engine.profile import Profile
    from decoy_engine.profile._walk import walk_dataframe

    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    columns = [
        col.get("name")
        for col in ((table_cfg or {}).get("columns") or [])
        if isinstance(col, dict) and col.get("name")
    ]
    empty_df = pd.DataFrame({name: pd.Series([], dtype="object") for name in columns})
    table_profile = walk_dataframe(
        empty_df,
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


__all__ = ["empty_input_profile", "first_chunk_profile"]
