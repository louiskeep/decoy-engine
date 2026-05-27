"""GlobalSettings: top-level knobs shared across the pipeline.

Per S1 spec line 152 (`global_settings: {seed: ...}`) + advisory axis 6
(reuse V1's `global_settings` naming convention; not a shim, a naming
choice).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class GlobalSettings(BaseModel):
    """Pipeline-wide settings.

    `seed` is the job-level seed material (S1 stub derivation; S3 swaps
    in real HMAC-keyed material). `post_validation` opts the pipeline
    into post-mask invariant checks per the operating model §Validation
    requirements.
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    post_validation: bool = False
