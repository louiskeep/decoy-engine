"""V1-reused-by-V2 transform implementations (slim 3-file leaf package).

S22-CL-V1GRAPHRUNNER (2026-05-30): all V1-only transform strategies were
deleted (apply_context, bucketize, categorical, factory, faker_based,
format_preservation, hash, passthrough, redact, reference, registry,
shuffle, truncate). The three files that remain are V2-LOAD-BEARING --
the V2 strategy handlers in ``execution/_strategies/_date_shift.py``,
``_formula.py``, and ``_fpe.py`` import from them per best-practices
§6.2 ("use established methodology; we do not roll our own"). The V2
docstrings explicitly cite the reuse.

The package's __init__.py is intentionally minimal: only the
``BaseMaskingStrategy`` ABC is re-exported because the three kept
strategies depend on it. The V1 ``StrategyManager`` / ``create_strategy``
registry was deleted alongside the V1 graph runner; V2 strategy
dispatch lives in ``execution/_adapter.py``.

A future sprint may move these three files to
``execution/_strategies/_reused_v1/`` to fully retire the ``transforms``
namespace. Deferred because the move is structural churn without
behavioral change, and the V2 strategy import paths would shift.
"""

from decoy_engine.transforms.base import BaseMaskingStrategy

__all__ = [
    "BaseMaskingStrategy",
]
