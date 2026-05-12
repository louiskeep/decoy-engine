"""Security-boundary contract test for FORECAST.

We promise buyers (especially in healthcare and fintech) that FORECAST
never sees raw data — it operates only on the JSON statistics STORM produced.
This test introspects `recommend`'s signature and fails CI if anyone adds
a parameter accepting raw data (DataFrame, file path, connector handle, etc.).

If you have a legitimate reason to expand the signature, change this test
together with the change and document the reason here. Bypassing the test
silently would erase the promise the platform's marketing copy makes.

Allowed-list (additions that DON'T break the boundary):

  - ``ctx: ExecutionContext | None`` (Item 71, 2026-05-12). The
    ExecutionContext carries only side-channel observation hooks
    (logger + key resolvers + connector resolver). None of those carry
    raw data; the logger forwards engine-emitted events to the
    platform's JobLogger so a standalone FORECAST run shows up in the
    bottom-pane SSE stream the same way a masking job does. The
    profile is still the sole carrier of dataset information.

If you need to add another non-data parameter, append to the
ALLOWED_EXTRA_PARAMS set with a comment justifying it.
"""

import inspect
import typing

from decoy_engine.context import ExecutionContext
from decoy_engine.forecast import recommend
from decoy_engine.storm.types import StormProfile


# Map of allowed extra parameter name -> expected type. A param is OK
# only when both match. Add entries here together with the signature
# change + a reason in the module docstring above.
ALLOWED_EXTRA_PARAMS: dict[str, object] = {
    "ctx": typing.Optional[ExecutionContext],
}


def test_recommend_accepts_only_a_storm_profile():
    sig = inspect.signature(recommend)
    params = list(sig.parameters.values())
    hints = typing.get_type_hints(recommend)

    # First parameter must be the StormProfile, positional. This is
    # the security contract.
    assert params, "FORECAST signature lost its profile parameter"
    first = params[0]
    assert hints.get(first.name) is StormProfile, (
        f"FORECAST first parameter type changed: expected StormProfile, "
        f"got {hints.get(first.name)}. Adding a raw-data type (DataFrame, "
        f"str, Path, Connector, etc.) here breaks the security contract."
    )
    assert first.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )

    # Any additional parameter must appear in ALLOWED_EXTRA_PARAMS
    # with the expected type — keeps the door shut on silent raw-data
    # additions without blocking documented logger-style hooks.
    for extra in params[1:]:
        assert extra.name in ALLOWED_EXTRA_PARAMS, (
            f"FORECAST gained unknown parameter {extra.name!r}. "
            f"If this is intentional, add it to ALLOWED_EXTRA_PARAMS "
            f"in this test with a justification in the module docstring."
        )
        expected = ALLOWED_EXTRA_PARAMS[extra.name]
        actual = hints.get(extra.name)
        assert actual == expected, (
            f"FORECAST parameter {extra.name!r} type changed: expected "
            f"{expected}, got {actual}."
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
