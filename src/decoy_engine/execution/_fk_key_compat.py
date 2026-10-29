"""Compatibility wrapper for `_pandas_adapter.py`'s FK key normalization.

Split out to keep `_pandas_adapter.py` under its recorded LOC ceiling
(`tests/sentry/test_module_size.py`): a pure one-line delegate carries no
reason to live inline in an already-dense module.
"""

from __future__ import annotations

from decoy_engine.execution._fk_keys import fk_key_value


def _fk_key_value(value: object) -> object:
    """Compatibility wrapper for tests and older internal imports.

    Delegates to `decoy_engine.execution._fk_keys.fk_key_value`, the single
    normalization shared by this adapter and the out-of-core route (SC1
    port), so equal logical keys match across the int/float dtype split
    pandas introduces (an int64 parent column vs a float64-because-null child
    column read by `to_pandas()`). Both call sites in `_pandas_adapter.py`
    filter nulls before calling this, so the null/NaN sentinel branch in
    `fk_key_value` is unreachable here -- a superset, not a divergence."""
    return fk_key_value(value)
