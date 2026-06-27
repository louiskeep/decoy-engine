"""Pydantic config models for the SP-05 validator framework and quarantine.

These models are the schema layer for the ``validators:`` and ``quarantine:``
top-level blocks in the pipeline config. They are validated by ``PipelineConfig``
at plan-compile time; downstream engine code reads the validated dict rather
than re-validating.

``extra="forbid"`` on both models ensures that unknown keys in the YAML are
rejected at config-load time with a clear error message (consistent with the
rest of the config schema).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ValidatorEntry(BaseModel):
    """One validator in the ``validators:`` list.

    ``name`` must match one of the built-in validator names (luhn, npi, iban,
    vin, fk_intact, no_orphan_children). Unknown names are rejected at runtime
    by the validator registry with a ``ValueError`` naming the offending
    validator and listing the known names.

    ``columns`` carries the per-table column lists for check-digit validators
    (luhn, npi, iban, vin). FK validators (fk_intact, no_orphan_children) read
    the ``relationships:`` block from the top-level config; ``columns`` is
    unused for them.

    Example YAML::

        validators:
          - name: luhn
            columns:
              orders: [credit_card_number]
          - name: fk_intact
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    columns: dict[str, list[str]] = Field(default_factory=dict)


class QuarantineConfig(BaseModel):
    """The ``quarantine:`` block in the pipeline config.

    When ``enabled`` is ``True`` and a configured ``trigger`` fires, the
    offending row is written to ``output_path`` instead of the main output.
    The job continues and completes successfully.

    Supported triggers (SP-05):
      - ``validation_fail``: row failed a job-level validator.
      - ``format_error``: row contained a malformed value at format conversion.
      - ``mask_error``: row triggered an error during the mask phase.

    Example YAML::

        quarantine:
          enabled: true
          output_path: /mnt/quarantine/run-2026-06-27.jsonl
          triggers:
            - validation_fail
            - format_error
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_path: str = ""
    triggers: list[str] = Field(default_factory=list)
