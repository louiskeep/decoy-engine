"""`delivery`: how a run's outputs become visible.

`strict` makes every output visible together or none of them. `best_effort` makes visible the
tables that passed and drops each failed table with the tables that depend on it. The default
is dropped from the dump (by value, `_global_settings.py` precedent), so a config that predates
the block keeps its exact dump and plan hash; `best_effort` moves the hash because it changes
what a failed run leaves behind.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

DeliveryMode = Literal["strict", "best_effort"]


class DeliveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)

    mode: DeliveryMode = "strict"
