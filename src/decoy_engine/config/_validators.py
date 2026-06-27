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

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Triggers that are fully wired in this SP release (SP-05).
# format_error and mask_error are reserved names for future wiring;
# they are NOT accepted yet so operators get a clear error rather than
# a silent no-op. See the carry-forward in the platform quarantine-rows
# backlog doc (decoy-platform phase5-gaps).
_WIRED_TRIGGERS: frozenset[str] = frozenset({"validation_fail"})


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

    Wired triggers (SP-05):
      - ``validation_fail``: row failed a job-level validator.

    Reserved (not yet wired - see carry-forward in p5-b-quarantine-rows.md):
      - ``format_error``: reserved for future wiring (malformed value at
        format conversion). Rejected at config validation until wired.
      - ``mask_error``: reserved for future wiring (error during mask phase).
        Rejected at config validation until wired.

    Example YAML::

        quarantine:
          enabled: true
          output_path: /mnt/quarantine/run-2026-06-27.jsonl
          triggers:
            - validation_fail
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_path: str = ""
    triggers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fail_closed_when_enabled(self) -> QuarantineConfig:
        """Enforce fail-closed invariants when enabled is True.

        1. output_path must be non-empty/non-whitespace: a valid enabled
           quarantine block with no output_path would silently drop rows
           (data loss). Rejected up front.
        2. Every trigger must be in the wired set: an unwired trigger
           (format_error, mask_error) appears to quarantine those rows
           but does nothing, creating a silent no-op. Rejected up front
           with a message naming the unwired trigger.
        """
        if not self.enabled:
            return self

        if not self.output_path or not self.output_path.strip():
            raise ValueError(
                "quarantine output_path must not be empty when enabled is True; "
                "a missing output_path would silently drop quarantined rows"
            )

        for trigger in self.triggers:
            if trigger not in _WIRED_TRIGGERS:
                raise ValueError(
                    f"trigger {trigger!r} is not yet wired in SP-05 and would be "
                    f"a silent no-op. Wired triggers: {sorted(_WIRED_TRIGGERS)}. "
                    "See the platform quarantine-rows backlog doc "
                    "(decoy-platform phase5-gaps) for the carry-forward plan."
                )

        return self
