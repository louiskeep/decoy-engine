"""The one post-scan route guard: routes that run no post-scan refuse a model post-scan config.

The sequential, out-of-core and chunked entries mask without running the post-scan, so a config
that asks for a model post-scan detector or for `ner_findings: fail` would publish output nobody
checked. Each of those entries calls `refuse_unchecked_route` as its first statement, before any
source is read. Regex-only `integrity: true` keeps today's behavior on those routes (the skip is
a separate, recorded follow-up). `run_pipeline`'s resident routes run the post-scan, so they do
not call the guard.

Pattern: a fail-closed admission check at the route entry with a registered, value-free refusal,
the behavior `DeclaredCastRouteError` (`execution/_errors.py`) already has for declared casting.
The error lives here, beside the guard, so verification never imports a private execution module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine import reasons
from decoy_engine.config import post_scan_integrity_requested
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


def _asks_for_models(detectors: object) -> bool:
    # Anything other than a list of `regex` entries counts as a model ask: fail closed on a raw dict.
    if detectors is None:
        return False
    if isinstance(detectors, list | tuple):
        return any(d != "regex" for d in detectors)
    return detectors != "regex"


def refuse_unchecked_route(config: Mapping[str, Any], route: str) -> None:
    """Raise `PostScanRouteError` when `config` asks this no-post-scan route for a model check."""
    if not post_scan_integrity_requested(config):
        return
    block = config["post_scan"]
    if _asks_for_models(block.get("detectors")):
        setting = "post_scan.detectors"
    elif block.get("ner_findings") == "fail":
        setting = "post_scan.ner_findings"
    else:
        return
    # Lazy: the helper sits in the secret-provider package, which this module need not load.
    from decoy_engine.secret_providers._errors import raise_bare

    raise_bare(PostScanRouteError(route, setting))
