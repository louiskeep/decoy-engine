"""D3: pure unit tests for the frozen decision-code catalog and the
planner-prose translators (`execution/physical/_reasons.py`).

Fast, in-memory, string-only -- these exist to give mutmut a dense,
easy-to-run target for the catalog's own logic (the translators, the
native-reason classifier), independent of the slower real-job D4 corpus in
`test_compiler_preflight_equivalence.py`.
"""

from __future__ import annotations

from decoy_engine.execution.physical import _reasons


def test_native_reason_code_family_classifies_static_codes() -> None:
    assert _reasons.native_reason_code_family("execution_mode_not_auto") == "static"
    assert _reasons.native_reason_code_family("non_pandas_substrate:polars") == "static"
    assert _reasons.native_reason_code_family("unsupported_strategy:col:faker") == "static"
    assert _reasons.native_reason_code_family("vault_column:ssn") == "static"


def test_native_reason_code_family_classifies_scan_codes() -> None:
    assert _reasons.native_reason_code_family("zero_row_source") == "scan"
    assert (
        _reasons.native_reason_code_family("unsupported_projection:missing=[]:extra=[]") == "scan"
    )
    assert _reasons.native_reason_code_family("non_utf8_column:n:int64") == "scan"
    assert (
        _reasons.native_reason_code_family("native_preflight_reroute:n:redact:integer:partial_null")
        == "scan"
    )
    assert _reasons.native_reason_code_family("native_chunk_schema_drift") == "scan"


def test_native_reason_code_family_flags_unknown_codes() -> None:
    assert _reasons.native_reason_code_family("something_never_seen_before") == "unknown"


def test_translate_polars_rejection_no_mask_work() -> None:
    prose = "no mask-kind work; the polars-native loop masks existing data (generation uses the synthesize path)"
    assert _reasons.translate_polars_rejection(prose) == (_reasons.CODE_NO_MASK_WORK,)


def test_translate_polars_rejection_substrate() -> None:
    prose = "resolved substrate is 'pandas'; the polars-native loop requires the polars substrate"
    assert _reasons.translate_polars_rejection(prose) == ("substrate_is:pandas",)


def test_translate_polars_rejection_fk_resolution() -> None:
    prose = "fk_resolution: FK edges route through the pandas oracle"
    assert _reasons.translate_polars_rejection(prose) == (_reasons.CODE_FK_RESOLUTION,)


def test_translate_polars_rejection_non_native_work() -> None:
    prose = "non-polars-native work: fpe, hash"
    assert _reasons.translate_polars_rejection(prose) == ("non_polars_native_work:fpe, hash",)


def test_translate_polars_rejection_combines_multiple_reasons() -> None:
    prose = (
        "no mask-kind work; the polars-native loop masks existing data "
        "(generation uses the synthesize path); "
        "resolved substrate is 'pandas'; the polars-native loop requires the polars substrate"
    )
    codes = _reasons.translate_polars_rejection(prose)
    assert _reasons.CODE_NO_MASK_WORK in codes
    assert "substrate_is:pandas" in codes


def test_translate_polars_rejection_unclassified_fallback() -> None:
    prose = "a brand new reason nobody wrote a template for"
    assert _reasons.translate_polars_rejection(prose) == (f"unclassified_polars_rejection:{prose}",)


def test_translate_chunked_rejection_no_mask_tables() -> None:
    assert _reasons.translate_chunked_rejection("no mask-kind tables to stream") == (
        _reasons.CODE_NO_MASK_TABLES,
    )


def test_translate_chunked_rejection_generate_tables_present() -> None:
    prose = (
        "generate-kind table(s) people present; chunked execution masks existing "
        "data and has no generation mode"
    )
    assert _reasons.translate_chunked_rejection(prose) == (_reasons.CODE_GENERATE_TABLES_PRESENT,)


def test_translate_chunked_rejection_masks_one_table_per_run() -> None:
    prose = "chunked execution masks one table per run; job declares 2 mask tables (a, b)"
    assert _reasons.translate_chunked_rejection(prose) == (_reasons.CODE_MASKS_ONE_TABLE_PER_RUN,)


def test_translate_chunked_rejection_substrate() -> None:
    prose = (
        "resolved substrate is 'polars'; the chunked route constructs the pandas "
        "adapter, so routing would silently change the job's executed substrate"
    )
    assert _reasons.translate_chunked_rejection(prose) == ("substrate_is:polars",)


def test_translate_chunked_rejection_relationships_unsupported() -> None:
    prose = (
        "chunked_relationships_unsupported: configs with FK relationships cannot "
        "run chunked (resolving a child key reads the whole parent frame)"
    )
    assert _reasons.translate_chunked_rejection(prose) == ("chunked_relationships_unsupported",)


def test_translate_chunked_rejection_check_chunked_compatibility_code_passthrough() -> None:
    prose = "strategy_not_chunk_safe: column 'x' uses a strategy that is not value-keyed"
    assert _reasons.translate_chunked_rejection(prose) == ("strategy_not_chunk_safe",)


def test_translate_chunked_rejection_lazy_source_both_split_halves_same_code() -> None:
    prose = (
        "source for table 't' is a lazy (LazySource) handle, not a resident frame; "
        "the chunk-stable-dtype runtime gate needs real column data, so auto-chunk "
        "conservatively declines rather than force-materializing it just to decide"
    )
    codes = _reasons.translate_chunked_rejection(prose)
    assert codes == (_reasons.CODE_CHUNKED_LAZY_SOURCE_UNSUPPORTED,)


def test_translate_chunked_rejection_below_threshold() -> None:
    prose = "source holds 5 rows, below the auto-chunk threshold (100000); full-frame is cheaper than streaming at this size"
    assert _reasons.translate_chunked_rejection(prose) == (
        _reasons.CODE_CHUNKED_SOURCE_BELOW_THRESHOLD,
    )


def test_translate_chunked_rejection_combines_generic_prefix_codes() -> None:
    prose = (
        "chunked_relationships_unsupported: configs with FK relationships cannot "
        "run chunked (resolving a child key reads the whole parent frame); "
        "non-scalar (composite bundle) work on table 't': address_full"
    )
    codes = _reasons.translate_chunked_rejection(prose)
    assert "chunked_relationships_unsupported" in codes
    assert _reasons.CODE_NON_SCALAR_COMPOSITE in codes


def test_translate_chunked_rejection_unclassified_fallback() -> None:
    prose = "a brand new chunked reason nobody wrote a template for"
    assert _reasons.translate_chunked_rejection(prose) == (
        f"unclassified_chunked_rejection:{prose}",
    )


def test_translate_relationship_mode_reason_deferred() -> None:
    from decoy_engine.execution._planner import RELATIONSHIP_ROUTE_DEFERRED

    assert (
        _reasons.translate_relationship_mode_reason(RELATIONSHIP_ROUTE_DEFERRED)
        == _reasons.CODE_RELATIONSHIP_ROUTE_DEFERRED
    )


def test_translate_relationship_mode_reason_no_relationship() -> None:
    from decoy_engine.execution._planner import _NO_RELATIONSHIP_ROUTE

    assert (
        _reasons.translate_relationship_mode_reason(_NO_RELATIONSHIP_ROUTE)
        == _reasons.CODE_NO_RELATIONSHIP_ROUTE
    )


def test_translate_relationship_mode_reason_unclassified() -> None:
    prose = "some other relationship text"
    assert (
        _reasons.translate_relationship_mode_reason(prose)
        == f"unclassified_relationship_mode_reason:{prose}"
    )


def test_precompilation_excluded_codes_are_disjoint_from_route_reason_codes() -> None:
    assert _reasons.PRECOMPILATION_EXCLUDED_CODES.isdisjoint(_reasons.ROUTE_REASON_CODES)


def test_driver_selection_codes_are_disjoint_from_route_reason_codes() -> None:
    assert _reasons.DRIVER_SELECTION_CODES.isdisjoint(_reasons.ROUTE_REASON_CODES)
