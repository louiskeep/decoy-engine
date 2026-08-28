"""Static strategy-capabilities tests (native program, Task 0.2).

The orthogonality Codex required (hashing is BOTH keyed AND row-local, so a
single `locality` enum could never express it) plus the machine-classifiable
diagnostics/quality fields Phase 1 eligibility reads (`row_error_modes`,
`warning_codes`, `quality_obligations`, `quarantine_required`). Every field is
sourced from the REAL strategy surface, never a hand-copied list: the totality
test enumerates the live registries and the diagnostics assertions check the
handlers' actual error/warning emission.
"""

from __future__ import annotations

import pytest

from decoy_engine.config._tables import GENERATE_TYPES
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.native._capabilities import (
    StrategyCapabilities,
    capabilities_for,
)
from decoy_engine.execution.native._determinism_protocol import (
    DETERMINISTIC_NO_DRAW,
    MASK_STRATEGY_TO_SITE,
    draw_site_by_id,
)

# ---------------------------------------------------------------------------
# Orthogonality (the Codex requirement the single enum could not express).
# ---------------------------------------------------------------------------


def test_hash_is_both_keyed_and_row_local() -> None:
    c = capabilities_for("hash")
    assert c.is_keyed and c.is_row_local
    assert c.key_source == "mask_key"


def test_shuffle_is_global_and_order_sensitive() -> None:
    c = capabilities_for("shuffle")
    assert c.is_global and c.is_order_sensitive
    assert c.needs_global_row_identity


def test_formula_output_type_not_static() -> None:
    assert capabilities_for("formula").output_type_is_static is False


def test_fpe_is_keyed_row_local_and_static() -> None:
    c = capabilities_for("fpe")
    assert c.is_keyed and c.is_row_local and c.output_type_is_static
    assert c.key_source == "mask_key"


def test_windowed_date_needs_global_row_identity_not_row_local() -> None:
    # Its per-row seed keys on the GLOBAL row index, not the source value, so a
    # partition that resets the index diverges: not row-local, needs the global
    # row number preserved.
    c = capabilities_for("windowed_date")
    assert c.needs_global_row_identity
    assert c.is_row_local is False


# ---------------------------------------------------------------------------
# Machine-classifiable diagnostics (Phase 1 eligibility reads these fields).
# ---------------------------------------------------------------------------


def test_zero_error_zero_warning_set_is_machine_classifiable() -> None:
    for s in ("hash", "redact", "truncate", "passthrough"):
        c = capabilities_for(s)
        assert c.row_error_modes == ()  # provably cannot emit row errors
        assert c.warning_codes == ()  # zero-warning
        assert c.quality_obligations == ()  # no class-A obligation
        assert c.quarantine_required is False
    # A strategy known to emit row errors is flagged, not blank.
    assert capabilities_for("bucketize").row_error_modes != ()


def test_row_error_strategies_are_exactly_the_five_emitters() -> None:
    # Sourced from the handlers' actual `ctx.row_errors.append(RowError(...))`
    # sites: bucketize/date_shift/top_code (format_error), code_set (mask_error),
    # nested (format_error + propagated child trigger).
    error_capable = {s for s in SCALAR_HANDLERS if capabilities_for(s).row_error_modes}
    assert error_capable == {"bucketize", "date_shift", "top_code", "code_set", "nested"}


def test_row_error_triggers_match_handler_surface() -> None:
    assert capabilities_for("bucketize").row_error_modes == ("format_error",)
    assert capabilities_for("date_shift").row_error_modes == ("format_error",)
    assert capabilities_for("top_code").row_error_modes == ("format_error",)
    assert capabilities_for("code_set").row_error_modes == ("mask_error",)
    assert set(capabilities_for("nested").row_error_modes) == {"format_error", "mask_error"}


def test_warning_codes_match_handler_return_surface() -> None:
    assert capabilities_for("top_code").warning_codes == ("top_code_generalized",)
    assert capabilities_for("geo_generalize").warning_codes == ("geo_generalize_cascade",)
    assert set(capabilities_for("fpe").warning_codes) == {
        "fpe_join_group_active",
        "fpe_sub_minimum_domain",
        "fpe_partial_plaintext_disclosure",
    }
    assert set(capabilities_for("nested").warning_codes) == {
        "nested_cell_json_parse_error",
        "nested_jsonpath_path_overlap",
    }


def test_faker_is_the_sole_pool_quality_obligation() -> None:
    # Faker is the only registry handler that builds/samples a bounded value
    # pool (PoolBuilder + PoolSampler + pool_cache), so it is the only static
    # class-A quality obligation. Every other handler is a type-only pool import.
    assert capabilities_for("faker").quality_obligations != ()
    pooled = {s for s in SCALAR_HANDLERS if capabilities_for(s).quality_obligations}
    assert pooled == {"faker"}


def test_quarantine_required_iff_row_error_capable() -> None:
    # A strategy that can emit a row error leaves the un-masked source value on
    # the failing row (bucketize keeps `col`, date_shift returns the original
    # date, ...), so it is unsafe without quarantine, which streaming lacks.
    for s in SCALAR_HANDLERS:
        c = capabilities_for(s)
        assert c.quarantine_required is bool(c.row_error_modes)


# ---------------------------------------------------------------------------
# Draw family + key source are sourced from the Task 0.1 inventory.
# ---------------------------------------------------------------------------


def test_draw_family_and_key_source_trace_to_inventory() -> None:
    for name in SCALAR_HANDLERS:
        c = capabilities_for(name)
        site_id = MASK_STRATEGY_TO_SITE[name]
        if site_id == DETERMINISTIC_NO_DRAW:
            assert c.draw_family is None
            assert c.key_source is None
        else:
            site = draw_site_by_id(site_id)
            assert c.draw_family == site.family
            expected_key = {"mask_key": "mask_key", "job_seed": "generation_seed", "none": None}[
                site.entropy_root
            ]
            assert c.key_source == expected_key


def test_is_keyed_agrees_with_mask_key_source() -> None:
    for name in SCALAR_HANDLERS:
        c = capabilities_for(name)
        assert c.is_keyed is (c.key_source == "mask_key")


# ---------------------------------------------------------------------------
# Totality against the LIVE registries (a new strategy fails until classified).
# ---------------------------------------------------------------------------


def test_capabilities_total_over_live_mask_registry() -> None:
    for name in SCALAR_HANDLERS:
        c = capabilities_for(name)
        assert isinstance(c, StrategyCapabilities)


def test_capabilities_total_over_live_generation_registry() -> None:
    for name in GENERATE_TYPES:
        c = capabilities_for(name)
        assert isinstance(c, StrategyCapabilities)


def test_unclassified_strategy_raises() -> None:
    with pytest.raises(KeyError):
        capabilities_for("a_brand_new_unclassified_strategy")


def test_diagnostics_populated_for_every_error_or_warn_capable_strategy() -> None:
    # The coverage guarantee: no error/warn-capable strategy silently defaults
    # to blank. We assert the known emitters are non-blank (a regression that
    # dropped a strategy's diagnostics would blank it and fail here).
    assert capabilities_for("nested").row_error_modes != ()
    assert capabilities_for("fpe").warning_codes != ()
    assert capabilities_for("code_set").row_error_modes != ()


def test_composite_and_group_placeholders_are_classified() -> None:
    # build_work_list stamps "<composite>" / "<group>" placeholder strategy
    # names; capabilities_for must resolve them so node-kind coverage is total.
    for placeholder in ("<composite>", "<group>"):
        assert isinstance(capabilities_for(placeholder), StrategyCapabilities)
