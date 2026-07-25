"""engine-v2 S7 Mimesis adapter package (optional backend).

Importing this package requires the optional ``mimesis`` dependency. The
default engine install does NOT pull it; the registry gates on
``importlib.util.find_spec("mimesis")`` before importing, so the engine runs
fine with the default 24-provider catalog when Mimesis is absent. This guard
gives a clear message for direct imports.

Public API (only available when ``mimesis`` is installed):

    from decoy_engine.providers_v2.mimesis import (
        MimesisAdapter,
        run_parity_suite,
        ParityCheckResult,
    )

Spec: docs/v2/sprints/engine-v2/sprint-07-mimesis-adapter.md in decoy-platform.
"""

from __future__ import annotations

try:
    import mimesis as _mimesis  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via subprocess test
    raise ImportError(
        "decoy_engine.providers_v2.mimesis requires the optional 'mimesis' "
        "dependency. Install it with `pip install decoy-engine[mimesis]`."
    ) from exc

from decoy_engine.providers_v2.mimesis._adapter import (
    MimesisAdapter,
    mimesis_capability,
)
from decoy_engine.providers_v2.mimesis._adoption_matrix import (
    ADOPTED_MIMESIS_PROVIDERS,
    MIMESIS_CANDIDATES,
)
from decoy_engine.providers_v2.mimesis._parity import (
    ParityCheckResult,
    is_adoptable,
    run_parity_suite,
)

__all__ = [
    "ADOPTED_MIMESIS_PROVIDERS",
    "MIMESIS_CANDIDATES",
    "MimesisAdapter",
    "ParityCheckResult",
    "is_adoptable",
    "mimesis_capability",
    "run_parity_suite",
]
