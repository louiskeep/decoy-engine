"""Security-boundary contract test for FORECAST.

We promise buyers (especially in healthcare and fintech) that FORECAST
never sees raw data — it operates only on the JSON statistics STORM produced.
This test introspects `recommend`'s signature and fails CI if anyone adds
a parameter accepting raw data (DataFrame, file path, connector handle, etc.).

If you have a legitimate reason to expand the signature, change this test
together with the change and document the reason here. Bypassing the test
silently would erase the promise the platform's marketing copy makes.
"""

import inspect
import typing

from decoy_engine.forecast import recommend
from decoy_engine.storm.types import StormProfile


def test_recommend_accepts_only_a_storm_profile():
    sig = inspect.signature(recommend)
    params = list(sig.parameters.values())

    # Exactly one parameter.
    assert len(params) == 1, (
        f"FORECAST signature changed: expected exactly 1 parameter, "
        f"got {len(params)}: {[p.name for p in params]}. "
        f"This may erase the FORECAST-never-sees-raw-data promise."
    )

    p = params[0]

    # Resolve annotations through typing.get_type_hints — recommender uses
    # `from __future__ import annotations` so raw .annotation values are strings.
    hints = typing.get_type_hints(recommend)
    assert hints.get(p.name) is StormProfile, (
        f"FORECAST parameter type changed: expected StormProfile, "
        f"got {hints.get(p.name)}. Adding a raw-data type (DataFrame, str, Path, "
        f"Connector, etc.) here breaks the security contract."
    )

    # The parameter is positional (so it can't be quietly default-supplied to
    # something raw-data-shaped from a config).
    assert p.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


# Defense in depth: also assert that `decoy_engine.forecast` doesn't import
# anything raw-data-shaped that could be sneaked in via a future signature
# change. (Module-level imports of pandas, sqlalchemy, file IO would be a
# strong signal that someone is planning to read raw data.)
def test_forecast_module_has_no_raw_data_imports():
    import decoy_engine.forecast.recommender as r
    forbidden = {"pandas", "sqlalchemy"}
    for mod in forbidden:
        assert not hasattr(r, mod), (
            f"decoy_engine.forecast.recommender imports {mod} — this is a strong "
            f"signal that someone is planning to read raw data from the recommender. "
            f"FORECAST must only consume the StormProfile JSON. If you need this import "
            f"for a non-data-reading reason, update this test with a comment explaining."
        )
