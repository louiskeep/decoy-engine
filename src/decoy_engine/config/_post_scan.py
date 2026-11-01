"""`post_scan`: opt-in controls for the Post-scan phase.

`integrity` enables exactly the existing post-mask integrity suite (residual
PII, FK preservation, policy validation), report-only. It does not enable
Pre-scan discovery or any other Post-scan check; `post_validation` and
`fidelity_report` stay separate subpass controls.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class PostScanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    integrity: bool = False


def post_scan_integrity_requested(config: Mapping[str, Any]) -> bool:
    """True when a validated config dict opts in to Post-scan integrity."""
    block = config.get("post_scan")
    return bool(block.get("integrity")) if isinstance(block, Mapping) else False
