"""Plan-time checks grouped by the config they read (CODEMAP: new plan checks land here)."""

from decoy_engine.plan.checks._generate_columns import check_generate_columns

__all__ = ["check_generate_columns"]
