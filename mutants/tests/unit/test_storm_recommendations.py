"""Locks the DEFAULT_STRATEGY_BY_DETECTOR invariants.

This table is the single source of truth that FORECAST + the platform's
column-override endpoint both consume. A typo here ships a bad default to
every user - the tests assert structural invariants so a future edit can't
silently break the contract.
"""

import pytest

from decoy_engine.storm.detectors import REGISTERED_DETECTORS
from decoy_engine.storm.recommendations import (
    DEFAULT_STRATEGY_BY_DETECTOR,
    VALID_STRATEGIES,
    get_default_strategy,
    known_detector_ids,
)


def _registered_detector_ids() -> set[str]:
    """All detector_ids the engine fires under in V1.

    Pulled from REGISTERED_DETECTORS' function names - each
    detector_X function emits detector_id="X" in its DetectorMatch.
    """
    return {fn.__name__.removeprefix("detect_") for fn in REGISTERED_DETECTORS}


class TestStrategyTableShape:
    @pytest.mark.parametrize("detector_id", list(DEFAULT_STRATEGY_BY_DETECTOR))
    def test_every_entry_has_valid_strategy_name(self, detector_id):
        strategy, params = DEFAULT_STRATEGY_BY_DETECTOR[detector_id]
        assert strategy in VALID_STRATEGIES, (
            f"{detector_id} maps to unknown strategy {strategy!r}; "
            f"add to VALID_STRATEGIES or fix the typo"
        )
        assert isinstance(params, dict), (
            f"{detector_id} params should be a dict, got {type(params).__name__}"
        )

    def test_every_registered_detector_has_a_default(self):
        """Catch the case where someone adds a new built-in detector but
        forgets to wire a smart default - the override endpoint would
        return None and the UI would show the column with no suggested
        strategy."""
        registered = _registered_detector_ids()
        defaulted = known_detector_ids()
        missing = registered - defaulted
        assert not missing, (
            f"Registered detectors with no default strategy: {sorted(missing)}. "
            f"Add an entry to DEFAULT_STRATEGY_BY_DETECTOR for each."
        )

    def test_no_orphan_strategies(self):
        """Catch the case where a strategy is in the table but no detector
        ever produces that id - dead code in the lookup."""
        registered = _registered_detector_ids()
        defaulted = known_detector_ids()
        orphans = defaulted - registered
        assert not orphans, (
            f"Strategy entries for unregistered detectors: {sorted(orphans)}. "
            f"Either register the detector or remove the entry."
        )


class TestStrategyLookup:
    def test_known_detector_returns_pair(self):
        result = get_default_strategy("ssn")
        assert result is not None
        strategy, params = result
        assert strategy == "fpe"
        # FPE in the engine takes `charset` (not "alphabet") and
        # preserves length naturally - there's no "length" param.
        assert params == {"charset": "digits"}

    def test_unknown_detector_returns_none(self):
        # Custom detectors and typos should both return None.
        assert get_default_strategy("custom__uk_nhs_number") is None
        assert get_default_strategy("totally_made_up") is None


class TestRedactDefaults:
    """V1 ships redact-by-default for detectors where the value's semantic
    or structural format preservation is V2 (see gap doc). The tests
    enforce that the redact value is documented + the rest of the params
    are minimal - so a careless edit can't accidentally drop a fake-but-
    realistic value into a redact'd column."""

    REDACT_DETECTORS = [
        "icd10",
        "url",
        "license_num",
        "health_plan_id",
        "device_id",
        "biometric_id",
    ]

    @pytest.mark.parametrize("detector_id", REDACT_DETECTORS)
    def test_redact_default_has_redacted_marker(self, detector_id):
        strategy, params = DEFAULT_STRATEGY_BY_DETECTOR[detector_id]
        assert strategy == "redact", f"{detector_id} should redact in V1 (semantic FPE is V2)"
        # The CVV entry uses "XXX"; the rest use "REDACTED". Both are
        # valid "obviously redacted" strings - assert it's at least one
        # of those. The engine's redact strategy keys this as
        # `redact_with` (not `value`), so the param assertion matches
        # what the strategy class actually reads.
        assert params.get("redact_with") in {"REDACTED", "XXX"}, (
            f"{detector_id} redact_with should be 'REDACTED' or 'XXX', "
            f"got {params.get('redact_with')!r}"
        )


class TestFormatPreservingDefaults:
    """The other half: detectors whose default IS format-preserving must
    point at fpe / date_shift / faker.* - never redact."""

    FORMAT_PRESERVING = [
        "first_name",
        "last_name",
        "person_name",
        "email",
        "us_phone",
        "fax_number",
        "address",
        "ssn",
        "iso_date",
        "us_date",
        "eu_date",
        "us_zip",
        "ipv4",
        "mrn",
        "npi",
        "pan",
        "iban",
        "vehicle_id",
    ]

    @pytest.mark.parametrize("detector_id", FORMAT_PRESERVING)
    def test_does_not_redact(self, detector_id):
        strategy, _ = DEFAULT_STRATEGY_BY_DETECTOR[detector_id]
        assert strategy != "redact", f"{detector_id} should format-preserve, not redact"
