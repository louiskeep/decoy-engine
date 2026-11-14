"""The one post-scan route guard: routes that run no post-scan refuse a model post-scan config.

The sequential, out-of-core and chunked entries mask without running the post-scan, so a config
that asks for a model post-scan detector or for `ner_findings: fail` would publish output nobody
checked. Each of those entries calls `refuse_unchecked_route` as its first statement, before any
source is read. `run_pipeline` keeps a model ask on its resident routes, which run the post-scan,
and judges regex-only `integrity: true` from the staged output at commit, so a model ask only
reaches these entries when a caller calls them directly.

Pattern: a fail-closed admission check at the route entry with a registered, value-free refusal,
the behavior `DeclaredCastRouteError` (`execution/_errors.py`) already has for declared casting.
The error lives here, beside the guard, so verification never imports a private execution module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine import reasons
from decoy_engine.config import post_scan_model_setting
from decoy_engine.errors import DecoyError

_REASON = reasons.route.POST_SCAN_MODELS_UNSUPPORTED

__all__ = ["PostScanRouteError", "refuse_unchecked_route"]


class PostScanRouteError(DecoyError):
    """A route that runs no post-scan was given a model post-scan config; refused before any read.

    `str(error)` and `message` are the registry's fixed summary. `route` names the refusing route
    and `setting` the config field that asked for the check; neither is a config value.
    """

    code = _REASON

    def __init__(self, route: str, setting: str = "post_scan.detectors") -> None:
        self.route = route
        self.setting = setting
        self.reason = f"{_REASON.namespace}.{_REASON}"
        self.message = reasons.get(_REASON.namespace, _REASON).summary
        super().__init__(self.message)

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.route, self.setting))


def refuse_unchecked_route(config: Mapping[str, Any], route: str) -> None:
    """Raise `PostScanRouteError` when `config` asks this no-post-scan route for a model check."""
    setting = post_scan_model_setting(config)
    if setting is None:
        return
    # Lazy: the helper sits in the secret-provider package, which this module need not load.
    from decoy_engine.secret_providers._errors import raise_bare

    raise_bare(PostScanRouteError(route, setting))
